"""One seat count per semaphore compartment, shared by threads and event loops.

A ``SemaphoreBulkhead`` keeps its own seat count under its lock, with a queue
of thread waiters and loop waiters, so sync callers (any thread) and async
callers (any event loop) together never exceed ``max_concurrent``, and no wait
is bound to one event loop.

Verification techniques applied:
- Boundary analysis: N holders admitted, the N+1th rejected, across sync and
  async callers and two event loops.
- Exit-path inventory: every way a waiter leaves (timeout, cancel while
  waiting, cancel after its wake, an exception in the body, a closed / stopped
  / blocked loop) leaves no count behind and loses no wake.
- Deadlock detection: an abandoned holder finalized by the garbage collector
  on a thread that holds the compartment lock, or is inside ``get_state()``,
  or while another thread waits for its seat.
- Randomized invariant (seeded): sync / async acquire, cancel, timeout and
  abandon-and-collect, then taken + free = capacity and N admitted.
- Contract: the ``Bulkhead`` ABC's async defaults, ``BulkheadState.queue_size``.
- Side effects: async holders appear on ``/bulkheads`` and the Prometheus
  gauges; the registry's async handle shares the registered compartment.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import gc
import random
import threading
import time
from collections.abc import Callable, Coroutine, Generator
from typing import Any
from unittest.mock import patch

import pytest

from baldur.api.handlers.bulkhead import bulkhead_status
from baldur.core.connection_health import ConnectionType
from baldur.interfaces.web_framework import HttpMethod, RequestContext
from baldur.services.bulkhead import metrics as bulkhead_metrics
from baldur.services.bulkhead.async_semaphore import AsyncSemaphoreBulkhead
from baldur.services.bulkhead.base import Bulkhead, BulkheadState, BulkheadType
from baldur.services.bulkhead.exceptions import BulkheadFullError
from baldur.services.bulkhead.registry import BulkheadRegistry
from baldur.services.bulkhead.semaphore import SemaphoreBulkhead

# Upper bound on any wait the test expects to end.
_WAIT_S = 5.0
# A waiter deadline far past _WAIT_S: a waiter admitted within _WAIT_S was
# reached by a wake, not by the re-check at its own deadline.
_LONG_DEADLINE_S = 20.0
# A waiter timeout meant to expire.
_SHORT_S = 0.05
# Poll interval for a state another thread or loop changes.
_POLL_S = 0.002
# A sync timeout given on an event-loop thread, and the bound for "at once"
# — half of it, so a loaded host cannot blur the two.
_LOOP_THREAD_TIMEOUT_S = 5.0
_IMMEDIATE_S = _LOOP_THREAD_TIMEOUT_S / 2
# A cyclic collection on a large, contended heap can take seconds.
_COLLECT_BUDGET_S = 15.0
# Joins for the randomized load: every operation is bounded by its own
# timeout, but abandon-and-collect runs collections concurrently.
_STRESS_BUDGET_S = 20.0

_poll = threading.Event()


def _eventually(predicate: Callable[[], bool], timeout: float = _WAIT_S) -> bool:
    """Poll a state another thread or event loop changes."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        _poll.wait(_POLL_S)
    return predicate()


def _join_all(threads: list[threading.Thread], budget: float = _WAIT_S) -> None:
    """Join every thread against one shared deadline."""
    deadline = time.monotonic() + budget
    for thread in threads:
        thread.join(max(0.0, deadline - time.monotonic()))
    assert not any(t.is_alive() for t in threads)


def _waiting(bulkhead: Bulkhead) -> int:
    return bulkhead.get_state().waiting_count


def _taken(bulkhead: Bulkhead) -> int:
    return bulkhead.get_state().active_count


def _assert_idle_and_admits_capacity(bulkhead: SemaphoreBulkhead) -> None:
    """No count is left behind, and exactly ``max_concurrent`` new callers fit."""
    state = bulkhead.get_state()
    assert state.active_count == 0
    assert state.waiting_count == 0
    admitted = [bulkhead.try_acquire() for _ in range(state.max_concurrent)]
    assert all(admitted)
    assert bulkhead.try_acquire() is False
    for _ in admitted:
        bulkhead.release()
    assert _taken(bulkhead) == 0


class _LoopThread:
    """An event loop running forever on its own daemon thread."""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self._stopped = threading.Event()
        self._thread = threading.Thread(daemon=True, target=self._run)
        self._thread.start()
        assert _eventually(self.loop.is_running)

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_forever()
        finally:
            self._stopped.set()

    def submit(self, coro: Coroutine[Any, Any, Any]) -> concurrent.futures.Future[Any]:
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    def block(self) -> threading.Event:
        """Block the loop's thread until the returned event is set."""
        unblock = threading.Event()
        entered = threading.Event()

        def _blocked() -> None:
            entered.set()
            unblock.wait(_WAIT_S)

        self.loop.call_soon_threadsafe(_blocked)
        assert entered.wait(_WAIT_S)
        return unblock

    def stop(self) -> None:
        """Stop the loop (it can be closed or resumed afterwards)."""
        if not self.loop.is_closed() and self.loop.is_running():
            self.loop.call_soon_threadsafe(self.loop.stop)
        assert self._stopped.wait(_WAIT_S)
        self._thread.join(_WAIT_S)

    def close_abandoning_tasks(self) -> None:
        """Stop and close the loop with its tasks left pending."""
        self.stop()
        self.loop.close()

    def shutdown(self) -> None:
        """Cancel every task, then stop and close the loop."""
        if self.loop.is_closed():
            return
        if self.loop.is_running():

            async def _cancel_all() -> None:
                current = asyncio.current_task()
                tasks = [t for t in asyncio.all_tasks() if t is not current]
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

            self.submit(_cancel_all()).result(_WAIT_S)
        self.stop()
        self.loop.close()


@pytest.fixture
def loops() -> Generator[Callable[[], _LoopThread], None, None]:
    """Factory of loop threads, all shut down at teardown."""
    started: list[_LoopThread] = []

    def _start() -> _LoopThread:
        loop_thread = _LoopThread()
        started.append(loop_thread)
        return loop_thread

    yield _start
    for loop_thread in started:
        loop_thread.shutdown()


async def _hold_until(
    bulkhead: Bulkhead, release: asyncio.Event, timeout: float | None = None
) -> bool:
    """Take a seat from a coroutine and hold it until ``release`` is set."""
    if not await bulkhead.try_acquire_async(timeout):
        return False
    try:
        await release.wait()
    finally:
        bulkhead.release()
    return True


def _abandon_pending_holder(bulkhead: SemaphoreBulkhead, own_finally: bool) -> None:
    """Leave a coroutine holding a seat on a loop that is closed under it.

    The coroutine stays pending in a reference cycle (task -> coroutine ->
    the future it awaits -> its wakeup callback -> task), so only a cyclic
    garbage collection finalizes it — and runs its ``finally`` (the
    ``acquire_async`` exit, or the holder's own ``release()``) on whatever
    thread runs that collection.
    """
    loop = asyncio.new_event_loop()

    async def _context_manager_holder() -> None:
        async with bulkhead.acquire_async():
            await asyncio.get_running_loop().create_future()

    async def _own_finally_holder() -> None:
        assert await bulkhead.try_acquire_async()
        try:
            await asyncio.get_running_loop().create_future()
        finally:
            bulkhead.release()

    holder = _own_finally_holder if own_finally else _context_manager_holder
    task = loop.create_task(holder())
    while _taken(bulkhead) == 0:
        loop.run_until_complete(asyncio.sleep(0))
    assert not task.done()
    loop.close()


@contextlib.contextmanager
def _collector_paused() -> Generator[None, None, None]:
    """Keep the automatic collector from finalizing the holder early."""
    was_enabled = gc.isenabled()
    gc.disable()
    try:
        yield
    finally:
        if was_enabled:
            gc.enable()


# =============================================================================
# One capacity for sync and async callers
# =============================================================================


class TestSemaphoreSharedCapacityBehavior:
    """Sync and async callers of one compartment draw on one seat count."""

    @pytest.mark.asyncio
    async def test_sync_holders_at_capacity_reject_async_caller(self):
        """N sync holders fill the compartment for async callers too."""
        # Given — every seat taken by a sync caller.
        bulkhead = SemaphoreBulkhead("shared", max_concurrent=3)
        for _ in range(3):
            assert bulkhead.try_acquire() is True

        # When / Then — the N+1th caller is async and finds no seat.
        assert await bulkhead.try_acquire_async(None) is False
        with pytest.raises(BulkheadFullError):
            async with bulkhead.acquire_async(timeout=_SHORT_S):
                pass

        # Then — one sync release admits one async caller, and only one.
        bulkhead.release()
        assert await bulkhead.try_acquire_async(None) is True
        assert await bulkhead.try_acquire_async(None) is False
        for _ in range(3):
            bulkhead.release()
        _assert_idle_and_admits_capacity(bulkhead)

    @pytest.mark.asyncio
    async def test_async_holders_at_capacity_reject_sync_caller(self):
        """N async holders fill the compartment for sync callers too."""
        bulkhead = SemaphoreBulkhead("shared", max_concurrent=3)
        for _ in range(3):
            assert await bulkhead.try_acquire_async(None) is True

        assert bulkhead.try_acquire() is False
        with pytest.raises(BulkheadFullError):
            with bulkhead.acquire():
                pass

        bulkhead.release()
        assert bulkhead.try_acquire() is True
        for _ in range(3):
            bulkhead.release()
        _assert_idle_and_admits_capacity(bulkhead)

    def test_mixed_sync_and_async_callers_never_run_more_than_capacity(self, loops):
        """Threads and two event loops together run at most N bodies at once."""
        # Given — capacity 3, four sync callers and two coroutines on each of
        # two loops, all waiting long enough to be admitted in turn.
        capacity = 3
        bulkhead = SemaphoreBulkhead("mixed", max_concurrent=capacity)
        gate = threading.Event()
        lock = threading.Lock()
        in_flight = {"now": 0, "max": 0, "done": 0}

        def _enter() -> None:
            with lock:
                in_flight["now"] += 1
                in_flight["max"] = max(in_flight["max"], in_flight["now"])

        def _leave() -> None:
            with lock:
                in_flight["now"] -= 1
                in_flight["done"] += 1

        def _sync_caller() -> None:
            with bulkhead.acquire(timeout=_WAIT_S):
                _enter()
                gate.wait(_WAIT_S)
                _leave()

        async def _async_caller() -> None:
            async with bulkhead.acquire_async(timeout=_WAIT_S):
                _enter()
                while not gate.is_set():
                    await asyncio.sleep(_POLL_S)
                _leave()

        loop_a, loop_b = loops(), loops()

        # When — everyone arrives; the gate opens once the compartment is full.
        threads = [threading.Thread(daemon=True, target=_sync_caller) for _ in range(4)]
        for thread in threads:
            thread.start()
        futures = [
            lt.submit(_async_caller()) for lt in (loop_a, loop_b) for _ in range(2)
        ]
        assert _eventually(lambda: in_flight["now"] == capacity)
        assert _eventually(lambda: _waiting(bulkhead) == 8 - capacity)
        gate.set()
        _join_all(threads)
        for future in futures:
            future.result(_WAIT_S)

        # Then — every caller ran, never more than N at once, nothing left.
        assert in_flight["done"] == 8
        assert in_flight["max"] == capacity
        _assert_idle_and_admits_capacity(bulkhead)

    def test_async_callers_on_two_loops_are_admitted_or_rejected(self, loops):
        """A second event loop gets a verdict, never a RuntimeError."""
        # Given — the only seat held by a sync caller.
        bulkhead = SemaphoreBulkhead("two-loops", max_concurrent=1)
        assert bulkhead.try_acquire() is True
        loop_a, loop_b = loops(), loops()

        async def _short_wait() -> bool:
            return await bulkhead.try_acquire_async(_SHORT_S)

        # When — both loops contend; both are rejected while the seat is held.
        rejected = [lt.submit(_short_wait()).result(_WAIT_S) for lt in (loop_a, loop_b)]

        # Then — then both wait at once on the one seat and both get it in turn.
        async def _admitted_then_release() -> bool:
            if not await bulkhead.try_acquire_async(_WAIT_S):
                return False
            bulkhead.release()
            return True

        waits = [lt.submit(_admitted_then_release()) for lt in (loop_a, loop_b)]
        assert _eventually(lambda: _waiting(bulkhead) == 2)
        bulkhead.release()
        admitted = [w.result(_WAIT_S) for w in waits]

        assert rejected == [False, False]
        assert admitted == [True, True]
        _assert_idle_and_admits_capacity(bulkhead)


# =============================================================================
# Waiter exits — no count left behind, no wake lost
# =============================================================================


async def _exit_by_timeout(bulkhead: SemaphoreBulkhead) -> None:
    """A waiter whose deadline passes with no free seat is rejected."""
    assert await bulkhead.try_acquire_async(_SHORT_S) is False


async def _exit_by_cancel_while_waiting(bulkhead: SemaphoreBulkhead) -> None:
    """A waiter cancelled while queued leaves the queue."""
    waiter = asyncio.create_task(bulkhead.try_acquire_async(_LONG_DEADLINE_S))
    while _waiting(bulkhead) == 0:
        await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter


async def _exit_by_cancel_after_wake(bulkhead: SemaphoreBulkhead) -> None:
    """A woken waiter cancelled before it re-checks passes its wake on.

    The cancel is issued before the release, both before the loop runs
    again: the release wakes the oldest waiter (W1), which then leaves
    without a seat. The next waiter (W2, deadline far away) gets the seat
    only if W1 passed the wake on.
    """
    w1 = asyncio.create_task(bulkhead.try_acquire_async(_LONG_DEADLINE_S))
    while _waiting(bulkhead) < 1:
        await asyncio.sleep(0)
    w2 = asyncio.create_task(bulkhead.try_acquire_async(_LONG_DEADLINE_S))
    while _waiting(bulkhead) < 2:
        await asyncio.sleep(0)

    w1.cancel()
    bulkhead.release()

    with pytest.raises(asyncio.CancelledError):
        await w1
    # W2 now holds the seat in place of the scenario's holder.
    assert await asyncio.wait_for(w2, _WAIT_S) is True


async def _exit_by_body_exception(bulkhead: SemaphoreBulkhead) -> None:
    """An admitted caller whose body raises gives its seat back."""
    bulkhead.release()
    with pytest.raises(ValueError, match="declined"):
        async with bulkhead.acquire_async(timeout=_WAIT_S):
            raise ValueError("declined")
    assert bulkhead.try_acquire() is True


_WAITER_EXITS = [
    _exit_by_timeout,
    _exit_by_cancel_while_waiting,
    _exit_by_cancel_after_wake,
    _exit_by_body_exception,
]
_WAITER_EXIT_IDS = ["timeout", "cancel_waiting", "cancel_after_wake", "body_raises"]


class TestSemaphoreWaiterQueueBehavior:
    """Every way a waiter leaves the queue keeps the counts and the wakes."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("exit_kind", _WAITER_EXITS, ids=_WAITER_EXIT_IDS)
    async def test_waiter_exit_leaves_no_count_behind(self, exit_kind):
        """After the exit and the holder's release, the compartment is idle."""
        # Given — the only seat held, so every arriving async caller waits.
        bulkhead = SemaphoreBulkhead("exits", max_concurrent=1)
        assert bulkhead.try_acquire() is True

        # When
        await exit_kind(bulkhead)
        bulkhead.release()

        # Then
        _assert_idle_and_admits_capacity(bulkhead)

    def test_waiter_on_closed_loop_is_not_counted_and_its_seat_goes_on(self, loops):
        """A queued waiter whose loop closed drops out; a live waiter is admitted."""
        # Given — the seat held; a waiter queued on a loop that is then closed.
        bulkhead = SemaphoreBulkhead("closed-loop", max_concurrent=1)
        assert bulkhead.try_acquire() is True
        doomed = loops()
        doomed.submit(bulkhead.try_acquire_async(_LONG_DEADLINE_S))
        assert _eventually(lambda: _waiting(bulkhead) == 1)
        doomed.close_abandoning_tasks()

        # When — a thread waits behind it, then the seat is released.
        admitted: list[bool] = []
        waiter = threading.Thread(
            daemon=True,
            target=lambda: admitted.append(bulkhead.try_acquire(_LONG_DEADLINE_S)),
        )
        state_with_dead_waiter = bulkhead.get_state()
        waiter.start()
        assert _eventually(lambda: _waiting(bulkhead) == 1)
        bulkhead.release()
        _join_all([waiter])

        # Then — the dead waiter was never counted; the thread got the seat
        # well before its own deadline.
        assert state_with_dead_waiter.waiting_count == 0
        assert admitted == [True]
        bulkhead.release()
        _assert_idle_and_admits_capacity(bulkhead)

    def test_release_while_oldest_waiter_loop_is_stopped_admits_next_waiter(
        self, loops
    ):
        """A wake to a stopped loop also wakes the next waiter; a later close prunes it."""
        # Given — the oldest waiter queued on a loop that is then stopped.
        bulkhead = SemaphoreBulkhead("stopped-loop", max_concurrent=1)
        assert bulkhead.try_acquire() is True
        stopped = loops()
        stopped.submit(bulkhead.try_acquire_async(_LONG_DEADLINE_S))
        assert _eventually(lambda: _waiting(bulkhead) == 1)
        stopped.stop()

        admitted: list[bool] = []
        waiter = threading.Thread(
            daemon=True,
            target=lambda: admitted.append(bulkhead.try_acquire(_LONG_DEADLINE_S)),
        )
        waiter.start()
        assert _eventually(lambda: _waiting(bulkhead) == 2)

        # When — the seat is released while the oldest waiter cannot run.
        bulkhead.release()
        _join_all([waiter])

        # Then — the live waiter was admitted before its deadline; once the
        # stopped loop closes, its woken waiter leaves the queue.
        assert admitted == [True]
        assert _waiting(bulkhead) == 1
        stopped.loop.close()
        assert _waiting(bulkhead) == 0
        bulkhead.release()
        _assert_idle_and_admits_capacity(bulkhead)

    def test_waiter_deadline_takes_free_seat_while_earlier_waiter_loop_is_blocked(
        self, loops
    ):
        """No rejection with a free seat: the deadline re-check takes it."""
        # Given — the oldest waiter on a loop that is then blocked, and a
        # thread waiter with a short deadline behind it.
        bulkhead = SemaphoreBulkhead("blocked-loop", max_concurrent=1)
        assert bulkhead.try_acquire() is True
        blocked = loops()
        oldest = blocked.submit(_hold_once(bulkhead, _LONG_DEADLINE_S))
        assert _eventually(lambda: _waiting(bulkhead) == 1)
        unblock = blocked.block()
        thread_deadline_s = 2.0
        admitted: list[bool] = []
        waiter = threading.Thread(
            daemon=True,
            target=lambda: admitted.append(bulkhead.try_acquire(thread_deadline_s)),
        )
        waiter.start()
        assert _eventually(lambda: _waiting(bulkhead) == 2)

        # When — the release wakes the blocked waiter, which cannot claim.
        bulkhead.release()
        _join_all([waiter])

        # Then — the thread took the free seat at its deadline instead of
        # being rejected; the blocked waiter gets it once it runs again.
        assert admitted == [True]
        unblock.set()
        bulkhead.release()
        assert oldest.result(_WAIT_S) is True
        _assert_idle_and_admits_capacity(bulkhead)


async def _hold_once(bulkhead: SemaphoreBulkhead, timeout: float) -> bool:
    """Wait for a seat, then give it straight back."""
    if not await bulkhead.try_acquire_async(timeout):
        return False
    bulkhead.release()
    return True


# =============================================================================
# release() from a finalizer — nothing deadlocks
# =============================================================================


def _collect_inside_get_state(bulkhead: SemaphoreBulkhead) -> None:
    """Run a cyclic collection on this thread while it is inside get_state()."""

    def _state_built_after_collect(*args: Any, **kwargs: Any) -> BulkheadState:
        gc.collect()
        return BulkheadState(*args, **kwargs)

    with patch(
        "baldur.services.bulkhead.semaphore.BulkheadState",
        new=_state_built_after_collect,
    ):
        bulkhead.get_state()


def _collect_holding_compartment_lock(bulkhead: SemaphoreBulkhead) -> None:
    """Run a cyclic collection on this thread while it holds the compartment lock."""
    with bulkhead._lock:
        gc.collect()


def _collect_without_lock(bulkhead: SemaphoreBulkhead) -> None:  # noqa: ARG001 - same shape as the other collection sites
    """Run a cyclic collection on this thread, holding nothing."""
    gc.collect()


_COLLECTION_SITES = [
    _collect_inside_get_state,
    _collect_holding_compartment_lock,
    _collect_without_lock,
]
_COLLECTION_SITE_IDS = ["inside_get_state", "holding_lock", "no_lock"]


class TestSemaphoreFinalizerReleaseBehavior:
    """An abandoned holder finalized anywhere returns its seat without deadlock."""

    @pytest.mark.parametrize("collect", _COLLECTION_SITES, ids=_COLLECTION_SITE_IDS)
    @pytest.mark.parametrize(
        "own_finally", [False, True], ids=["acquire_async_exit", "own_finally"]
    )
    def test_collected_holder_seat_reaches_waiting_thread(self, collect, own_finally):
        """The finalizer's release never blocks; its seat reaches the waiter.

        A release queued while the collecting thread held the lock is applied
        by whichever thread takes the lock next — here, the test's own state
        read after the collection.
        """
        # Given — the only seat held by a coroutine on a loop that was closed
        # under it, and a thread waiting for that seat (deadline far away, so
        # its admission comes from the release, not its deadline re-check).
        bulkhead = SemaphoreBulkhead("finalizer", max_concurrent=1)
        admitted: list[bool] = []
        with _collector_paused():
            _abandon_pending_holder(bulkhead, own_finally=own_finally)
            waiter = threading.Thread(
                daemon=True,
                target=lambda: admitted.append(bulkhead.try_acquire(_LONG_DEADLINE_S)),
            )
            waiter.start()
            assert _eventually(lambda: _waiting(bulkhead) == 1)

            # When — the collection runs on another thread at the chosen site,
            # then the next lock operation brings the state current.
            collector = threading.Thread(daemon=True, target=collect, args=(bulkhead,))
            collector.start()
            _join_all([collector], budget=_COLLECT_BUDGET_S)
            bulkhead.get_state()
            _join_all([waiter])

        # Then — neither thread deadlocked, and the waiter was admitted.
        assert admitted == [True]
        bulkhead.release()
        _assert_idle_and_admits_capacity(bulkhead)

    @pytest.mark.parametrize(
        "own_finally", [False, True], ids=["acquire_async_exit", "own_finally"]
    )
    def test_collected_holder_seat_returns_with_no_waiter(self, own_finally):
        """With nobody waiting, a collection inside get_state() frees the seat."""
        bulkhead = SemaphoreBulkhead("finalizer-idle", max_concurrent=1)
        with _collector_paused():
            _abandon_pending_holder(bulkhead, own_finally=own_finally)
            assert bulkhead.try_acquire() is False

            collector = threading.Thread(
                daemon=True, target=_collect_inside_get_state, args=(bulkhead,)
            )
            collector.start()
            _join_all([collector], budget=_COLLECT_BUDGET_S)

        _assert_idle_and_admits_capacity(bulkhead)


# =============================================================================
# Randomized seat-count invariant
# =============================================================================

_STRESS_CAPACITY = 3
_STRESS_OPS = 40


class TestSemaphoreStressInvariantBehavior:
    """Randomized sync / async / cancel / timeout / abandon load keeps the count exact."""

    @pytest.mark.parametrize("seed", [11, 23, 37, 59])
    def test_random_load_leaves_taken_plus_free_equal_to_capacity(self, seed, loops):
        """At quiescence nothing is taken or waiting, and exactly N are admitted."""
        # Given
        rng = random.Random(seed)
        bulkhead = SemaphoreBulkhead("stress", max_concurrent=_STRESS_CAPACITY)
        loop_a, loop_b = loops(), loops()
        guard = threading.Lock()
        in_flight = {"now": 0, "max": 0}

        def _enter() -> None:
            with guard:
                in_flight["now"] += 1
                in_flight["max"] = max(in_flight["max"], in_flight["now"])

        def _leave() -> None:
            with guard:
                in_flight["now"] -= 1

        hold = threading.Event()  # never set: holds end by their own timeout

        def _sync_op(timeout: float | None, hold_s: float) -> None:
            if bulkhead.try_acquire(timeout):
                try:
                    _enter()
                    hold.wait(hold_s)
                    _leave()
                finally:
                    bulkhead.release()

        async def _async_op(timeout: float | None, hold_s: float) -> None:
            if not await bulkhead.try_acquire_async(timeout):
                return
            try:
                _enter()
                try:
                    await asyncio.sleep(hold_s)
                finally:
                    _leave()
            finally:
                bulkhead.release()

        async def _cancelled_op(timeout: float, cancel_after_s: float) -> None:
            task = asyncio.ensure_future(_async_op(timeout, cancel_after_s * 2))
            await asyncio.sleep(cancel_after_s)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        def _abandon_and_collect(collect_on: str) -> None:
            # A holder if a seat is free, else a waiter: both are finalized
            # by the collection on the chosen thread.
            _abandon_pending_waiter_or_holder(bulkhead)
            if collect_on == "loop_a":
                loop_a.submit(_collect_async()).result(_WAIT_S)
            elif collect_on == "loop_b":
                loop_b.submit(_collect_async()).result(_WAIT_S)
            else:
                gc.collect()

        threads: list[threading.Thread] = []
        futures: list[concurrent.futures.Future[Any]] = []

        # When — a seeded mix of operations, all running concurrently.
        for _ in range(_STRESS_OPS):
            kind = rng.choice(["sync", "async", "cancel", "timeout", "abandon"])
            timeout = rng.choice([None, 0.01, 0.05, 0.2])
            hold_s = rng.uniform(0.0, 0.005)
            target_loop = rng.choice([loop_a, loop_b])
            if kind == "sync":
                threads.append(
                    threading.Thread(
                        daemon=True, target=_sync_op, args=(timeout, hold_s)
                    )
                )
            elif kind == "async":
                futures.append(target_loop.submit(_async_op(timeout, hold_s)))
            elif kind == "cancel":
                futures.append(target_loop.submit(_cancelled_op(0.2, hold_s)))
            elif kind == "timeout":
                futures.append(target_loop.submit(_async_op(0.01, hold_s)))
            else:
                collect_on = rng.choice(["here", "loop_a", "loop_b"])
                threads.append(
                    threading.Thread(
                        daemon=True, target=_abandon_and_collect, args=(collect_on,)
                    )
                )
        with _collector_paused():
            for thread in threads:
                thread.start()
            _join_all(threads, budget=_STRESS_BUDGET_S)
            for future in futures:
                future.result(_STRESS_BUDGET_S)
            gc.collect()

        # Then
        assert in_flight["max"] <= _STRESS_CAPACITY
        _assert_idle_and_admits_capacity(bulkhead)


async def _collect_async() -> None:
    gc.collect()


def _abandon_pending_waiter_or_holder(bulkhead: SemaphoreBulkhead) -> None:
    """Leave a holder (seat free) or a waiter (none free) pending on a closed loop."""
    loop = asyncio.new_event_loop()

    async def _pending() -> None:
        async with bulkhead.acquire_async(timeout=_LONG_DEADLINE_S):
            await asyncio.get_running_loop().create_future()

    loop.create_task(_pending())
    loop.run_until_complete(asyncio.sleep(0))
    loop.close()


# =============================================================================
# A sync call on an event-loop thread never waits
# =============================================================================


class TestSemaphoreLoopThreadVerdictBehavior:
    """acquire / try_acquire with a timeout give the immediate verdict on a loop thread."""

    @pytest.mark.asyncio
    async def test_sync_acquire_inside_coroutine_rejects_at_once_when_loop_holds_seats(
        self,
    ):
        """The loop is not blocked for the timeout; the call is rejected at once."""
        # Given — this loop's coroutines hold every seat.
        bulkhead = SemaphoreBulkhead("loop-thread", max_concurrent=2)
        release = asyncio.Event()
        holders = [
            asyncio.create_task(_hold_until(bulkhead, release)) for _ in range(2)
        ]
        while _taken(bulkhead) < 2:
            await asyncio.sleep(0)

        # When
        started = time.monotonic()
        with pytest.raises(BulkheadFullError):
            with bulkhead.acquire(timeout=_LOOP_THREAD_TIMEOUT_S):
                pass
        acquire_elapsed = time.monotonic() - started
        started = time.monotonic()
        tried = bulkhead.try_acquire(timeout=_LOOP_THREAD_TIMEOUT_S)
        try_elapsed = time.monotonic() - started

        # Then
        assert tried is False
        assert acquire_elapsed < _IMMEDIATE_S
        assert try_elapsed < _IMMEDIATE_S
        assert _waiting(bulkhead) == 0
        release.set()
        assert await asyncio.gather(*holders) == [True, True]
        _assert_idle_and_admits_capacity(bulkhead)

    def test_sync_acquire_off_loop_thread_waits_for_released_seat(self):
        """Off a loop thread the same timeout is a real wait."""
        bulkhead = SemaphoreBulkhead("off-loop", max_concurrent=1)
        assert bulkhead.try_acquire() is True
        admitted: list[bool] = []
        waiter = threading.Thread(
            daemon=True,
            target=lambda: admitted.append(bulkhead.try_acquire(_LONG_DEADLINE_S)),
        )
        waiter.start()
        assert _eventually(lambda: _waiting(bulkhead) == 1)

        bulkhead.release()
        _join_all([waiter])

        assert admitted == [True]
        bulkhead.release()
        _assert_idle_and_admits_capacity(bulkhead)


# =============================================================================
# Bulkhead ABC async defaults; BulkheadState.queue_size
# =============================================================================


class _CountingBulkhead(Bulkhead):
    """A third-party-style subclass that overrides only the abstract methods."""

    def __init__(self, capacity: int) -> None:
        self._capacity = capacity
        self.taken = 0
        self.try_acquire_timeouts: list[float | None] = []
        self.release_calls = 0

    @property
    def name(self) -> str:
        return "third-party"

    @contextlib.contextmanager
    def acquire(self, timeout: float | None = None) -> Generator[None, None, None]:
        if not self.try_acquire(timeout):
            raise BulkheadFullError("third-party", self._capacity, self.taken)
        try:
            yield
        finally:
            self.release()

    def try_acquire(self, timeout: float | None = None) -> bool:
        self.try_acquire_timeouts.append(timeout)
        if self.taken < self._capacity:
            self.taken += 1
            return True
        return False

    def release(self) -> None:
        self.release_calls += 1
        self.taken -= 1

    def get_state(self) -> BulkheadState:
        return BulkheadState(
            name="third-party",
            bulkhead_type=BulkheadType.SEMAPHORE,
            max_concurrent=self._capacity,
            active_count=self.taken,
            waiting_count=0,
            rejected_count=0,
        )


class TestBulkheadAsyncDefaultContract:
    """The ABC's non-abstract async entry points: immediate verdict, release on exit."""

    @pytest.mark.asyncio
    async def test_try_acquire_async_default_gives_immediate_verdict(self):
        """Any timeout becomes try_acquire(None): no waiting, same count."""
        bulkhead = _CountingBulkhead(capacity=1)

        first = await bulkhead.try_acquire_async(timeout=5.0)
        second = await bulkhead.try_acquire_async(timeout=5.0)

        assert (first, second) == (True, False)
        assert bulkhead.try_acquire_timeouts == [None, None]

    @pytest.mark.asyncio
    async def test_acquire_async_default_releases_when_body_raises(self):
        bulkhead = _CountingBulkhead(capacity=1)

        with pytest.raises(ValueError, match="body"):
            async with bulkhead.acquire_async(timeout=1.0):
                raise ValueError("body")

        assert bulkhead.release_calls == 1
        assert bulkhead.taken == 0

    @pytest.mark.asyncio
    async def test_acquire_async_default_full_raises_error_with_state_fields(self):
        bulkhead = _CountingBulkhead(capacity=1)
        assert await bulkhead.try_acquire_async() is True

        with pytest.raises(BulkheadFullError) as exc_info:
            async with bulkhead.acquire_async(timeout=1.0):
                pass

        assert exc_info.value.bulkhead_name == "third-party"
        assert exc_info.value.max_concurrent == 1
        assert exc_info.value.active_count == 1
        assert bulkhead.release_calls == 0


class TestBulkheadStateQueueSizeContract:
    """``queue_size`` is a trailing field defaulting to 0, carried by /bulkheads."""

    def test_queue_size_defaults_to_zero_after_existing_positional_fields(self):
        state = BulkheadState("db", BulkheadType.SEMAPHORE, 10, 2, 1, 0, None)

        assert state.queue_size == 0

    def test_semaphore_state_reports_queue_size_zero(self):
        assert SemaphoreBulkhead("db", max_concurrent=4).get_state().queue_size == 0

    def test_bulkheads_status_payload_carries_queue_size(self):
        state = BulkheadState(
            name="pool",
            bulkhead_type=BulkheadType.THREAD_POOL,
            max_concurrent=15,
            active_count=3,
            waiting_count=1,
            rejected_count=0,
            queue_size=10,
        )
        registry = _RegistryWithStates({"pool": state})

        with patch(
            "baldur.services.bulkhead.registry.get_bulkhead_registry",
            return_value=registry,
        ):
            response = bulkhead_status(RequestContext(method=HttpMethod.GET, path="/"))

        assert response.body["bulkheads"]["pool"]["queue_size"] == 10


class _RegistryWithStates:
    """The two reads ``bulkhead_status`` makes of a registry."""

    def __init__(self, states: dict[str, BulkheadState]) -> None:
        self._states = states

    def get_all_states(self) -> dict[str, BulkheadState]:
        return self._states


# =============================================================================
# Status surface counts async holders
# =============================================================================


class TestSemaphoreStatusSurfaceBehavior:
    """/bulkheads and the Prometheus gauges show seats async callers hold."""

    @pytest.mark.asyncio
    async def test_bulkheads_status_counts_async_holders(self):
        # Given — two seats held through the registry's async handle.
        registry = BulkheadRegistry()
        registry.get_or_create("orders", max_concurrent=3)
        handle = registry.get_async("orders")
        assert await handle.try_acquire() is True
        assert await handle.try_acquire() is True

        # When
        with patch(
            "baldur.services.bulkhead.registry.get_bulkhead_registry",
            return_value=registry,
        ):
            response = bulkhead_status(
                RequestContext(
                    method=HttpMethod.GET, path="/", query_params={"name": "orders"}
                )
            )

        # Then
        payload = response.body["bulkheads"]["orders"]
        assert payload["active_count"] == 2
        assert payload["available_permits"] == 1
        await handle.release()
        await handle.release()

    @pytest.mark.asyncio
    async def test_metrics_gauges_count_async_holders(self):
        # Given
        registry = BulkheadRegistry()
        compartment = registry.get_or_create("orders", max_concurrent=3)
        handle = registry.get_async("orders")
        assert await handle.try_acquire() is True
        updater = bulkhead_metrics.BulkheadMetricsUpdater(interval=60.0)

        # When
        with (
            patch(
                "baldur.services.bulkhead.registry.get_bulkhead_registry",
                return_value=registry,
            ),
            patch.object(
                bulkhead_metrics, "update_bulkhead_metrics", autospec=True
            ) as update,
        ):
            updater._update_all_metrics()

        # Then — the gauge update for "orders" carries the async holder.
        forwarded = {c.kwargs["bulkhead_name"]: c.kwargs for c in update.call_args_list}
        assert forwarded["orders"]["active_count"] == 1
        assert forwarded["orders"]["max_concurrent"] == 3
        assert compartment.get_state().active_count == 1
        await handle.release()


# =============================================================================
# The registry's async handle shares the registered compartment
# =============================================================================


class TestAsyncHandleSharedCompartmentBehavior:
    """get_async(name) is a view of the registered compartment, not a twin pool."""

    @pytest.mark.asyncio
    async def test_handle_seats_count_on_registered_compartment(self):
        # Given
        registry = BulkheadRegistry()
        compartment = registry.get_or_create("payments", max_concurrent=2)
        handle = registry.get_async("payments")

        # When — the handle takes one seat, a sync caller the other.
        assert await handle.try_acquire() is True
        assert compartment.try_acquire() is True

        # Then — the compartment is full for both kinds of caller.
        assert compartment.try_acquire() is False
        assert await handle.try_acquire() is False
        assert handle.get_state() == compartment.get_state()
        await handle.release()
        compartment.release()
        assert compartment.get_state().active_count == 0

    @pytest.mark.asyncio
    async def test_register_replaces_handle_and_old_seat_returns_to_old_compartment(
        self,
    ):
        # Given — a seat held through the handle of the first compartment.
        registry = BulkheadRegistry()
        first = registry.get_or_create("payments", max_concurrent=1)
        old_handle = registry.get_async("payments")
        assert await old_handle.try_acquire() is True

        # When — the name is registered again.
        second = SemaphoreBulkhead("payments", max_concurrent=1)
        registry.register(second)
        new_handle = registry.get_async("payments")

        # Then — the new handle admits on the new compartment; the old seat
        # goes back where it was taken.
        assert new_handle is not old_handle
        assert await new_handle.try_acquire() is True
        assert second.get_state().active_count == 1
        await old_handle.release()
        assert first.get_state().active_count == 0
        assert second.get_state().active_count == 1
        await new_handle.release()

    def test_unregister_drops_handle(self):
        registry = BulkheadRegistry()
        registry.get_or_create("payments", max_concurrent=1)
        registry.get_async("payments")

        assert registry.unregister("payments") is True

        with pytest.raises(KeyError):
            registry.get_async("payments")

    def test_builtin_handle_shares_builtin_compartment(self):
        registry = BulkheadRegistry()

        handle = registry.get_async(ConnectionType.DATABASE)

        assert handle._compartment is registry.get(ConnectionType.DATABASE)

    @pytest.mark.asyncio
    async def test_directly_constructed_handle_builds_its_own_compartment(self):
        handle = AsyncSemaphoreBulkhead("standalone", max_concurrent=2)

        assert await handle.try_acquire() is True
        state = handle.get_state()

        assert state.name == "standalone"
        assert state.bulkhead_type == BulkheadType.SEMAPHORE
        assert state.max_concurrent == 2
        assert state.active_count == 1
        await handle.release()
        assert handle.get_state().active_count == 0
