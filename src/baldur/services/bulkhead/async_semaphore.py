"""
Async Semaphore Bulkhead - asyncio.Semaphore-based async bulkhead.

Suited to I/O-bound async work.
Unlike threading.Semaphore, it does not block the event loop.

Usage:
    bulkhead = AsyncSemaphoreBulkhead("database", max_concurrent=10)

    async with bulkhead.acquire(timeout=1.0):
        await async_db_operation()
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import datetime

import structlog

from baldur.services.bulkhead.base import (
    BulkheadState,
    BulkheadType,
)
from baldur.services.bulkhead.exceptions import BulkheadFullError
from baldur.services.bulkhead.metrics import increment_rejected_count
from baldur.utils.time import utc_now

logger = structlog.get_logger()


class AsyncSemaphoreBulkhead:
    """
    Async semaphore-based bulkhead.

    Limits concurrency in an asyncio environment to prevent resource exhaustion.
    Does not block the event loop; suited to async I/O work.

    Features:
    - asyncio.Semaphore-based non-blocking wait
    - asyncio.wait_for-based timeout
    - Rejection statistics tracking
    """

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
        self._name = name
        self._max_concurrent = max_concurrent
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._lock = asyncio.Lock()

        # Statistics
        self._active_count = 0
        self._waiting_count = 0
        self._rejected_count = 0
        self._last_rejection_time: datetime | None = None

    @property
    def name(self) -> str:
        """Bulkhead name."""
        return self._name

    @asynccontextmanager
    async def acquire(self, timeout: float | None = None) -> AsyncGenerator[None, None]:
        """
        Acquire a resource asynchronously.

        Args:
            timeout: Wait timeout (seconds). None fails immediately (non-blocking).

        Yields:
            None

        Raises:
            BulkheadFullError: When resource acquisition fails
        """
        acquired = False
        try:
            async with self._lock:
                self._waiting_count += 1

            # timeout=None tries immediately (non-blocking)
            if timeout is None:
                # locked() True means the semaphore is at 0, so fail immediately
                if self._semaphore.locked():
                    acquired = False
                else:
                    # Try to acquire immediately
                    try:
                        await asyncio.wait_for(
                            self._semaphore.acquire(),
                            timeout=0.001,  # near-instant
                        )
                        acquired = True
                    except TimeoutError:
                        acquired = False
            else:
                try:
                    await asyncio.wait_for(
                        self._semaphore.acquire(),
                        timeout=timeout,
                    )
                    acquired = True
                except TimeoutError:
                    acquired = False

            async with self._lock:
                self._waiting_count -= 1
                if acquired:
                    self._active_count += 1
                else:
                    self._rejected_count += 1
                    self._last_rejection_time = utc_now()

            if not acquired:
                # Emit outside the lock — the prometheus client takes its own
                # lock, so recording under self._lock would nest two locks.
                increment_rejected_count(self._name)
                raise BulkheadFullError(
                    bulkhead_name=self._name,
                    max_concurrent=self._max_concurrent,
                    active_count=self._active_count,
                )

            yield

        finally:
            if acquired:
                self._semaphore.release()
                async with self._lock:
                    self._active_count -= 1

    async def try_acquire(self, timeout: float | None = None) -> bool:
        """
        Acquisition attempt, mirroring this class's ``acquire`` timeout contract.

        Args:
            timeout: Maximum time (seconds) to wait for capacity — an upper
                bound on waiting, not a guarantee of it. None means no waiting
                (immediate verdict).

        Returns:
            True on success, False on failure
        """
        # timeout=None tries immediately (non-blocking)
        if timeout is None:
            # locked() True means the semaphore is at 0, so fail immediately
            if self._semaphore.locked():
                acquired = False
            else:
                try:
                    await asyncio.wait_for(
                        self._semaphore.acquire(),
                        timeout=0.001,  # near-instant
                    )
                    acquired = True
                except TimeoutError:
                    acquired = False
        else:
            try:
                await asyncio.wait_for(
                    self._semaphore.acquire(),
                    timeout=timeout,
                )
                acquired = True
            except TimeoutError:
                acquired = False

        async with self._lock:
            if acquired:
                self._active_count += 1
            else:
                self._rejected_count += 1
                self._last_rejection_time = utc_now()
        if not acquired:
            # Emit outside the lock (see acquire()).
            increment_rejected_count(self._name)
        return acquired

    async def release(self) -> None:
        """Release the resource."""
        self._semaphore.release()
        async with self._lock:
            self._active_count = max(0, self._active_count - 1)

    def get_state(self) -> BulkheadState:
        """Return the current state (synchronous method)."""
        return BulkheadState(
            name=self._name,
            bulkhead_type=BulkheadType.SEMAPHORE,
            max_concurrent=self._max_concurrent,
            active_count=self._active_count,
            waiting_count=self._waiting_count,
            rejected_count=self._rejected_count,
            last_rejection_time=self._last_rejection_time,
        )
