"""
Semaphore Bulkhead - semaphore-based concurrent execution limit.

A synchronous bulkhead implementation suitable for I/O-bound work.
Uses threading.BoundedSemaphore to limit the concurrent execution count.

Usage:
    bulkhead = SemaphoreBulkhead("database", max_concurrent=10)

    # Fail immediately without a timeout (non-blocking)
    with bulkhead.acquire():
        do_database_work()

    # Fail after waiting at most 1 second
    with bulkhead.acquire(timeout=1.0):
        do_database_work()
"""

from __future__ import annotations

import threading
from collections.abc import Generator
from contextlib import contextmanager
from datetime import datetime

import structlog

from baldur.services.bulkhead.base import (
    Bulkhead,
    BulkheadState,
    BulkheadType,
)
from baldur.services.bulkhead.exceptions import BulkheadFullError
from baldur.services.bulkhead.metrics import increment_rejected_count
from baldur.utils.time import utc_now

logger = structlog.get_logger()


class SemaphoreBulkhead(Bulkhead):
    """
    Semaphore-based bulkhead.

    Limits the concurrent execution count to prevent resource exhaustion.
    Suitable for I/O-bound work (DB queries, cache lookups, etc.).

    Features:
    - Maximum concurrent execution count limit
    - Timeout-based wait support
    - Rejection statistics tracking
    """

    def __init__(
        self,
        name: str,
        max_concurrent: int = 10,
        fair: bool = True,  # noqa: ARG002 - for future fair scheduling implementation
    ):
        """
        Args:
            name: Bulkhead name (domain identifier)
            max_concurrent: Maximum concurrent execution count
            fair: If True, guarantees FIFO ordering (future implementation)
        """
        self._name = name
        self._max_concurrent = max_concurrent
        self._semaphore = threading.BoundedSemaphore(max_concurrent)
        self._lock = threading.Lock()

        # Statistics
        self._active_count = 0
        self._waiting_count = 0
        self._rejected_count = 0
        self._last_rejection_time: datetime | None = None

    @property
    def name(self) -> str:
        """Bulkhead name."""
        return self._name

    @contextmanager
    def acquire(self, timeout: float | None = None) -> Generator[None, None, None]:
        """
        Acquire the resource.

        Args:
            timeout: Wait timeout (seconds). If None, fail immediately (non-blocking).

        Yields:
            None

        Raises:
            BulkheadFullError: When resource acquisition fails
        """
        acquired = False
        try:
            with self._lock:
                self._waiting_count += 1

            # If timeout=None, attempt immediately with blocking=False
            if timeout is None:
                acquired = self._semaphore.acquire(blocking=False)
            else:
                acquired = self._semaphore.acquire(blocking=True, timeout=timeout)

            with self._lock:
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
                with self._lock:
                    self._active_count -= 1

    def try_acquire(self, timeout: float | None = None) -> bool:
        """
        Attempt to acquire. Non-blocking if timeout is None, otherwise waits up to the given time.

        Args:
            timeout: Wait timeout (seconds). If None, succeed/fail immediately.

        Returns:
            True if acquisition succeeded, False if it failed
        """
        if timeout is None:
            acquired = self._semaphore.acquire(blocking=False)
        else:
            acquired = self._semaphore.acquire(blocking=True, timeout=timeout)
        with self._lock:
            if acquired:
                self._active_count += 1
            else:
                self._rejected_count += 1
                self._last_rejection_time = utc_now()
        if not acquired:
            # Emit outside the lock (see acquire()).
            increment_rejected_count(self._name)
        return acquired

    def release(self) -> None:
        """Release the resource."""
        self._semaphore.release()
        with self._lock:
            self._active_count = max(0, self._active_count - 1)

    def get_state(self) -> BulkheadState:
        """Return the current state."""
        with self._lock:
            return BulkheadState(
                name=self._name,
                bulkhead_type=BulkheadType.SEMAPHORE,
                max_concurrent=self._max_concurrent,
                active_count=self._active_count,
                waiting_count=self._waiting_count,
                rejected_count=self._rejected_count,
                last_rejection_time=self._last_rejection_time,
            )
