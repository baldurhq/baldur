"""
In-Memory Audit Buffer.

Memory fallback buffer for when the WAL fails.
Holds critical logs in memory during a disk outage and flushes them to file
once the system recovers.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from baldur.audit.resilience.buffer_protocol import AuditBufferProtocol

import structlog

from baldur.settings.resilient_recorder import get_resilient_recorder_settings
from baldur.utils.time import utc_now

logger = structlog.get_logger()


def _get_max_entries() -> int:
    """Get max entries from settings."""
    return get_resilient_recorder_settings().memory_buffer_max_entries


def _get_flush_interval() -> float:
    """Get flush interval from settings."""
    return get_resilient_recorder_settings().memory_buffer_flush_interval


class InMemoryAuditBuffer:
    """
    Memory fallback buffer for when the WAL fails.

    Holds critical logs in memory during a disk outage and flushes them to
    file once the system recovers.

    Design principles:
    - Max entry count comes from ResilientRecorderSettings (default 10,000)
    - FIFO: the oldest entry is dropped once capacity is exceeded
    - Periodic flush attempts (default every 30 seconds)

    Thread-safe: uses RLock
    """

    _instance: InMemoryAuditBuffer | None = None
    _lock = threading.Lock()

    # Legacy constants for backward compatibility
    MAX_ENTRIES = 10_000
    FLUSH_INTERVAL_SECONDS = 30.0

    def __init__(
        self,
        max_entries: int | None = None,
        flush_interval_seconds: float | None = None,
    ):
        """
        Initialize InMemoryAuditBuffer.

        Args:
            max_entries: Max entry count (default from ResilientRecorderSettings)
            flush_interval_seconds: Flush interval (default from
                ResilientRecorderSettings)
        """
        self._buffer: list[dict[str, Any]] = []
        self._buffer_lock = threading.RLock()
        self._last_flush_attempt: datetime | None = None
        self._flush_failures: int = 0
        self._total_dropped: int = 0
        self._total_buffered: int = 0
        self._max_entries = (
            max_entries if max_entries is not None else _get_max_entries()
        )
        self._flush_interval_seconds = (
            flush_interval_seconds
            if flush_interval_seconds is not None
            else _get_flush_interval()
        )

    @classmethod
    def get_instance(cls) -> InMemoryAuditBuffer:
        """Return the singleton instance."""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        """Reset the instance (for testing)."""
        with cls._lock:
            cls._instance = None

    def add(self, entry: dict[str, Any]) -> bool:
        """
        Add an entry.

        Args:
            entry: WAL entry dictionary

        Returns:
            True if added successfully, False if buffer full and oldest removed
        """
        with self._buffer_lock:
            dropped = False
            if len(self._buffer) >= self._max_entries:
                # FIFO: drop the oldest entry
                self._buffer.pop(0)
                self._total_dropped += 1
                dropped = True
                logger.warning(
                    "[InMemoryAuditBuffer] Buffer full, dropped oldest entry "  # noqa: G004
                    f"(total dropped: {self._total_dropped})"
                )

            entry["buffered_at"] = utc_now().isoformat()
            self._buffer.append(entry)
            self._total_buffered += 1

            return not dropped

    def try_flush(self, wal_write_func: Callable[[dict[str, Any]], int | None]) -> int:
        """
        Try to flush the buffer to the WAL.

        Args:
            wal_write_func: WAL write function (entry dict -> returns sequence)

        Returns:
            Number of flushed entries
        """
        with self._buffer_lock:
            if not self._buffer:
                return 0

            self._last_flush_attempt = utc_now()
            flushed = 0
            remaining = []

            for entry in self._buffer:
                try:
                    # Strip buffered_at before writing to the WAL
                    entry_copy = {k: v for k, v in entry.items() if k != "buffered_at"}
                    result = wal_write_func(entry_copy)
                    if result is not None:
                        flushed += 1
                    else:
                        remaining.append(entry)
                except Exception as e:
                    logger.debug(
                        "in_memory_audit_buffer.flush_entry_failed",
                        error=e,
                    )
                    remaining.append(entry)

            self._buffer = remaining

            if flushed > 0:
                logger.info(
                    "in_memory_audit_buffer.flushed_entries_wal",
                    flushed=flushed,
                )

            if remaining:
                self._flush_failures += 1

            return flushed

    def count(self) -> int:
        """Current buffer size (AuditBufferProtocol)."""
        with self._buffer_lock:
            return len(self._buffer)

    def get_buffer_size(self) -> int:
        """Current buffer size (legacy alias)."""
        return self.count()

    def get_stats(self) -> dict[str, Any]:
        """Buffer statistics."""
        with self._buffer_lock:
            current = len(self._buffer)
            capacity = self._max_entries
            return {
                # Common keys (AuditBufferProtocol)
                "count": current,
                "total_added": self._total_buffered,
                "total_dropped": self._total_dropped,
                "capacity": capacity,
                "usage_percent": (current / capacity * 100) if capacity else None,
                # Implementation-specific keys
                "buffered_entries": current,
                "max_entries": capacity,
                "flush_interval_seconds": self._flush_interval_seconds,
                "total_buffered": self._total_buffered,
                "flush_failures": self._flush_failures,
                "last_flush_attempt": (
                    self._last_flush_attempt.isoformat()
                    if self._last_flush_attempt
                    else None
                ),
            }

    def clear(self) -> int:
        """Empty the buffer (for testing). Returns the number of removed entries."""
        with self._buffer_lock:
            count = len(self._buffer)
            self._buffer.clear()
            return count


def get_inmemory_audit_buffer() -> InMemoryAuditBuffer:
    """Get the in-memory audit buffer instance."""
    return InMemoryAuditBuffer.get_instance()


def get_audit_buffer() -> AuditBufferProtocol:
    """
    Audit Buffer factory.

    Implementation selected by environment variable:
    - BALDUR_BUFFER_TYPE=memory (default, the existing volatile buffer)
    - BALDUR_BUFFER_TYPE=disk (persistent buffer, survives pod restarts)

    Returns:
        InMemoryAuditBuffer or DiskBufferAdapter instance

    Note:
        DiskBufferAdapter offers the same interface as InMemoryAuditBuffer.
        - add(entry): add an entry
        - try_flush(callback): flush to the WAL
        - get_stats(): read statistics
    """
    import os

    buffer_type = os.environ.get("BALDUR_BUFFER_TYPE", "memory")

    if buffer_type == "disk":
        # Direct import to the concrete module — disk_buffer re-exports
        # DiskBufferAdapter via lazy __getattr__ which mypy sees as plain `type`,
        # losing the `get_instance` classmethod.
        from baldur.audit.persistence.disk_buffer_adapter import DiskBufferAdapter

        return DiskBufferAdapter.get_instance()
    return InMemoryAuditBuffer.get_instance()


__all__ = ["InMemoryAuditBuffer", "get_inmemory_audit_buffer", "get_audit_buffer"]
