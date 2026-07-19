"""
In-Memory Failed Operation Repository Implementation.

Thread-safe in-memory storage for DLQ (Dead Letter Queue) entries.
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta
from typing import Any

from baldur.adapters.memory.base import _now
from baldur.dlq.helpers import compress_entries
from baldur.interfaces.repositories import (
    DLQCompressedEntry,
    DLQCompressedStatus,
    FailedOperationData,
    FailedOperationRepository,
    FailedOperationStatus,
)


class InMemoryFailedOperationRepository(FailedOperationRepository):
    """
    In-memory implementation of FailedOperationRepository.

    Thread-safe storage for DLQ entries in memory.
    Data is lost when the process exits.

    Maintains status/domain indexes for O(1) lookup instead of O(n) scan.
    """

    def __init__(self):
        # 538 D1: opaque-string ids. _next_id stays the internal monotonic
        # counter; the storage key and DTO id are str(self._next_id), so no
        # per-lookup int() conversion is scattered across the id-keyed methods.
        self._storage: dict[str, FailedOperationData] = {}
        self._next_id = 1
        self._lock = threading.RLock()  # RLock for reentrant calls

        # Secondary indexes: ID sets by status, domain, and (status, domain)
        self._index_by_status: dict[str, set[str]] = {}
        self._index_by_domain: dict[str, set[str]] = {}
        self._index_by_status_domain: dict[tuple[str, str], set[str]] = {}

        # Compressed entry storage (351_DLQ_COMPRESSION)
        self._compressed_storage: dict[str, DLQCompressedEntry] = {}

    def create(
        self,
        domain: str,
        failure_type: str,
        error_message: str = "",
        error_code: str = "",
        entity_type: str | None = None,
        entity_id: str | None = None,
        entity_refs: dict[str, Any] | None = None,
        user_id: int | None = None,
        snapshot_data: dict[str, Any] | None = None,
        request_data: dict[str, Any] | None = None,
        response_data: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        retry_count: int = 0,
        max_retries: int = 2,
        next_action_hint: str = "",
        recommended_action: str = "",
        expires_at: datetime | None = None,
    ) -> FailedOperationData:
        """Create a new failed operation record (domain-neutral)."""
        refs = entity_refs or {}

        with self._lock:
            status = FailedOperationStatus.PENDING.value
            entry_id = str(self._next_id)
            entry = FailedOperationData(
                id=entry_id,
                domain=domain,
                failure_type=failure_type,
                status=status,
                entity_type=entity_type,
                entity_id=entity_id,
                entity_refs=refs,
                user_id=user_id,
                snapshot_data=snapshot_data or {},
                error_code=error_code,
                error_message=error_message,
                retry_count=retry_count,
                max_retries=max_retries,
                request_data=request_data or {},
                response_data=response_data or {},
                metadata=metadata or {},
                next_action_hint=next_action_hint,
                recommended_action=recommended_action,
                created_at=_now(),
                updated_at=_now(),
                expires_at=expires_at,
            )
            self._storage[entry_id] = entry

            self._add_to_index(entry_id, status, domain)

            self._next_id += 1
            return entry

    def _add_to_index(self, entry_id: str, status: str, domain: str) -> None:
        """Add entry to indexes. Must be called with lock held."""
        if status not in self._index_by_status:
            self._index_by_status[status] = set()
        self._index_by_status[status].add(entry_id)

        if domain not in self._index_by_domain:
            self._index_by_domain[domain] = set()
        self._index_by_domain[domain].add(entry_id)

        key = (status, domain)
        if key not in self._index_by_status_domain:
            self._index_by_status_domain[key] = set()
        self._index_by_status_domain[key].add(entry_id)

    def _remove_from_index(self, entry_id: str, status: str, domain: str) -> None:
        """Remove entry from indexes. Must be called with lock held."""
        if status in self._index_by_status:
            self._index_by_status[status].discard(entry_id)
        if domain in self._index_by_domain:
            self._index_by_domain[domain].discard(entry_id)
        key = (status, domain)
        if key in self._index_by_status_domain:
            self._index_by_status_domain[key].discard(entry_id)

    def _update_index_status(
        self, entry_id: str, old_status: str, new_status: str, domain: str
    ) -> None:
        """Update indexes on status change. Must be called with lock held."""
        self._remove_from_index(entry_id, old_status, domain)
        self._add_to_index(entry_id, new_status, domain)

    def _copy_with_updates(
        self, entry: FailedOperationData, **updates
    ) -> FailedOperationData:
        """Create a copy of entry with specified field updates."""
        return FailedOperationData(
            id=updates.get("id", entry.id),
            domain=updates.get("domain", entry.domain),
            failure_type=updates.get("failure_type", entry.failure_type),
            status=updates.get("status", entry.status),
            entity_type=updates.get("entity_type", entry.entity_type),
            entity_id=updates.get("entity_id", entry.entity_id),
            entity_refs=updates.get("entity_refs", entry.entity_refs),
            user_id=updates.get("user_id", entry.user_id),
            snapshot_data=updates.get("snapshot_data", entry.snapshot_data),
            error_code=updates.get("error_code", entry.error_code),
            error_message=updates.get("error_message", entry.error_message),
            retry_count=updates.get("retry_count", entry.retry_count),
            max_retries=updates.get("max_retries", entry.max_retries),
            last_retry_at=updates.get("last_retry_at", entry.last_retry_at),
            request_data=updates.get("request_data", entry.request_data),
            response_data=updates.get("response_data", entry.response_data),
            metadata=updates.get("metadata", entry.metadata),
            resolved_at=updates.get("resolved_at", entry.resolved_at),
            resolved_by_id=updates.get("resolved_by_id", entry.resolved_by_id),
            resolution_type=updates.get("resolution_type", entry.resolution_type),
            resolution_note=updates.get("resolution_note", entry.resolution_note),
            next_action_hint=updates.get("next_action_hint", entry.next_action_hint),
            recommended_action=updates.get(
                "recommended_action", entry.recommended_action
            ),
            created_at=updates.get("created_at", entry.created_at),
            updated_at=updates.get("updated_at", _now()),
            expires_at=updates.get("expires_at", entry.expires_at),
        )

    def get_by_id(self, id: str) -> FailedOperationData | None:
        """Get a failed operation by ID."""
        with self._lock:
            return self._storage.get(id)

    def get_pending_by_domain(
        self,
        domain: str,
        limit: int = 100,
    ) -> list[FailedOperationData]:
        """Get pending operations for a specific domain."""
        with self._lock:
            key = (FailedOperationStatus.PENDING.value, domain)
            entry_ids = self._index_by_status_domain.get(key, set())
            results = []
            for entry_id in entry_ids:
                if len(results) >= limit:
                    break
                entry = self._storage.get(entry_id)
                if entry:
                    results.append(entry)
            return results

    def get_pending_count_by_domain(self, domain: str) -> int:
        """Get count of pending operations for a domain."""
        with self._lock:
            key = (FailedOperationStatus.PENDING.value, domain)
            return len(self._index_by_status_domain.get(key, set()))

    def update_status(
        self,
        id: str,
        status: str,
        resolution_type: str = "",
        resolution_note: str = "",
        resolved_by_id: int | None = None,
        recommended_action: str = "",
    ) -> bool:
        """Update the status of a failed operation."""
        with self._lock:
            entry = self._storage.get(id)
            if entry is None:
                return False

            old_status = entry.status
            updated = self._copy_with_updates(
                entry,
                status=status,
                resolved_at=(
                    _now()
                    if status == FailedOperationStatus.RESOLVED.value
                    else entry.resolved_at
                ),
                resolved_by_id=resolved_by_id or entry.resolved_by_id,
                resolution_type=resolution_type or entry.resolution_type,
                resolution_note=resolution_note or entry.resolution_note,
                recommended_action=recommended_action or entry.recommended_action,
            )
            self._storage[id] = updated

            if old_status != status:
                self._update_index_status(id, old_status, status, entry.domain)

            return True

    def increment_retry_count(self, id: str) -> bool:
        """Increment retry count and update last_retry_at."""
        with self._lock:
            entry = self._storage.get(id)
            if entry is None:
                return False

            updated = self._copy_with_updates(
                entry,
                retry_count=entry.retry_count + 1,
                last_retry_at=_now(),
            )
            self._storage[id] = updated
            return True

    def mark_as_resolved(
        self,
        id: str,
        resolution_type: str,
        resolution_note: str = "",
        resolved_by_id: int | None = None,
    ) -> bool:
        """Mark a failed operation as resolved."""
        return self.update_status(
            id=id,
            status=FailedOperationStatus.RESOLVED.value,
            resolution_type=resolution_type,
            resolution_note=resolution_note,
            resolved_by_id=resolved_by_id,
        )

    def get_expired_operations(
        self,
        before_date: datetime,
        limit: int = 100,
    ) -> list[FailedOperationData]:
        """Get operations that have expired."""
        with self._lock:
            results = [
                entry
                for entry in self._storage.values()
                if entry.expires_at and entry.expires_at < before_date
            ]
            return results[:limit]

    def bulk_update_status(
        self,
        ids: list[str],
        status: str,
    ) -> int:
        """Bulk update status for multiple operations."""
        count = 0
        for id in ids:
            if self.update_status(id, status):
                count += 1
        return count

    def find_by_status(
        self,
        status: str,
        domain: str | None = None,
        failure_type: str | None = None,
        limit: int = 100,
    ) -> list[FailedOperationData]:
        """Find operations by status with optional filters."""
        with self._lock:
            if domain:
                key = (status, domain)
                entry_ids = self._index_by_status_domain.get(key, set())
            else:
                entry_ids = self._index_by_status.get(status, set())

            results = []
            for entry_id in entry_ids:
                if len(results) >= limit:
                    break
                entry = self._storage.get(entry_id)
                if entry is None:
                    continue
                if failure_type and entry.failure_type != failure_type:
                    continue
                results.append(entry)
            return results

    def _select_for_find(
        self,
        status: str | None,
        domain: str | None,
        failure_type: str | None,
    ) -> list[FailedOperationData]:
        """Resolve the filtered (unsorted) entry list. Must hold the lock.

        Picks the most selective secondary index for the (status, domain)
        combination, falls back to full storage when neither is set, then
        applies the non-indexed ``failure_type`` filter in Python.
        """
        if status is not None and domain is not None:
            entry_ids = self._index_by_status_domain.get((status, domain), set())
            entries = [self._storage[i] for i in entry_ids if i in self._storage]
        elif status is not None:
            entry_ids = self._index_by_status.get(status, set())
            entries = [self._storage[i] for i in entry_ids if i in self._storage]
        elif domain is not None:
            entry_ids = self._index_by_domain.get(domain, set())
            entries = [self._storage[i] for i in entry_ids if i in self._storage]
        else:
            entries = list(self._storage.values())

        if failure_type is not None:
            entries = [e for e in entries if e.failure_type == failure_type]
        return entries

    def find(
        self,
        *,
        status: str | None = None,
        domain: str | None = None,
        failure_type: str | None = None,
        offset: int = 0,
        limit: int = 100,
    ) -> list[FailedOperationData]:
        """Paginated cross-status query ordered by created_at DESC."""
        with self._lock:
            entries = self._select_for_find(status, domain, failure_type)
            entries.sort(key=lambda e: e.created_at or _now(), reverse=True)
            return entries[offset : offset + limit]

    def count(
        self,
        *,
        status: str | None = None,
        domain: str | None = None,
        failure_type: str | None = None,
    ) -> int:
        """Count operations matching filters (pre-slice set size)."""
        with self._lock:
            return len(self._select_for_find(status, domain, failure_type))

    def count_created_in_window(self, start: datetime, end: datetime) -> int:
        """Count entries whose created_at is within the inclusive [start, end]."""
        with self._lock:
            return sum(
                1
                for entry in self._storage.values()
                if entry.created_at and start <= entry.created_at <= end
            )

    def find_replayable(
        self,
        max_retries: int,
        domain: str | None = None,
        failure_type: str | None = None,
        limit: int = 100,
    ) -> list[FailedOperationData]:
        """Find operations that can be replayed."""
        with self._lock:
            pending_status = FailedOperationStatus.PENDING.value
            if domain:
                key = (pending_status, domain)
                entry_ids = self._index_by_status_domain.get(key, set())
            else:
                entry_ids = self._index_by_status.get(pending_status, set())

            results = []
            for entry_id in entry_ids:
                if len(results) >= limit:
                    break
                entry = self._storage.get(entry_id)
                if entry is None:
                    continue
                if entry.retry_count >= max_retries:
                    continue
                if failure_type and entry.failure_type != failure_type:
                    continue
                results.append(entry)
            return results

    def find_sla_breached(
        self,
        current_time: datetime,
        sla_thresholds: dict[str, timedelta],
    ) -> list[FailedOperationData]:
        """Find operations that have breached their SLA."""
        with self._lock:
            pending_ids = self._index_by_status.get(
                FailedOperationStatus.PENDING.value, set()
            )
            results = []
            for entry_id in pending_ids:
                entry = self._storage.get(entry_id)
                if entry is None:
                    continue
                threshold = sla_thresholds.get(entry.domain, timedelta(hours=24))
                if entry.created_at and current_time - entry.created_at > threshold:
                    results.append(entry)
            return results

    def find_expired(
        self,
        current_time: datetime,
    ) -> list[FailedOperationData]:
        """Find operations past their retention period."""
        with self._lock:
            return [
                entry
                for entry in self._storage.values()
                if entry.expires_at and entry.expires_at < current_time
            ]

    def get_statistics(self) -> dict[str, Any]:
        """Get statistics about failed operations.

        Adds pending-specific breakdowns for the daily report:
        - pending_by_domain: {domain: pending_count} (required by
          update_dlq_pending_gauges — pre-existing bug fix)
        - pending_by_domain_and_failure_type: {domain: {failure_type: count}}
          (powers DLQPendingBreakdown in daily report)

        Memory adapter iterates the pending index (O(N) in-memory).
        """
        # D9: pending-specific breakdowns for the daily report.
        with self._lock:
            pending_status = FailedOperationStatus.PENDING.value
            pending_ids = self._index_by_status.get(pending_status, set())

            pending_by_domain: dict[str, int] = {}
            pending_by_domain_and_failure_type: dict[str, dict[str, int]] = {}

            for entry_id in pending_ids:
                entry = self._storage.get(entry_id)
                if entry is None:
                    continue
                pending_by_domain[entry.domain] = (
                    pending_by_domain.get(entry.domain, 0) + 1
                )
                ft_map = pending_by_domain_and_failure_type.setdefault(entry.domain, {})
                ft_map[entry.failure_type] = ft_map.get(entry.failure_type, 0) + 1

            return {
                "total": len(self._storage),
                "by_status": {
                    status: len(ids) for status, ids in self._index_by_status.items()
                },
                "by_domain": {
                    domain: len(ids) for domain, ids in self._index_by_domain.items()
                },
                "pending_by_domain": pending_by_domain,
                "pending_by_domain_and_failure_type": (
                    pending_by_domain_and_failure_type
                ),
            }

    def get_facet_counts(  # noqa: C901
        self,
        *,
        status: str | None = None,
        domain: str | None = None,
    ) -> dict[str, dict[str, int]]:
        """Faceted status×domain counts via the 1D/2D indexes.

        ``by_status`` is scoped by ``domain``; ``by_domain`` is scoped by
        ``status`` (faceted-search semantics). Empty buckets are dropped
        explicitly with ``if ids``: ``_remove_from_index`` discards ids
        without deleting an emptied set, so a fully-drained status/domain key
        lingers as an empty set and would otherwise surface as ``:0``,
        breaking zero-drop parity with the SQL/Redis adapters.
        """
        # D2/D3: faceted status×domain counts over the 1D/2D indexes.
        with self._lock:
            by_status: dict[str, int] = {}
            if domain is None:
                for s, ids in self._index_by_status.items():
                    if ids:
                        by_status[s] = len(ids)
            else:
                for (s, d), ids in self._index_by_status_domain.items():
                    if d == domain and ids:
                        by_status[s] = len(ids)

            by_domain: dict[str, int] = {}
            if status is None:
                for d, ids in self._index_by_domain.items():
                    if ids:
                        by_domain[d] = len(ids)
            else:
                for (s, d), ids in self._index_by_status_domain.items():
                    if s == status and ids:
                        by_domain[d] = len(ids)

            return {"by_status": by_status, "by_domain": by_domain}

    # Force-redrive accepts an at-cap entry parked in REQUIRES_REVIEW (the 606
    # poison-pill terminal state) in addition to the normal PENDING source.
    _FORCE_ACQUIRABLE = frozenset(
        {
            FailedOperationStatus.PENDING.value,
            FailedOperationStatus.REQUIRES_REVIEW.value,
        }
    )

    def try_acquire_for_replay(
        self,
        id: str,
        max_retries: int,
        force: bool = False,
    ) -> FailedOperationData | None:
        """Atomically acquire a DLQ entry for replay.

        ``force=True`` bypasses the cap gate (operator cap-override): it accepts
        a {PENDING, REQUIRES_REVIEW} source, resets retry_count to a fresh budget
        (this redrive is attempt 1), and stamps the metadata history scar before
        the reset. See ``FailedOperationRepository.try_acquire_for_replay``.
        """
        with self._lock:
            entry = self._storage.get(id)
            if entry is None:
                return None

            if force:
                if entry.status not in self._FORCE_ACQUIRABLE:
                    return None
            else:
                if entry.status != FailedOperationStatus.PENDING.value:
                    return None
                if entry.retry_count >= max_retries:
                    return None

            old_status = entry.status
            new_status = FailedOperationStatus.REPLAYING.value

            if force:
                # D3/G5: stamp the forensic history before resetting the budget,
                # then grant a fresh cap budget (retry_count == 1 == this redrive).
                metadata = dict(entry.metadata or {})
                metadata["previous_total_retries"] = (
                    metadata.get("previous_total_retries", 0) + entry.retry_count
                )
                metadata["force_redrive_count"] = (
                    metadata.get("force_redrive_count", 0) + 1
                )
                updated = self._copy_with_updates(
                    entry,
                    status=new_status,
                    retry_count=1,
                    last_retry_at=_now(),
                    metadata=metadata,
                )
            else:
                updated = self._copy_with_updates(
                    entry,
                    status=new_status,
                    retry_count=entry.retry_count + 1,
                    last_retry_at=_now(),
                )
            self._storage[id] = updated

            self._update_index_status(id, old_status, new_status, entry.domain)

            return updated

    def complete_replay(
        self,
        id: str,
        success: bool,
        resolution_type: str = "",
        note: str = "",
        resolved_by_id: int | None = None,
        error_details: dict[str, Any] | None = None,
    ) -> bool:
        """Complete a replay operation by updating the final status."""
        with self._lock:
            entry = self._storage.get(id)
            if entry is None:
                return False

            resolved_at: datetime | None
            if success:
                new_status = FailedOperationStatus.RESOLVED.value
                resolved_at = _now()
            elif entry.retry_count >= entry.max_retries:
                # At cap: converge to the terminal review state (mirrors Redis)
                new_status = FailedOperationStatus.REQUIRES_REVIEW.value
                resolved_at = entry.resolved_at
            else:
                # Under cap: revert to pending for the next retry
                new_status = FailedOperationStatus.PENDING.value
                resolved_at = entry.resolved_at

            old_status = entry.status
            updated = self._copy_with_updates(
                entry,
                status=new_status,
                error_message=note or entry.error_message,
                metadata={**(entry.metadata or {}), **(error_details or {})},
                resolved_at=resolved_at,
                resolved_by_id=resolved_by_id or entry.resolved_by_id,
                resolution_type=resolution_type or entry.resolution_type,
                resolution_note=note or entry.resolution_note,
            )
            self._storage[id] = updated

            if old_status != new_status:
                self._update_index_status(id, old_status, new_status, entry.domain)

            return True

    def release_stale_replaying(
        self,
        older_than_minutes: int = 30,
    ) -> int:
        """Release DLQ entries stuck in REPLAYING state."""
        cutoff = _now() - timedelta(minutes=older_than_minutes)
        released = 0

        with self._lock:
            for id, entry in list(self._storage.items()):
                if (
                    entry.status == FailedOperationStatus.REPLAYING.value
                    and entry.last_retry_at
                    and entry.last_retry_at < cutoff
                ):
                    old_status = entry.status
                    new_status = FailedOperationStatus.PENDING.value
                    updated = self._copy_with_updates(
                        entry,
                        status=new_status,
                    )
                    self._storage[id] = updated
                    self._update_index_status(id, old_status, new_status, entry.domain)
                    released += 1

        return released

    def clear(self) -> None:
        """Clear all entries (for testing)."""
        with self._lock:
            self._storage.clear()
            self._next_id = 1
            self._index_by_status.clear()
            self._index_by_domain.clear()
            self._index_by_status_domain.clear()

    # =========================================================================
    # Cleanup Operations
    # =========================================================================

    def archive_old_resolved(
        self,
        older_than_days: int = 30,
    ) -> int:
        """Archive resolved entries older than N days."""
        cutoff = _now() - timedelta(days=older_than_days)
        archived_count = 0

        with self._lock:
            for id, entry in list(self._storage.items()):
                if (
                    entry.status == FailedOperationStatus.RESOLVED.value
                    and entry.resolved_at
                    and entry.resolved_at < cutoff
                ):
                    old_status = entry.status
                    new_status = FailedOperationStatus.ARCHIVED.value
                    updated = self._copy_with_updates(
                        entry,
                        status=new_status,
                    )
                    self._storage[id] = updated
                    self._update_index_status(id, old_status, new_status, entry.domain)
                    archived_count += 1

        return archived_count

    def _purge_by_ids(self, ids: list[str]) -> int:
        """Purge specific archived entries by ID. Must be called with lock held."""
        purged_count = 0
        for id in ids:
            entry = self._storage.get(id)
            if entry and entry.status == FailedOperationStatus.ARCHIVED.value:
                self._remove_from_index(id, entry.status, entry.domain)
                del self._storage[id]
                purged_count += 1
            elif entry:
                raise ValueError(
                    f"Entry {id} is not archived (status: {entry.status}). "
                    "Only archived entries can be purged."
                )
        return purged_count

    def _purge_older_than(self, older_than_days: int) -> int:
        """Purge archived entries older than N days. Must be called with lock held."""
        cutoff = _now() - timedelta(days=older_than_days)
        to_delete = [
            (id, entry)
            for id, entry in self._storage.items()
            if entry.status == FailedOperationStatus.ARCHIVED.value
            and entry.updated_at
            and entry.updated_at < cutoff
        ]
        for id, entry in to_delete:
            self._remove_from_index(id, entry.status, entry.domain)
            del self._storage[id]
        return len(to_delete)

    def purge_archived(
        self,
        ids: list[str] | None = None,
        older_than_days: int | None = None,
    ) -> int:
        """Permanently delete archived entries."""
        if ids is not None and older_than_days is not None:
            raise ValueError("Specify either ids or older_than_days, not both")

        with self._lock:
            if ids is not None:
                return self._purge_by_ids(ids)
            if older_than_days is not None:
                return self._purge_older_than(older_than_days)
            # No-args is a no-op (fail-safe): a destructive purge with no
            # selection criteria deletes nothing. Use ``older_than_days=0`` to
            # purge every archived entry.
            return 0

    def count_archived_older_than(self, older_than_days: int) -> int:
        """Count archived entries older than N days."""
        cutoff = _now() - timedelta(days=older_than_days)
        with self._lock:
            return sum(
                1
                for entry in self._storage.values()
                if entry.status == FailedOperationStatus.ARCHIVED.value
                and entry.resolved_at
                and entry.resolved_at < cutoff
            )

    # =========================================================================
    # Size Limit / Overflow Operations (329_DLQ_SIZE_LIMIT)
    # =========================================================================

    def count_all(self) -> int:
        """Return active DLQ item count (excludes resolved/rejected/archived).

        Matches Redis adapter semantics where resolved entries are removed
        from the PENDING_KEY sorted set.
        """
        with self._lock:
            excluded = 0
            for status in (
                FailedOperationStatus.RESOLVED.value,
                FailedOperationStatus.REJECTED.value,
                FailedOperationStatus.ARCHIVED.value,
            ):
                excluded += len(self._index_by_status.get(status, set()))
            return len(self._storage) - excluded

    def count_by_domain(self, domain: str) -> int:
        """Return DLQ item count for a specific domain."""
        with self._lock:
            domain_ids = self._index_by_domain.get(domain, set())
            return len(domain_ids)

    def get_oldest_ids(self, count: int, domain: str | None = None) -> list[str]:
        """Return IDs of the oldest items (by created_at)."""
        with self._lock:
            if domain:
                domain_ids = self._index_by_domain.get(domain, set())
                entries = [
                    (eid, self._storage[eid])
                    for eid in domain_ids
                    if eid in self._storage
                ]
            else:
                entries = list(self._storage.items())

            # Sort by created_at ascending (oldest first)
            entries.sort(key=lambda x: x[1].created_at or _now())
            return [eid for eid, _ in entries[:count]]

    def delete(self, entry_id: str) -> bool:
        """Delete a single entry by ID. Returns True if deleted."""
        with self._lock:
            entry = self._storage.pop(entry_id, None)
            if entry is None:
                return False
            self._remove_from_index(entry_id, entry.status, entry.domain)
            return True

    _EVICTION_PROTECTED = frozenset(
        {
            FailedOperationStatus.REPLAYING.value,
            FailedOperationStatus.REVIEWING.value,
        }
    )

    def evict_oldest(self, count: int, domain: str | None = None) -> int:
        """Delete the oldest items, skipping entries in protected statuses."""
        oldest_ids = self.get_oldest_ids(count, domain)
        evicted = 0
        with self._lock:
            for eid in oldest_ids:
                entry = self._storage.get(eid)
                if entry and entry.status in self._EVICTION_PROTECTED:
                    continue
                entry = self._storage.pop(eid, None)
                if entry:
                    self._remove_from_index(eid, entry.status, entry.domain)
                    evicted += 1
        return evicted

    def get_cleanup_stats(self) -> dict[str, Any]:
        """Get statistics for cleanup operations."""
        now = _now()
        day_30_ago = now - timedelta(days=30)
        day_90_ago = now - timedelta(days=90)

        with self._lock:
            total = len(self._storage)
            by_status: dict[str, int] = {}
            resolved_older_than_30_days = 0
            archived_older_than_90_days = 0

            for entry in self._storage.values():
                # Count by status
                by_status[entry.status] = by_status.get(entry.status, 0) + 1

                # Count resolved older than 30 days
                if (
                    entry.status == FailedOperationStatus.RESOLVED.value
                    and entry.resolved_at
                    and entry.resolved_at < day_30_ago
                ):
                    resolved_older_than_30_days += 1

                # Count archived older than 90 days
                if (
                    entry.status == FailedOperationStatus.ARCHIVED.value
                    and entry.updated_at
                    and entry.updated_at < day_90_ago
                ):
                    archived_older_than_90_days += 1

            return {
                "total": total,
                "by_status": by_status,
                "resolved_older_than_30_days": resolved_older_than_30_days,
                "archived_older_than_90_days": archived_older_than_90_days,
            }

    # =========================================================================
    # Compression Operations (351_DLQ_COMPRESSION)
    # =========================================================================

    def compress_and_evict_oldest(self, count: int, domain: str | None = None) -> int:
        """
        Compress then evict oldest entries (in-memory implementation).

        Same logical flow as Redis adapter but uses Python dict/list.
        """
        # 1. Fetch oldest entries with full data
        oldest_ids = self.get_oldest_ids(count, domain)
        if not oldest_ids:
            return 0

        with self._lock:
            entries = [self._storage[eid] for eid in oldest_ids if eid in self._storage]

        if not entries:
            return 0

        # 2. Compress
        result = compress_entries(entries)
        if result is None:
            # PRO compression module not loaded — fail-open with 0 evictions.
            return 0

        # 3. Store compressed summaries
        for summary in result.entries:
            self.store_compressed_entry(summary)

        # 4. Delete originals
        evicted = 0
        with self._lock:
            for eid in oldest_ids:
                entry = self._storage.pop(eid, None)
                if entry:
                    self._remove_from_index(eid, entry.status, entry.domain)
                    evicted += 1

        return evicted

    def store_compressed_entry(self, entry: DLQCompressedEntry) -> bool:
        """Store compressed entry in memory dict."""
        self._compressed_storage[entry.id] = entry
        return True

    def get_compressed_entries(
        self,
        domain: str | None = None,
        status: str | None = None,
        limit: int = 100,
    ) -> list[DLQCompressedEntry]:
        """Query compressed entries from memory, newest first."""
        entries = sorted(
            self._compressed_storage.values(),
            key=lambda e: e.compressed_at,
            reverse=True,
        )
        if domain:
            entries = [e for e in entries if e.domain == domain]
        if status:
            entries = [e for e in entries if e.status == status]
        return entries[:limit]

    def get_compressed_entries_before(
        self,
        *,
        status: str,
        before: datetime,
        limit: int = 100,
        offset: int = 0,
        after: datetime | None = None,
    ) -> list[DLQCompressedEntry]:
        """Query compressed entries in a cutoff window, oldest first."""
        entries = [
            e
            for e in self._compressed_storage.values()
            if e.status == status
            and e.compressed_at < before
            and (after is None or e.compressed_at >= after)
        ]
        entries.sort(key=lambda e: e.compressed_at)
        return entries[offset : offset + limit]

    def get_compressed_summary(self) -> dict[str, Any]:
        """Aggregate statistics of compressed entries."""
        status_counts: dict[str, int] = {s.value: 0 for s in DLQCompressedStatus}
        total_compressed_items = 0

        for entry in self._compressed_storage.values():
            status_counts[entry.status] = status_counts.get(entry.status, 0) + 1
            total_compressed_items += entry.count

        return {
            "total_summaries": len(self._compressed_storage),
            "total_compressed_items": total_compressed_items,
            "by_status": status_counts,
        }

    def update_compressed_status(self, entry_id: str, new_status: str) -> bool:
        """Transition compressed entry lifecycle status."""
        entry = self._compressed_storage.get(entry_id)
        if entry is None:
            return False

        now = _now()
        entry.status = new_status

        if new_status == DLQCompressedStatus.STALE.value:
            entry.stale_at = now
        elif new_status == DLQCompressedStatus.ARCHIVED.value:
            entry.archived_at = now

        return True
