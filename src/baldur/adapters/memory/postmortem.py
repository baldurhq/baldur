"""In-memory implementation for PostmortemRepository.

Thread-safe. Suitable for testing and non-Django environments.
"""

from __future__ import annotations

import threading
from datetime import datetime
from typing import Any

from baldur.interfaces.repositories import PostmortemData, PostmortemRepository

__all__ = ["InMemoryPostmortemRepository"]


class InMemoryPostmortemRepository(PostmortemRepository):
    """In-memory implementation for PostmortemRepository.

    Thread-safe. Suitable for testing and non-Django environments.
    """

    def __init__(self) -> None:
        self._storage: dict[str, PostmortemData] = {}  # incident_id -> data
        self._lock = threading.RLock()

    def save(self, data: PostmortemData) -> bool:
        """Persist a postmortem record."""
        with self._lock:
            self._storage[data.incident_id] = data
            return True

    def get_by_incident_id(self, incident_id: str) -> PostmortemData | None:
        """Retrieve a single postmortem by incident ID."""
        with self._lock:
            return self._storage.get(incident_id)

    def find(
        self,
        *,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
        service: str | None = None,
        min_duration: float | None = None,
        offset: int = 0,
        limit: int = 100,
    ) -> list[PostmortemData]:
        """Query postmortems with optional filters."""
        with self._lock:
            results = list(self._storage.values())
        results = self._apply_filters(
            results, start_date, end_date, service, min_duration
        )
        results.sort(key=lambda d: d.started_at or datetime.min, reverse=True)
        return results[offset : offset + limit]

    def count(
        self,
        *,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
        service: str | None = None,
        min_duration: float | None = None,
    ) -> int:
        """Count postmortems matching filters."""
        with self._lock:
            results = list(self._storage.values())
        results = self._apply_filters(
            results, start_date, end_date, service, min_duration
        )
        return len(results)

    def update_fields(
        self,
        incident_id: str,
        fields: dict[str, Any],
    ) -> bool:
        """Partial update of specific fields."""
        with self._lock:
            data = self._storage.get(incident_id)
            if data is None:
                return False
            for key, value in fields.items():
                if hasattr(data, key):
                    existing = getattr(data, key)
                    if isinstance(existing, dict) and isinstance(value, dict):
                        existing.update(value)
                    else:
                        setattr(data, key, value)
            return True

    def clear(self) -> None:
        """Clear all entries (for test cleanup)."""
        with self._lock:
            self._storage.clear()

    @staticmethod
    def _apply_filters(
        results: list[PostmortemData],
        start_date: datetime | None,
        end_date: datetime | None,
        service: str | None,
        min_duration: float | None,
    ) -> list[PostmortemData]:
        """Apply common query filters."""
        if start_date:
            results = [
                r for r in results if r.started_at and r.started_at >= start_date
            ]
        if end_date:
            results = [r for r in results if r.started_at and r.started_at <= end_date]
        if min_duration is not None:
            results = [r for r in results if r.duration_seconds >= min_duration]
        if service:
            results = [r for r in results if service in r.affected_services]
        return results
