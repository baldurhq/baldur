"""In-memory implementation for CascadeEventArchiveRepository.

Thread-safe. Suitable for testing and non-Django environments.
"""

from __future__ import annotations

import threading
from datetime import datetime

from baldur.interfaces.repositories import CascadeEventArchiveRepository
from baldur.models.cascade_event import CascadeEventData

__all__ = ["InMemoryCascadeEventArchiveRepository"]


class InMemoryCascadeEventArchiveRepository(CascadeEventArchiveRepository):
    """In-memory implementation for CascadeEventArchiveRepository.

    Thread-safe with RLock. Suitable for testing and non-Django environments.
    """

    def __init__(self) -> None:
        self._storage: dict[str, CascadeEventData] = {}  # cascade_id -> data
        self._lock = threading.RLock()

    def save(self, data: CascadeEventData) -> bool:
        """Persist a cascade event record (overwrite on duplicate)."""
        with self._lock:
            self._storage[data.cascade_id] = data
            return True

    def get_by_cascade_id(self, cascade_id: str) -> CascadeEventData | None:
        """Retrieve a single cascade event by ID."""
        with self._lock:
            return self._storage.get(cascade_id)

    def find(
        self,
        *,
        namespace: str | None = None,
        trigger_type: str | None = None,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
        is_test: bool | None = None,
        offset: int = 0,
        limit: int = 100,
    ) -> list[CascadeEventData]:
        """Query with optional filters. Results ordered by timestamp DESC."""
        with self._lock:
            results = list(self._storage.values())
        results = self._apply_filters(
            results, namespace, trigger_type, start_date, end_date, is_test
        )
        results.sort(
            key=lambda d: d.timestamp or datetime.min,
            reverse=True,
        )
        return results[offset : offset + limit]

    def count(
        self,
        *,
        namespace: str | None = None,
        trigger_type: str | None = None,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
    ) -> int:
        """Count cascade events matching filters."""
        with self._lock:
            results = list(self._storage.values())
        results = self._apply_filters(
            results, namespace, trigger_type, start_date, end_date
        )
        return len(results)

    def delete_older_than(self, cutoff: datetime) -> int:
        """Delete archived events older than cutoff."""
        with self._lock:
            to_delete = [
                cid
                for cid, data in self._storage.items()
                if data.timestamp and data.timestamp < cutoff
            ]
            for cid in to_delete:
                del self._storage[cid]
            return len(to_delete)

    def get_chain(
        self,
        namespace: str,
        *,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
    ) -> list[CascadeEventData]:
        """Retrieve hash chain for integrity verification. Ordered by timestamp ASC."""
        with self._lock:
            results = [d for d in self._storage.values() if d.namespace == namespace]
        if start_date:
            results = [r for r in results if r.timestamp and r.timestamp >= start_date]
        if end_date:
            results = [r for r in results if r.timestamp and r.timestamp <= end_date]
        results.sort(key=lambda d: d.timestamp or datetime.min)
        return results

    def clear(self) -> None:
        """Clear all entries (for test cleanup)."""
        with self._lock:
            self._storage.clear()

    @staticmethod
    def _apply_filters(
        results: list[CascadeEventData],
        namespace: str | None = None,
        trigger_type: str | None = None,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
        is_test: bool | None = None,
    ) -> list[CascadeEventData]:
        """Apply common query filters."""
        if namespace:
            results = [r for r in results if r.namespace == namespace]
        if trigger_type:
            results = [r for r in results if r.trigger_type == trigger_type]
        if start_date:
            results = [r for r in results if r.timestamp and r.timestamp >= start_date]
        if end_date:
            results = [r for r in results if r.timestamp and r.timestamp <= end_date]
        if is_test is not None:
            results = [r for r in results if r.is_test == is_test]
        return results
