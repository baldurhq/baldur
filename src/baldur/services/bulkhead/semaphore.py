"""
Semaphore Bulkhead - one seat count shared by threads and event loops.

A bulkhead implementation suitable for I/O-bound work. It keeps its own seat
count under a lock instead of an OS semaphore, so sync callers (any thread) and
async callers (any event loop) draw on the same capacity, and a waiting caller
is never bound to one event loop.

Usage:
    bulkhead = SemaphoreBulkhead("database", max_concurrent=10)

    # Fail immediately without a timeout (non-blocking)
    with bulkhead.acquire():
        do_database_work()

    # Fail after waiting at most 1 second
    with bulkhead.acquire(timeout=1.0):
        do_database_work()

    # From a coroutine — waits without blocking the event loop
    async with bulkhead.acquire_async(timeout=1.0):
        await do_async_work()
"""

from __future__ import annotations

import _thread
import asyncio
import time
from collections import deque
from collections.abc import AsyncGenerator, Generator
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime
from typing import Any

import structlog

from baldur.core.process_utils import fork_safe_lock
from baldur.services.bulkhead.base import (
    Bulkhead,
    BulkheadState,
    BulkheadType,
)
from baldur.services.bulkhead.exceptions import BulkheadFullError
from baldur.services.bulkhead.metrics import increment_rejected_count
from baldur.utils.time import utc_now

logger = structlog.get_logger()

__all__ = ["SemaphoreBulkhead"]

# Pending-operation tag for one seat given back. A waiter leaving without a
# seat is queued as the waiter object itself.
_SEAT_RETURNED = object()


def _running_loop() -> asyncio.AbstractEventLoop | None:
    """Return the event loop running on this thread, or None."""
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None


def _set_wake(future: asyncio.Future[None]) -> None:
    """Resolve a loop waiter's future unless it already finished."""
    if not future.done():
        future.set_result(None)


class _ThreadWaiter:
    """A thread waiting for a seat.

    The wake token is a raw lock taken when the waiter is built: the thread
    waits with ``token.acquire(timeout=...)`` (one C call) and is woken by one
    ``token.release()``, which never waits.
    """

    __slots__ = ("deadline", "queued", "token", "woken")

    def __init__(self, deadline: float) -> None:
        self.deadline = deadline
        self.queued = False
        self.woken = False
        self.token = _thread.allocate_lock()
        self.token.acquire()

    def is_dead(self, now: float) -> bool:
        return now >= self.deadline

    def needs_rearm(self) -> bool:
        return self.woken

    def wake(self) -> bool:
        """Wake the thread; True when the wake reached a live waiter."""
        if not self.woken:
            self.woken = True
            self.token.release()
        return True

    def rearm(self) -> None:
        """Make the next wait block until the next wake."""
        self.woken = False
        self.token.acquire(False)


class _LoopWaiter:
    """A coroutine waiting for a seat on its own running event loop."""

    __slots__ = ("deadline", "future", "loop", "queued", "woken")

    def __init__(self, loop: asyncio.AbstractEventLoop, deadline: float) -> None:
        self.deadline = deadline
        self.loop = loop
        self.future: asyncio.Future[None] = loop.create_future()
        self.queued = False
        self.woken = False

    def is_dead(self, now: float) -> bool:
        return self.loop.is_closed() or now >= self.deadline

    def needs_rearm(self) -> bool:
        return self.woken or self.future.done()

    def wake(self) -> bool:
        """Wake the coroutine; True when its loop can run the wake now.

        A loop that is not running still gets the wake (it may resume), but
        the caller is told to wake the next waiter as well, so a freed seat
        reaches a live one. A closed loop refuses the wake.
        """
        if not self.woken:
            try:
                self.loop.call_soon_threadsafe(_set_wake, self.future)
            except RuntimeError:
                return False
            self.woken = True
        return self.loop.is_running()

    def rearm(self) -> None:
        """Give the next wait a fresh future."""
        self.woken = False
        self.future = self.loop.create_future()


_Waiter = _ThreadWaiter | _LoopWaiter


class SemaphoreBulkhead(Bulkhead):
    """
    Semaphore-style bulkhead with one seat count for sync and async callers.

    Limits the concurrent execution count to prevent resource exhaustion.
    Suitable for I/O-bound work (DB queries, cache lookups, etc.).

    Features:
    - One capacity shared by sync callers (any thread) and async callers
      (any event loop)
    - Timeout-bounded waiting that never blocks an event loop
    - Rejection statistics tracking

    Admission rules:
    - A caller takes a seat itself, under the compartment lock, when fewer
      than ``max_concurrent`` are taken; a seat is never handed to a waiter.
      A release wakes the oldest waiter, which re-checks — an arriving caller
      may take the freed seat first (no first-come order), and the woken
      waiter then waits again for its remaining time.
    - A waiter whose deadline passes re-checks once and takes a free seat if
      there is one.
    - A sync call made on a thread that is running an event loop gives the
      immediate verdict: the seat it would wait for can be held by a
      coroutine of the very loop it would block.
    - ``release()`` never waits for a lock, so it is safe wherever it runs —
      including a ``finally`` that the garbage collector runs for an
      abandoned coroutine.
    """

    def __init__(
        self,
        name: str,
        max_concurrent: int = 10,
        fair: bool = True,  # noqa: ARG002 - kept for signature compatibility
    ):
        """
        Args:
            name: Bulkhead name (domain identifier)
            max_concurrent: Maximum concurrent execution count
            fair: Ignored. Waiting callers get no first-come order (see the
                class docstring).
        """
        self._name = name
        self._max_concurrent = max_concurrent
        self._lock = fork_safe_lock()

        # Seat count and FIFO waiter queue — changed only under ``_lock``.
        self._taken = 0
        self._waiters: deque[_Waiter] = deque()
        # Operations queued without the lock (a seat given back, a waiter
        # leaving). ``deque.append`` / ``popleft`` are atomic, so a releaser
        # never waits: every lock holder applies the list before unlocking
        # and re-checks it after unlocking.
        self._pending: deque[Any] = deque()

        # Statistics
        self._rejected_count = 0
        self._last_rejection_time: datetime | None = None

    @property
    def name(self) -> str:
        """Bulkhead name."""
        return self._name

    # ------------------------------------------------------------------
    # Lock protocol
    # ------------------------------------------------------------------

    def _enter(self) -> None:
        """Take the compartment lock (blocking) and bring the state current.

        An exception raised while settling (a signal handler's, on any
        bytecode) unlocks before it propagates, so the lock is never left held.
        """
        self._lock.acquire()
        try:
            self._settle()
        except BaseException:
            self._lock.release()
            raise

    def _exit(self) -> None:
        """Apply queued operations, unlock, then pick up any queued meanwhile."""
        try:
            self._settle()
        finally:
            self._lock.release()
        self._apply_pending_nonblocking()

    def _apply_pending_nonblocking(self) -> None:
        """Apply the pending list if the lock is free; never waits.

        A busy lock means its holder applies the list before unlocking and
        re-checks it after unlocking, so an operation queued here is never
        stranded.
        """
        while self._pending and self._lock.acquire(blocking=False):
            try:
                self._settle()
            finally:
                self._lock.release()

    def _settle(self) -> None:
        """Apply pending operations and prune dead waiters (lock held)."""
        pending = self._pending
        while pending:
            op = pending.popleft()
            if op is _SEAT_RETURNED:
                if self._taken > 0:
                    self._taken -= 1
                    self._wake_next()
            else:
                self._drop_waiter(op)
        if self._waiters:
            self._prune()

    def _wake_next(self) -> None:
        """Wake the oldest waiter not yet woken (lock held).

        A waiter whose loop is not running gets the wake but does not absorb
        it — the next waiter is woken too. A waiter whose loop refuses the
        wake (closed) leaves the queue.
        """
        for waiter in list(self._waiters):
            if waiter.woken:
                continue
            if waiter.wake():
                return
            if not waiter.woken:
                self._unqueue(waiter)

    def _prune(self) -> None:
        """Remove waiters whose loop closed or deadline passed (lock held).

        A pruned waiter that had been woken passes its wake on. A pruned
        waiter that runs later does its own deadline re-check.
        """
        now = time.monotonic()
        lost_wakes = 0
        for waiter in list(self._waiters):
            if waiter.is_dead(now):
                self._unqueue(waiter)
                if waiter.woken:
                    waiter.woken = False
                    lost_wakes += 1
        for _ in range(lost_wakes):
            self._wake_next()

    def _unqueue(self, waiter: _Waiter) -> None:
        if waiter.queued:
            waiter.queued = False
            self._waiters.remove(waiter)

    def _enqueue(self, waiter: _Waiter) -> None:
        waiter.queued = True
        self._waiters.append(waiter)

    def _requeue(self, waiter: _Waiter) -> None:
        """Wait again for the remaining time (lock held).

        A woken waiter that found its seat taken by an arriving caller goes
        to the tail with a fresh wake; an unwoken one keeps its place.
        """
        if waiter.needs_rearm() or not waiter.queued:
            self._unqueue(waiter)
            waiter.rearm()
            self._enqueue(waiter)

    def _drop_waiter(self, waiter: _Waiter) -> None:
        """A waiter left without a seat: unqueue it, pass on its wake (lock held)."""
        self._unqueue(waiter)
        if waiter.woken:
            waiter.woken = False
            self._wake_next()

    def _take_seat_locked(self) -> bool:
        if self._taken < self._max_concurrent:
            self._taken += 1
            return True
        return False

    def _count_rejection_locked(self) -> None:
        self._rejected_count += 1
        self._last_rejection_time = utc_now()

    def _full_error(self) -> BulkheadFullError:
        return BulkheadFullError(
            bulkhead_name=self._name,
            max_concurrent=self._max_concurrent,
            active_count=self._taken,
        )

    # ------------------------------------------------------------------
    # Sync entry points
    # ------------------------------------------------------------------

    @contextmanager
    def acquire(self, timeout: float | None = None) -> Generator[None, None, None]:
        """
        Acquire a seat for the body of a ``with`` block.

        Args:
            timeout: Wait timeout (seconds). If None, fail immediately
                (non-blocking). Ignored on a thread running an event loop
                (immediate verdict).

        Yields:
            None

        Raises:
            BulkheadFullError: When resource acquisition fails
        """
        if not self.try_acquire(timeout):
            raise self._full_error()
        try:
            yield
        finally:
            self.release()

    def try_acquire(self, timeout: float | None = None) -> bool:
        """
        Attempt to take a seat, waiting up to ``timeout`` for one.

        Args:
            timeout: Upper bound on waiting (seconds). None means no waiting
                (immediate verdict). On a thread that is running an event loop
                the verdict is always immediate.

        Returns:
            True if a seat was taken (release it with :meth:`release`)
        """
        if timeout is not None and _running_loop() is not None:
            timeout = None
        waiter: _ThreadWaiter | None = None
        self._enter()
        try:
            if self._take_seat_locked():
                return True
            if timeout is None or timeout <= 0:
                self._count_rejection_locked()
            else:
                waiter = _ThreadWaiter(time.monotonic() + timeout)
                self._enqueue(waiter)
        finally:
            self._exit()
        if waiter is None:
            # Emit outside the lock — the prometheus client takes its own lock.
            increment_rejected_count(self._name)
            return False
        return self._wait_as_thread(waiter)

    def _wait_as_thread(self, waiter: _ThreadWaiter) -> bool:
        while True:
            remaining = waiter.deadline - time.monotonic()
            try:
                if remaining > 0:
                    waiter.token.acquire(True, remaining)
            except BaseException:
                # A signal handler's exception interrupted the wait (POSIX lock
                # waits are interruptible): leave the queue — passing on a
                # wake this waiter received — without waiting for the lock.
                self._pending.append(waiter)
                self._apply_pending_nonblocking()
                raise
            self._enter()
            try:
                if self._take_seat_locked():
                    self._unqueue(waiter)
                    return True
                if time.monotonic() < waiter.deadline:
                    self._requeue(waiter)
                    continue
                self._drop_waiter(waiter)
                self._count_rejection_locked()
            finally:
                self._exit()
            increment_rejected_count(self._name)
            return False

    def release(self) -> None:
        """Give a seat back. Never waits for a lock."""
        self._pending.append(_SEAT_RETURNED)
        self._apply_pending_nonblocking()

    # ------------------------------------------------------------------
    # Async entry points
    # ------------------------------------------------------------------

    async def try_acquire_async(self, timeout: float | None = None) -> bool:
        """
        Attempt to take a seat from a coroutine, waiting up to ``timeout``.

        Waits on a future of the running loop, so the loop keeps serving
        other work, and any loop in the process can wait on the same
        compartment.

        Args:
            timeout: Upper bound on waiting (seconds). None means no waiting
                (immediate verdict).

        Returns:
            True if a seat was taken (release it with :meth:`release`)
        """
        waiter: _LoopWaiter | None = None
        self._enter()
        try:
            if self._take_seat_locked():
                return True
            if timeout is None or timeout <= 0:
                self._count_rejection_locked()
            else:
                waiter = _LoopWaiter(
                    asyncio.get_running_loop(), time.monotonic() + timeout
                )
                self._enqueue(waiter)
        finally:
            self._exit()
        if waiter is None:
            increment_rejected_count(self._name)
            return False
        return await self._wait_as_coroutine(waiter)

    async def _wait_as_coroutine(self, waiter: _LoopWaiter) -> bool:
        while True:
            remaining = waiter.deadline - time.monotonic()
            try:
                if remaining > 0:
                    try:
                        await asyncio.wait_for(waiter.future, remaining)
                    except TimeoutError:
                        pass
            except BaseException:
                # Cancelled, or finalized by the garbage collector (which can
                # run this on any thread, inside any lock): queue the removal
                # and never wait.
                self._pending.append(waiter)
                self._apply_pending_nonblocking()
                raise
            self._enter()
            try:
                if self._take_seat_locked():
                    self._unqueue(waiter)
                    return True
                if time.monotonic() < waiter.deadline:
                    self._requeue(waiter)
                    continue
                self._drop_waiter(waiter)
                self._count_rejection_locked()
            finally:
                self._exit()
            increment_rejected_count(self._name)
            return False

    @asynccontextmanager
    async def acquire_async(
        self, timeout: float | None = None
    ) -> AsyncGenerator[None, None]:
        """
        Hold a seat for the body of an ``async with`` block.

        Args:
            timeout: Upper bound on waiting (seconds). None means no waiting.

        Yields:
            None

        Raises:
            BulkheadFullError: When no seat is available within ``timeout``
        """
        if not await self.try_acquire_async(timeout):
            raise self._full_error()
        try:
            yield
        finally:
            self.release()

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    def get_state(self) -> BulkheadState:
        """Return the current state (sync and async callers alike)."""
        self._enter()
        try:
            return BulkheadState(
                name=self._name,
                bulkhead_type=BulkheadType.SEMAPHORE,
                max_concurrent=self._max_concurrent,
                active_count=self._taken,
                waiting_count=len(self._waiters),
                rejected_count=self._rejected_count,
                last_rejection_time=self._last_rejection_time,
            )
        finally:
            self._exit()
