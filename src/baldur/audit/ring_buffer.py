"""
Ring Buffer with Backpressure for Shadow Logging.

DROP_OLDEST is the default, following the non-intrusive principle.
Does not affect main application performance.

Usage:
    buffer = RingBuffer[AuditEntry](capacity=10000)
    buffer.put(entry)  # Non-blocking
    batch = buffer.get_batch(max_size=100)  # Background worker
"""

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from threading import Lock
from typing import Generic, TypeVar

import structlog

from baldur.settings.backpressure import BackpressureStrategy

logger = structlog.get_logger()

T = TypeVar("T")


@dataclass
class RingBufferStats:
    """Buffer statistics."""

    capacity: int
    size: int
    total_enqueued: int
    total_dropped: int
    drop_rate: float


class RingBuffer(Generic[T]):
    """
    Thread-Safe Ring Buffer with Backpressure.

    Non-intrusive buffer for Shadow Logging.
    Never blocks the main application.

    Features:
    - Non-blocking put() with DROP_OLDEST strategy
    - Batch retrieval for background workers
    - Thread-safe operations
    - Statistics for monitoring
    - Drop rate alert callback (operational visibility)
    - High capacity warning (memory protection)

    Usage:
        buffer = RingBuffer[AuditEntry](capacity=10000)

        # Producer (main thread, non-blocking)
        buffer.put(entry)

        # Consumer (background thread)
        batch = buffer.get_batch(max_size=100)
        for entry in batch:
            await store.save(entry)
    """

    # Memory warning threshold (100k and above)
    CAPACITY_WARNING_THRESHOLD = 100000
    # Estimated event size (1KB)
    ESTIMATED_EVENT_SIZE_BYTES = 1024
    # Minimum samples before a drop-rate alert
    MIN_SAMPLES_FOR_ALERT = 100

    def __init__(
        self,
        capacity: int = 10000,
        strategy: BackpressureStrategy = BackpressureStrategy.DROP_OLDEST,
        on_drop_threshold: Callable[["RingBufferStats"], None] | None = None,
        drop_rate_threshold: float = 0.01,
    ):
        """
        Initialize RingBuffer.

        Args:
            capacity: Maximum buffer size
            strategy: Backpressure strategy (DROP_OLDEST recommended)
            on_drop_threshold: Callback invoked when the drop rate exceeds
                the threshold
            drop_rate_threshold: Drop-rate alert threshold (default 1%)
        """
        if capacity < 1:
            raise ValueError("capacity must be at least 1")

        # High-capacity memory warning
        if capacity > self.CAPACITY_WARNING_THRESHOLD:
            estimated_mb = (capacity * self.ESTIMATED_EVENT_SIZE_BYTES) / (1024 * 1024)
            logger.warning(
                "ring_buffer.high_use_mb_ram",
                capacity=capacity,
                estimated_mb=estimated_mb,
            )

        self._capacity = capacity
        self._strategy = strategy
        self._buffer: deque = deque(maxlen=capacity)
        self._lock = Lock()
        self._total_enqueued = 0
        self._total_dropped = 0

        # Drop-rate alert settings
        self._on_drop_threshold = on_drop_threshold
        self._drop_rate_threshold = drop_rate_threshold
        self._alert_sent = False

    @classmethod
    def from_settings(cls, settings=None, **overrides) -> "RingBuffer[T]":
        """
        Create an instance from Settings.

        Args:
            settings: RingBufferSettings instance (auto-loaded if None)
            **overrides: Per-field overrides

        Returns:
            RingBuffer: Settings-based instance
        """
        from baldur.settings.ring_buffer import get_ring_buffer_settings

        s = settings or get_ring_buffer_settings()
        strategy_map = {
            "drop_oldest": BackpressureStrategy.DROP_OLDEST,
            "drop_newest": BackpressureStrategy.DROP_NEWEST,
        }
        return cls(
            capacity=overrides.get("capacity", s.capacity),
            strategy=overrides.get(
                "strategy",
                strategy_map.get(s.strategy, BackpressureStrategy.DROP_OLDEST),
            ),
            on_drop_threshold=overrides.get("on_drop_threshold"),
            drop_rate_threshold=overrides.get("drop_rate_threshold", 0.01),
        )

    @property
    def capacity(self) -> int:
        """Get buffer capacity."""
        return self._capacity

    @property
    def size(self) -> int:
        """Get current buffer size."""
        with self._lock:
            return len(self._buffer)

    @property
    def is_empty(self) -> bool:
        """Check if buffer is empty."""
        with self._lock:
            return len(self._buffer) == 0

    @property
    def is_full(self) -> bool:
        """Check if buffer is at capacity."""
        with self._lock:
            return len(self._buffer) >= self._capacity

    def put(self, item: T) -> bool:
        """
        Add item to buffer. Non-blocking.

        Args:
            item: Item to add

        Returns:
            True if added, False if dropped (DROP_NEWEST only)
        """
        with self._lock:
            self._total_enqueued += 1

            if len(self._buffer) >= self._capacity:
                if self._strategy == BackpressureStrategy.DROP_OLDEST:
                    # deque with maxlen automatically drops oldest
                    self._total_dropped += 1
                    self._buffer.append(item)
                    self._check_drop_rate_alert()
                    return True
                # DROP_NEWEST: reject new item
                self._total_dropped += 1
                self._check_drop_rate_alert()
                return False

            self._buffer.append(item)
            return True

    def _check_drop_rate_alert(self) -> None:
        """Invoke the alert callback when the drop rate exceeds the threshold."""
        if self._on_drop_threshold is None or self._alert_sent:
            return

        if self._total_enqueued < self.MIN_SAMPLES_FOR_ALERT:
            return

        drop_rate = self._total_dropped / self._total_enqueued
        if drop_rate > self._drop_rate_threshold:
            self._alert_sent = True
            stats = RingBufferStats(
                capacity=self._capacity,
                size=len(self._buffer),
                total_enqueued=self._total_enqueued,
                total_dropped=self._total_dropped,
                drop_rate=drop_rate,
            )
            try:
                self._on_drop_threshold(stats)
            except Exception:
                pass  # An alert failure must never disrupt the main logic

    def reset_alert(self) -> None:
        """Reset the alert state (for periodic invocation)."""
        with self._lock:
            self._alert_sent = False

    def put_many(self, items: list[T]) -> int:
        """
        Add multiple items to buffer.

        Args:
            items: Items to add

        Returns:
            Number of items actually added
        """
        added = 0
        for item in items:
            if self.put(item):
                added += 1
        return added

    def get(self) -> T | None:
        """
        Get and remove single item from buffer.

        Returns:
            Item or None if empty
        """
        with self._lock:
            if self._buffer:
                return self._buffer.popleft()
            return None

    def get_batch(self, max_size: int | None = None) -> list[T]:
        """
        Get and remove batch of items.

        Args:
            max_size: Maximum batch size (loaded from Settings if None)

        Returns:
            List of items (may be smaller than max_size)
        """
        if max_size is None:
            from baldur.settings.ring_buffer import get_ring_buffer_settings

            max_size = get_ring_buffer_settings().batch_max_size

        with self._lock:
            batch = []
            count = min(max_size, len(self._buffer))
            for _ in range(count):
                if self._buffer:
                    batch.append(self._buffer.popleft())
            return batch

    def peek(self) -> T | None:
        """
        Peek at next item without removing.

        Returns:
            Item or None if empty
        """
        with self._lock:
            if self._buffer:
                return self._buffer[0]
            return None

    def peek_batch(self, max_size: int = 100) -> list[T]:
        """
        Peek at multiple items without removing.

        Args:
            max_size: Maximum items to peek

        Returns:
            List of items
        """
        with self._lock:
            count = min(max_size, len(self._buffer))
            return list(self._buffer)[:count]

    def get_all(self) -> list[T]:
        """
        Return all items (non-destructive).

        Returns every item in the buffer as a list.
        Does not remove any item.

        Returns:
            A copied list of every item in the buffer
        """
        with self._lock:
            return list(self._buffer)

    def clear(self) -> int:
        """
        Clear all items from buffer.

        Returns:
            Number of items cleared
        """
        with self._lock:
            count = len(self._buffer)
            self._buffer.clear()
            return count

    def get_stats(self) -> RingBufferStats:
        """
        Get buffer statistics.

        Returns:
            RingBufferStats with current metrics
        """
        with self._lock:
            size = len(self._buffer)
            drop_rate = (
                self._total_dropped / self._total_enqueued
                if self._total_enqueued > 0
                else 0.0
            )
            return RingBufferStats(
                capacity=self._capacity,
                size=size,
                total_enqueued=self._total_enqueued,
                total_dropped=self._total_dropped,
                drop_rate=drop_rate,
            )

    def reset_stats(self) -> None:
        """Reset statistics counters."""
        with self._lock:
            self._total_enqueued = 0
            self._total_dropped = 0

    def __len__(self) -> int:
        """Get current size."""
        return self.size

    def __repr__(self) -> str:
        """String representation."""
        stats = self.get_stats()
        return (
            f"RingBuffer(capacity={stats.capacity}, size={stats.size}, "
            f"dropped={stats.total_dropped}, drop_rate={stats.drop_rate:.2%})"
        )
