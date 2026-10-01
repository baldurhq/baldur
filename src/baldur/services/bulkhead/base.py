"""
Bulkhead Base - resource isolation abstract interface.

Defines the base interface for the Bulkhead pattern.
A pattern derived from ship design that isolates a failure in one compartment so it does not propagate to other compartments.

Supported types:
- SEMAPHORE: semaphore-based concurrent execution limit (suitable for I/O-bound work)
- THREAD_POOL: thread-pool-based isolation (suitable for CPU-bound work)
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncGenerator, Callable, Generator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from functools import wraps
from typing import Any, TypeVar

from baldur.services.bulkhead.exceptions import BulkheadFullError

T = TypeVar("T")

__all__ = ["Bulkhead", "BulkheadState", "BulkheadType"]


class BulkheadType(str, Enum):
    """Bulkhead type."""

    SEMAPHORE = "semaphore"
    """Semaphore-based - limits only the concurrent execution count"""

    THREAD_POOL = "thread_pool"
    """Thread-pool-based - isolated execution in a dedicated thread pool"""


@dataclass
class BulkheadState:
    """Bulkhead current state data."""

    name: str
    """Bulkhead name (domain identifier)"""

    bulkhead_type: BulkheadType
    """Bulkhead type"""

    max_concurrent: int
    """Capacity — the number of seats the compartment admits at once. For a
    thread-pool compartment this is its worker count plus its queue size."""

    active_count: int
    """Seats taken — by sync and async callers alike. For a thread-pool
    compartment this counts running, queued and held calls."""

    waiting_count: int
    """Callers waiting — for a seat (semaphore compartment) or, for a
    thread-pool compartment, admitted tasks no worker has started yet"""

    rejected_count: int
    """Total number of rejected requests"""

    last_rejection_time: datetime | None = None
    """Last rejection time"""

    queue_size: int = 0
    """Waiting-queue seats included in ``max_concurrent`` (thread-pool
    compartments; 0 for a semaphore compartment)"""

    @property
    def available_permits(self) -> int:
        """Number of available permits."""
        return max(0, self.max_concurrent - self.active_count)

    @property
    def utilization_percent(self) -> float:
        """Utilization (0-100%)."""
        if self.max_concurrent == 0:
            return 0.0
        return (self.active_count / self.max_concurrent) * 100


class Bulkhead(ABC):
    """
    Bulkhead abstract interface.

    Prevents a failure in one component from propagating to other components through resource isolation.

    Usage (context manager):
        bulkhead = SemaphoreBulkhead("database", max_concurrent=10)

        with bulkhead.acquire(timeout=5.0):
            do_database_work()

    Usage (decorator):
        @bulkhead.wrap
        def do_work():
            pass
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Bulkhead name (domain identifier)."""
        pass

    @abstractmethod
    @contextmanager
    def acquire(self, timeout: float | None = None) -> Generator[None, None, None]:
        """
        Acquire the resource (context manager).

        Args:
            timeout: Wait timeout (seconds). If None, fail immediately (non-blocking).

        Yields:
            None

        Raises:
            BulkheadFullError: When resource acquisition fails
        """
        pass

    @abstractmethod
    def try_acquire(self, timeout: float | None = None) -> bool:
        """
        Attempt to acquire the resource.

        Args:
            timeout: Maximum time (seconds) the implementation may wait for
                capacity — an upper bound on waiting, not a guarantee of it.
                None means no waiting (immediate verdict). Implementations whose
                capacity model already buffers bursts (bounded-queue thread
                pools) may return an immediate verdict for any timeout value.

        Returns:
            True if acquisition succeeded, False if it failed
        """
        pass

    @abstractmethod
    def release(self) -> None:
        """Release the resource."""
        pass

    async def try_acquire_async(self, timeout: float | None = None) -> bool:  # noqa: ARG002 - immediate verdict; see docstring
        """
        Attempt to acquire a seat from a coroutine.

        Takes a seat on the same count as :meth:`try_acquire`, so sync and
        async callers of one compartment share its capacity. This default
        gives the immediate verdict (``self.try_acquire(None)``) for any
        ``timeout`` — it never waits and never touches the event loop, which
        satisfies the contract (``timeout`` is an upper bound on waiting).
        Implementations that can wait without blocking the loop override it.

        Non-abstract so existing third-party subclasses stay instantiable.

        Args:
            timeout: Upper bound on waiting (seconds). None means no waiting.

        Returns:
            True if a seat was taken (release it with :meth:`release`)
        """
        return self.try_acquire(None)

    @asynccontextmanager
    async def acquire_async(
        self, timeout: float | None = None
    ) -> AsyncGenerator[None, None]:
        """
        Hold a seat for the body of an ``async with`` block.

        Built on :meth:`try_acquire_async` and :meth:`release`.

        Args:
            timeout: Upper bound on waiting (seconds). None means no waiting.

        Yields:
            None

        Raises:
            BulkheadFullError: When no seat is available within ``timeout``
        """
        if not await self.try_acquire_async(timeout):
            state = self.get_state()
            raise BulkheadFullError(
                bulkhead_name=self.name,
                max_concurrent=state.max_concurrent,
                active_count=state.active_count,
            )
        try:
            yield
        finally:
            self.release()

    @abstractmethod
    def get_state(self) -> BulkheadState:
        """Return the current state."""
        pass

    def execute(
        self,
        fn: Callable[..., T],
        *args: Any,
        timeout: float | None = None,
        **kwargs: Any,
    ) -> T:
        """
        Execute a function inside the bulkhead compartment.

        Default implementation: acquire a permit (bounded by ``timeout``),
        run ``fn`` in the calling thread, release on return. ``timeout``
        bounds the **admission wait only** — once admitted, ``fn`` occupies
        the caller until it returns. Implementations that offload execution
        (dedicated worker pools) override this to also bound execution time.

        Non-abstract so existing third-party subclasses stay instantiable.

        Args:
            fn: Function to execute
            *args: Positional arguments
            timeout: Admission-wait timeout (seconds). If None, fail
                immediately when no permit is available (non-blocking).
            **kwargs: Keyword arguments

        Returns:
            Function execution result

        Raises:
            BulkheadFullError: When no permit is available within ``timeout``
        """
        with self.acquire(timeout=timeout):
            return fn(*args, **kwargs)

    def wrap(self, fn: Callable[..., T]) -> Callable[..., T]:
        """
        Decorator that wraps a function with the bulkhead.

        Args:
            fn: Function to wrap

        Returns:
            Function with the bulkhead applied
        """

        @wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> T:
            with self.acquire():
                return fn(*args, **kwargs)

        return wrapper
