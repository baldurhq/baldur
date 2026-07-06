"""In-memory implementation for RecoverySessionArchiveRepository.

Thread-safe. Suitable for testing and non-Django environments.
"""

from __future__ import annotations

import threading
from datetime import datetime

from baldur.interfaces.repositories import RecoverySessionArchiveRepository
from baldur.models.recovery_session import RecoverySessionData

__all__ = ["InMemoryRecoverySessionArchiveRepository"]


class InMemoryRecoverySessionArchiveRepository(RecoverySessionArchiveRepository):
    """In-memory implementation for RecoverySessionArchiveRepository.

    Thread-safe with RLock. Suitable for testing and non-Django environments.
    """

    def __init__(self) -> None:
        self._storage: dict[str, RecoverySessionData] = {}  # session_id -> data
        self._lock = threading.RLock()

    def save(self, data: RecoverySessionData) -> bool:
        """Persist a recovery session record (overwrite on duplicate)."""
        with self._lock:
            self._storage[data.session_id] = data
            return True

    def get_by_session_id(self, session_id: str) -> RecoverySessionData | None:
        """Retrieve a single session by ID."""
        with self._lock:
            return self._storage.get(session_id)

    def find(
        self,
        *,
        namespace: str | None = None,
        status: str | None = None,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
        offset: int = 0,
        limit: int = 100,
    ) -> list[RecoverySessionData]:
        """Query with optional filters. Results ordered by started_at DESC."""
        with self._lock:
            results = list(self._storage.values())
        results = self._apply_filters(results, namespace, status, start_date, end_date)
        results.sort(
            key=lambda d: d.started_at or datetime.min,
            reverse=True,
        )
        return results[offset : offset + limit]

    def count(
        self,
        *,
        namespace: str | None = None,
        status: str | None = None,
    ) -> int:
        """Count sessions matching filters."""
        with self._lock:
            results = list(self._storage.values())
        if namespace:
            results = [r for r in results if r.namespace == namespace]
        if status:
            results = [r for r in results if r.status == status]
        return len(results)

    def update(self, data: RecoverySessionData) -> bool:
        """Full update of an existing session record."""
        with self._lock:
            if data.session_id not in self._storage:
                return False
            self._storage[data.session_id] = data
            return True

    def delete_older_than(self, cutoff: datetime) -> int:
        """Delete archived sessions older than cutoff."""
        with self._lock:
            to_delete = [
                sid
                for sid, data in self._storage.items()
                if data.started_at and data.started_at < cutoff
            ]
            for sid in to_delete:
                del self._storage[sid]
            return len(to_delete)

    def clear(self) -> None:
        """Clear all entries (for test cleanup)."""
        with self._lock:
            self._storage.clear()

    @staticmethod
    def _apply_filters(
        results: list[RecoverySessionData],
        namespace: str | None = None,
        status: str | None = None,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
    ) -> list[RecoverySessionData]:
        """Apply common query filters."""
        if namespace:
            results = [r for r in results if r.namespace == namespace]
        if status:
            results = [r for r in results if r.status == status]
        if start_date:
            results = [
                r for r in results if r.started_at and r.started_at >= start_date
            ]
        if end_date:
            results = [r for r in results if r.started_at and r.started_at <= end_date]
        return results
