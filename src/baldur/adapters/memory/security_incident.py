"""
In-Memory Security Incident Repository Implementation.

Thread-safe in-memory storage for security incidents.
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta
from typing import Any

from baldur.adapters.memory.base import _now
from baldur.interfaces.repositories import (
    SecurityIncidentData,
    SecurityIncidentRepository,
    SecurityIncidentStatus,
)


class InMemorySecurityIncidentRepository(SecurityIncidentRepository):
    """
    In-memory implementation of SecurityIncidentRepository.

    Thread-safe storage for security incidents in memory.
    """

    def __init__(self):
        self._storage: dict[int, SecurityIncidentData] = {}
        self._next_id = 1
        self._lock = threading.RLock()  # RLock for reentrant calls

    def create(
        self,
        incident_type: str,
        severity: str,
        description: str = "",
        source_ip: str | None = None,
        user_agent: str = "",
        user_id: int | None = None,
        entity_refs: dict[str, int] | None = None,
        raw_payload: dict[str, Any] | None = None,
    ) -> SecurityIncidentData:
        """Create a new security incident (domain-neutral)."""
        refs = entity_refs or {}

        with self._lock:
            incident = SecurityIncidentData(
                id=self._next_id,
                incident_type=incident_type,
                severity=severity,
                status=SecurityIncidentStatus.OPEN.value,
                description=description,
                source_ip=source_ip,
                user_agent=user_agent,
                user_id=user_id,
                entity_refs=refs,
                raw_payload=raw_payload or {},
                created_at=_now(),
                updated_at=_now(),
            )
            self._storage[self._next_id] = incident
            self._next_id += 1
            return incident

    def get_by_id(self, id: int) -> SecurityIncidentData | None:
        """Get a security incident by ID."""
        with self._lock:
            return self._storage.get(id)

    def update_status(
        self,
        id: int,
        status: str,
        investigation_notes: str = "",
        assigned_to_id: int | None = None,
    ) -> bool:
        """Update incident status."""
        with self._lock:
            entry = self._storage.get(id)
            if entry is None:
                return False

            updated = SecurityIncidentData(
                id=entry.id,
                incident_type=entry.incident_type,
                severity=entry.severity,
                status=status,
                description=entry.description,
                source_ip=entry.source_ip,
                user_agent=entry.user_agent,
                user_id=entry.user_id,
                entity_refs=entry.entity_refs,
                raw_payload=entry.raw_payload,
                assigned_to_id=assigned_to_id or entry.assigned_to_id,
                investigation_notes=investigation_notes or entry.investigation_notes,
                resolved_at=(
                    _now()
                    if status == SecurityIncidentStatus.RESOLVED.value
                    else entry.resolved_at
                ),
                created_at=entry.created_at,
                updated_at=_now(),
            )
            self._storage[id] = updated
            return True

    def find_by_type(
        self,
        incident_type: str,
        status: str | None = None,
        limit: int = 100,
    ) -> list[SecurityIncidentData]:
        """Find incidents by type."""
        with self._lock:
            results = []
            for entry in self._storage.values():
                if entry.incident_type != incident_type:
                    continue
                if status and entry.status != status:
                    continue
                results.append(entry)
                if len(results) >= limit:
                    break
            return results

    def find_by_source_ip(
        self,
        source_ip: str,
        since: datetime | None = None,
    ) -> list[SecurityIncidentData]:
        """Find incidents by source IP."""
        with self._lock:
            results = []
            for entry in self._storage.values():
                if entry.source_ip != source_ip:
                    continue
                if since and entry.created_at and entry.created_at < since:
                    continue
                results.append(entry)
            return results

    def count_by_source_ip(
        self,
        source_ip: str,
        since: datetime,
    ) -> int:
        """Count incidents by source IP since a given time."""
        return len(self.find_by_source_ip(source_ip, since))

    def get_open_incidents(self, limit: int = 100) -> list[SecurityIncidentData]:
        """Get all open incidents."""
        with self._lock:
            results = [
                entry
                for entry in self._storage.values()
                if entry.status == SecurityIncidentStatus.OPEN.value
            ]
            return results[:limit]

    def get_by_type(
        self,
        incident_type: str,
        limit: int = 100,
    ) -> list[SecurityIncidentData]:
        """Get incidents by type."""
        with self._lock:
            results = [
                entry
                for entry in self._storage.values()
                if entry.incident_type == incident_type
            ]
            return results[:limit]

    def get_by_severity(
        self,
        severity: str,
        limit: int = 100,
    ) -> list[SecurityIncidentData]:
        """Get incidents by severity."""
        with self._lock:
            results = [
                entry for entry in self._storage.values() if entry.severity == severity
            ]
            return results[:limit]

    def mark_as_resolved(
        self,
        id: int,
        investigation_notes: str = "",
    ) -> bool:
        """Mark incident as resolved."""
        return self.update_status(
            id=id,
            status=SecurityIncidentStatus.RESOLVED.value,
            investigation_notes=investigation_notes,
        )

    def get_recent_by_ip(
        self,
        source_ip: str,
        hours: int = 24,
        limit: int = 100,
    ) -> list[SecurityIncidentData]:
        """Get recent incidents from a specific IP."""
        since = _now() - timedelta(hours=hours)
        with self._lock:
            results = []
            for entry in self._storage.values():
                if entry.source_ip != source_ip:
                    continue
                if entry.created_at and entry.created_at < since:
                    continue
                results.append(entry)
                if len(results) >= limit:
                    break
            return results

    def count_by_type_since(
        self,
        incident_type: str,
        since: datetime,
    ) -> int:
        """Count incidents of a type since a given time."""
        with self._lock:
            count = 0
            for entry in self._storage.values():
                if entry.incident_type != incident_type:
                    continue
                if entry.created_at and entry.created_at >= since:
                    count += 1
            return count

    def get_statistics(self) -> dict[str, Any]:
        """Get statistics about security incidents."""
        with self._lock:
            stats: dict[str, Any] = {
                "total": len(self._storage),
                "by_type": {},
                "by_severity": {},
                "by_status": {},
            }
            for entry in self._storage.values():
                stats["by_type"][entry.incident_type] = (
                    stats["by_type"].get(entry.incident_type, 0) + 1
                )
                stats["by_severity"][entry.severity] = (
                    stats["by_severity"].get(entry.severity, 0) + 1
                )
                stats["by_status"][entry.status] = (
                    stats["by_status"].get(entry.status, 0) + 1
                )
            return stats

    def clear(self) -> None:
        """Clear all entries (for testing)."""
        with self._lock:
            self._storage.clear()
            self._next_id = 1
