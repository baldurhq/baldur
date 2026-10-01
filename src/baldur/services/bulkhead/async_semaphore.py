"""
Async Semaphore Bulkhead - the async view of one bulkhead compartment.

An ``AsyncSemaphoreBulkhead`` takes its seats on the same count as the
compartment's sync callers, so sync and async callers of one compartment
together never exceed its capacity. Waiting never blocks the event loop and is
not bound to any one loop.

Usage:
    bulkhead = AsyncSemaphoreBulkhead("database", max_concurrent=10)

    async with bulkhead.acquire(timeout=1.0):
        await async_db_operation()

    # The registry's async view of a registered compartment shares its seats:
    async_bulkhead = get_bulkhead_registry().get_async("database")
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import structlog

from baldur.services.bulkhead.base import Bulkhead, BulkheadState
from baldur.services.bulkhead.semaphore import SemaphoreBulkhead

logger = structlog.get_logger()

__all__ = ["AsyncSemaphoreBulkhead"]


class AsyncSemaphoreBulkhead:
    """
    Async handle over one bulkhead compartment.

    Constructed directly, it builds its own :class:`SemaphoreBulkhead`. The
    registry's ``get_async(name)`` returns a handle over the registered
    compartment instead, so the async callers it admits count against that
    compartment's capacity and appear in its state.

    Features:
    - Seats shared with the compartment's sync callers
    - Timeout-bounded waiting on the running loop (any loop)
    - Rejection statistics tracked by the compartment
    """

    _compartment: Bulkhead

    def __init__(
        self,
        name: str,
        max_concurrent: int = 10,
    ):
        """
        Args:
            name: Bulkhead name (domain identifier)
            max_concurrent: Maximum concurrent executions
        """
        self._compartment = SemaphoreBulkhead(name=name, max_concurrent=max_concurrent)

    @classmethod
    def _over(cls, compartment: Bulkhead) -> AsyncSemaphoreBulkhead:
        """Build a handle over an existing compartment (registry path)."""
        handle = cls.__new__(cls)
        handle._compartment = compartment
        return handle

    @property
    def name(self) -> str:
        """Bulkhead name."""
        return self._compartment.name

    @asynccontextmanager
    async def acquire(self, timeout: float | None = None) -> AsyncGenerator[None, None]:
        """
        Hold a seat for the body of an ``async with`` block.

        Args:
            timeout: Upper bound on waiting (seconds). None fails immediately
                (non-blocking). A thread-pool compartment always gives the
                immediate verdict.

        Yields:
            None

        Raises:
            BulkheadFullError: When resource acquisition fails
        """
        async with self._compartment.acquire_async(timeout=timeout):
            yield

    async def try_acquire(self, timeout: float | None = None) -> bool:
        """
        Acquisition attempt, mirroring this class's ``acquire`` timeout contract.

        Args:
            timeout: Maximum time (seconds) to wait for capacity — an upper
                bound on waiting, not a guarantee of it. None means no waiting
                (immediate verdict).

        Returns:
            True on success (release it with :meth:`release`), False on failure
        """
        return await self._compartment.try_acquire_async(timeout)

    async def release(self) -> None:
        """Release the resource."""
        self._compartment.release()

    def get_state(self) -> BulkheadState:
        """Return the compartment's state (sync and async callers alike)."""
        return self._compartment.get_state()
