"""
Redis DLQ Maintenance — archiving, cleanup, and size limit operations.

Extracted from RedisDLQRepository for single-responsibility.
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING, Any

import structlog

from baldur.interfaces.repositories import FailedOperationStatus
from baldur.utils.time import utc_now

if TYPE_CHECKING:
    from baldur.adapters.redis.dlq import RedisDLQRepository

logger = structlog.get_logger()

__all__ = ["RedisDLQMaintenance"]


class RedisDLQMaintenance:
    """DLQ archiving, purging, eviction, and cleanup statistics."""

    def __init__(self, repository: RedisDLQRepository) -> None:
        self._repo = repository

    @property
    def _backend(self):
        return self._repo._backend

    def archive_old_resolved(self, older_than_days: int = 30) -> int:
        """Archive resolved entries older than N days.

        The count reflects writes that actually landed: ``_update`` returns
        False when the blob is already gone (concurrent purge/eviction or TTL
        expiry between the snapshot read and the write), which is no
        transition and must not be counted.
        """
        resolved = self._repo.query.by_status(
            FailedOperationStatus.RESOLVED.value, limit=10000
        )

        archived = 0
        cutoff = utc_now() - timedelta(days=older_than_days)

        for entry in resolved:
            if not (entry.resolved_at and entry.resolved_at < cutoff):
                continue
            if self._repo._update(
                entry_id=entry.id,
                status=FailedOperationStatus.ARCHIVED.value,
            ):
                archived += 1

        return archived

    def purge_archived(
        self,
        ids: list[str] | None = None,
        older_than_days: int | None = None,
    ) -> int:
        """Permanently delete archived entries.

        ``older_than_days`` is compared with ``is not None`` (not truthiness) so
        an explicit ``0`` means "older than 0 days" — i.e. every archived entry
        resolved before now — matching the memory/SQL adapter contract. A
        ``None`` filter (neither argument) stays a no-op for the Redis adapter.

        The count reflects deletes that actually landed: ``delete`` is
        idempotent and returns False for an already-absent entry, which
        removes nothing and must not be counted.
        """
        if ids is not None and older_than_days is not None:
            raise ValueError("Specify either ids or older_than_days, not both")

        purged = 0

        if ids:
            for id in ids:
                entry = self._repo.get_by_id(id)
                if not entry or entry.status != FailedOperationStatus.ARCHIVED.value:
                    continue
                if self._repo.delete(id):
                    purged += 1
        elif older_than_days is not None:
            archived = self._repo.query.by_status(
                FailedOperationStatus.ARCHIVED.value, limit=10000
            )
            cutoff = utc_now() - timedelta(days=older_than_days)

            for entry in archived:
                if not (entry.resolved_at and entry.resolved_at < cutoff):
                    continue
                if self._repo.delete(entry.id):
                    purged += 1

        return purged

    def count_all(self) -> int:
        """Return active DLQ item count (ZCARD on PENDING_KEY, O(1))."""
        return self._backend.zcard(self._repo.PENDING_KEY)

    def count_by_domain(self, domain: str) -> int:
        """Return DLQ item count for a specific domain (ZCARD, O(1))."""
        domain_key = f"{self._repo.BY_DOMAIN_PREFIX}{domain}"
        return self._backend.zcard(domain_key)

    def get_oldest_ids(self, count: int, domain: str | None = None) -> list[str]:
        """Return IDs of the oldest items (by score/timestamp).

        ZSET members are opaque composite-id strings (538 D2); returned
        verbatim with no numeric coercion.
        """
        key = (
            f"{self._repo.BY_DOMAIN_PREFIX}{domain}"
            if domain
            else self._repo.PENDING_KEY
        )
        members = self._backend.zrange(key, 0, count - 1)
        return [m.decode() if isinstance(m, bytes) else str(m) for m in members]

    # Statuses that must never be evicted (active processing in progress)
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
        for entry_id in oldest_ids:
            data = self._repo._decode_entry(self._repo._load_blob(entry_id))
            if data and data.get("status", "") in self._EVICTION_PROTECTED:
                logger.warning(
                    "dlq.eviction_skipped_protected",
                    entry_id=entry_id,
                    status=data.get("status"),
                )
                continue
            if self._repo.delete(entry_id):
                evicted += 1
        return evicted

    def get_cleanup_stats(self) -> dict[str, Any]:
        """Get statistics for cleanup operations.

        D5: ``by_status`` iterates PENDING + every ``_STATUS_INDEXED`` status
        via ``count_by_status`` (each an O(1) ZCARD), dropping zero counts.
        Replaces the prior hardcoded 5-status subset that omitted
        replaying/reviewing/replayed/expired/permanently_failed and surfaced
        zero counts as ``: 0``. Keeps the ``by_status`` shape consistent with
        the memory (iterates storage) and SQL (``GROUP BY status``) adapters,
        which both surface only present statuses.
        """
        stats = self._repo.query.get_statistics()

        by_status: dict[str, int] = {}
        pending_count = self._repo.query.count_by_status(
            FailedOperationStatus.PENDING.value
        )
        if pending_count:
            by_status[FailedOperationStatus.PENDING.value] = pending_count
        for s in self._repo._STATUS_INDEXED:
            n = self._repo.query.count_by_status(s)
            if n:
                by_status[s] = n

        resolved = self._repo.query.by_status(
            FailedOperationStatus.RESOLVED.value, limit=10000
        )
        archived = self._repo.query.by_status(
            FailedOperationStatus.ARCHIVED.value, limit=10000
        )

        now = utc_now()
        resolved_30_days = sum(
            1 for e in resolved if e.resolved_at and (now - e.resolved_at).days > 30
        )
        archived_90_days = sum(
            1 for e in archived if e.resolved_at and (now - e.resolved_at).days > 90
        )

        return {
            "total": stats["total"],
            "by_status": by_status,
            "resolved_older_than_30_days": resolved_30_days,
            "archived_older_than_90_days": archived_90_days,
        }

    def count_archived_older_than(self, older_than_days: int) -> int:
        """Count archived entries older than N days."""
        archived = self._repo.query.by_status(
            FailedOperationStatus.ARCHIVED.value, limit=10000
        )
        cutoff = utc_now() - timedelta(days=older_than_days)
        return sum(1 for e in archived if e.resolved_at and e.resolved_at < cutoff)
