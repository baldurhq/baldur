"""InMemoryAuditBuffer-compatible adapter for DiskPersistentBuffer.

Provides the same interface as InMemoryAuditBuffer so that existing
code can migrate to disk-backed persistence transparently.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

from baldur.audit.persistence.disk_buffer_models import BufferEntry

__all__ = [
    "DiskBufferAdapter",
]


class DiskBufferAdapter:
    """InMemoryAuditBuffer-compatible adapter.

    Wraps :class:`DiskPersistentBuffer` to expose the legacy
    ``add`` / ``try_flush`` / ``get_stats`` interface.

    Usage::

        # Legacy code
        buffer = InMemoryAuditBuffer.get_instance()
        buffer.add(entry)

        # New code (same interface)
        buffer = DiskBufferAdapter.get_instance()
        buffer.add(entry)
    """

    _instance: DiskBufferAdapter | None = None
    _lock = threading.Lock()

    def __init__(self, disk_buffer: Any | None = None) -> None:
        """Initialise the adapter.

        Args:
            disk_buffer: Optional :class:`DiskPersistentBuffer` instance.
                When ``None``, the module-level singleton is used.
        """
        if disk_buffer is not None:
            self._disk_buffer = disk_buffer
        else:
            from baldur.audit.persistence.disk_buffer import get_disk_buffer

            self._disk_buffer = get_disk_buffer()
        self._total_buffered = 0
        self._total_dropped = 0

    @classmethod
    def get_instance(cls) -> DiskBufferAdapter:
        """Return the singleton instance."""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        """Reset the singleton (for testing)."""
        with cls._lock:
            cls._instance = None

    def add(self, entry: dict[str, Any]) -> bool:
        """Add an entry (InMemoryAuditBuffer compatible).

        Args:
            entry: Event data.

        Returns:
            ``True`` if buffered successfully, ``False`` if dropped.
        """
        result = self._disk_buffer.put(entry)
        if result is not None:
            self._total_buffered += 1
            return True
        self._total_dropped += 1
        return False

    def try_flush(
        self,
        wal_write_func: Callable[[dict[str, Any]], int | None],
    ) -> int:
        """Flush via WAL write function (InMemoryAuditBuffer compatible).

        Args:
            wal_write_func: WAL write function.

        Returns:
            Number of flushed entries.
        """

        def handler(entries: list[BufferEntry]) -> bool:
            for entry in entries:
                result = wal_write_func(entry.data)
                if result is None:
                    return False
            return True

        return self._disk_buffer.flush_to(handler)

    def get_stats(self) -> dict[str, Any]:
        """Return adapter statistics."""
        stats = self._disk_buffer.get_stats()
        current = self.count()
        return {
            # Common keys (AuditBufferProtocol)
            "count": current,
            "total_added": self._total_buffered,
            "total_dropped": self._total_dropped,
            "capacity": None,
            "usage_percent": None,
            # Implementation-specific keys
            **stats,
            "total_buffered": self._total_buffered,
        }

    def count(self) -> int:
        """Return current entry count (AuditBufferProtocol)."""
        return self._disk_buffer.count()

    def clear(self) -> int:
        """Logical reset (ClearableBuffer). Physical deletion via retention policy only."""
        keys = [e.key for e in self._disk_buffer.iter_entries()]
        if not keys:
            return 0
        return self._disk_buffer.delete_batch(keys)

    def __len__(self) -> int:
        """Return current entry count."""
        return self.count()
