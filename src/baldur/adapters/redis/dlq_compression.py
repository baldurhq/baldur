"""
Redis DLQ Compression — compressed entry storage and management.

Extracted from RedisDLQRepository for single-responsibility.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

import structlog

from baldur.audit.helpers import log_dlq_compress_audit
from baldur.dlq.helpers import compress_entries
from baldur.interfaces.repositories import DLQCompressedEntry, DLQCompressedStatus
from baldur.utils.time import utc_now

if TYPE_CHECKING:
    from baldur.adapters.redis.dlq import RedisDLQRepository

logger = structlog.get_logger()

__all__ = ["RedisDLQCompression"]

# Redis key patterns for compressed entries
_COMPRESSED_PREFIX = "dlq:compressed:"
_COMPRESSED_INDEX_KEY = "dlq:compressed:index"
_COMPRESSED_BY_DOMAIN_PREFIX = "dlq:compressed:by_domain:"

# Defensive ceiling on the number of source IDs embedded in a single compress
# audit event. Bounds the event's wire size at emit so an oversized batch does
# not exceed collector/transport message-size limits. A hard safety rail, not a
# tunable setting — the authoritative set identity (``source_ids_hash`` +
# ``source_count``) is always emitted in full; truncation drops only the
# forensic-convenience ID list.
_AUDIT_SOURCE_IDS_CAP = 5000


class RedisDLQCompression:
    """DLQ compression operations."""

    def __init__(self, repository: RedisDLQRepository) -> None:
        self._repo = repository

    @property
    def _backend(self):
        return self._repo._backend

    def compress_and_evict_oldest(self, count: int, domain: str | None = None) -> int:
        """Compress then evict oldest entries."""
        oldest_ids = self._repo.maintenance.get_oldest_ids(count, domain)
        if not oldest_ids:
            return 0

        entries = []
        for entry_id in oldest_ids:
            entry = self._repo.get_by_id(entry_id)
            if entry is not None:
                entries.append(entry)

        if not entries:
            return 0

        result = compress_entries(entries)
        if result is None:
            # PRO compression module not loaded — fail-open, OSS returns 0 evictions.
            return 0

        for summary in result.entries:
            self.store_compressed_entry(summary)

        self._record_compression_audit(
            source_ids=[e.id for e in entries],
            summaries=result.entries,
        )

        evicted = 0
        for entry in entries:
            if self._repo.delete(entry.id):
                evicted += 1

        return evicted

    def store_compressed_entry(self, entry: DLQCompressedEntry) -> bool:
        """Store compressed entry as a STRING/blob + two index sorted sets.

        Mirrors the main-entry write: the summary dict is encoded to a single
        ``bytes`` blob (``fast_dumps``) and the blob + both index ``zadd``s are
        issued as one ``batch_write_ops`` call — all-or-nothing in normal mode
        (1 RTT), one fsync in degraded mode. ``set_blob`` writes to the bounded
        blob store, matching main-entry degraded semantics. The inner
        ``sample_context`` keeps its JSON-string form so the unchanged
        deserializer can ``fast_loads`` it.
        """
        from baldur.utils.serialization import fast_dumps, fast_dumps_str

        key = f"{_COMPRESSED_PREFIX}{entry.id}"
        data = {
            "id": entry.id,
            "domain": entry.domain,
            "failure_type": entry.failure_type,
            "error_code": entry.error_code,
            "count": str(entry.count),
            "first_seen": entry.first_seen.isoformat(),
            "last_seen": entry.last_seen.isoformat(),
            "sample_error_message": entry.sample_error_message,
            "sample_context": fast_dumps_str(entry.sample_context),
            "status": entry.status,
            "compressed_at": entry.compressed_at.isoformat(),
        }
        encoded = fast_dumps(data)

        score = entry.compressed_at.timestamp()
        domain_key = f"{_COMPRESSED_BY_DOMAIN_PREFIX}{entry.domain}"
        self._backend.batch_write_ops(
            [
                ("set_blob", key, encoded),
                ("zadd", _COMPRESSED_INDEX_KEY, {entry.id: score}),
                ("zadd", domain_key, {entry.id: score}),
            ]
        )
        return True

    def get_compressed_entries(
        self,
        domain: str | None = None,
        status: str | None = None,
        limit: int = 100,
    ) -> list[DLQCompressedEntry]:
        """Query compressed entries from Redis sorted set index."""
        from baldur.utils.serialization import fast_loads

        if domain:
            key = f"{_COMPRESSED_BY_DOMAIN_PREFIX}{domain}"
        else:
            key = _COMPRESSED_INDEX_KEY

        member_ids = self._backend.zrevrange(key, 0, limit - 1)

        entries = []
        for member_id in member_ids:
            entry_key = f"{_COMPRESSED_PREFIX}{member_id}"
            blob = self._backend.get_blob(entry_key)
            if blob is None:
                continue
            data = fast_loads(blob)
            if status and data.get("status") != status:
                continue
            entries.append(_deserialize_compressed_entry(data))

        return entries

    def get_compressed_summary(self) -> dict[str, Any]:
        """Aggregate statistics of compressed entries."""
        from baldur.utils.serialization import fast_loads

        total = self._backend.zcard(_COMPRESSED_INDEX_KEY)

        all_ids = self._backend.zrange(_COMPRESSED_INDEX_KEY, 0, -1)
        status_counts: dict[str, int] = {s.value: 0 for s in DLQCompressedStatus}
        total_compressed_items = 0

        for member_id in all_ids:
            entry_key = f"{_COMPRESSED_PREFIX}{member_id}"
            blob = self._backend.get_blob(entry_key)
            if blob is not None:
                data = fast_loads(blob)
                st = data.get("status", DLQCompressedStatus.ACTIVE.value)
                status_counts[st] = status_counts.get(st, 0) + 1
                total_compressed_items += int(data.get("count", 0))

        return {
            "total_summaries": total,
            "total_compressed_items": total_compressed_items,
            "by_status": status_counts,
        }

    def update_compressed_status(self, entry_id: str, new_status: str) -> bool:
        """Transition compressed entry lifecycle status.

        Rewrites the STRING/blob via GET → decode → mutate → encode → SET
        (mirroring the main-entry pure-field update). The index/by_domain sets
        are scored by ``compressed_at`` and do not move on a status change, so
        no index ops are needed — ``get_compressed_entries`` re-reads ``status``
        from the decoded blob and filters in Python.
        """
        from baldur.utils.serialization import fast_dumps, fast_loads

        key = f"{_COMPRESSED_PREFIX}{entry_id}"
        blob = self._backend.get_blob(key)
        if blob is None:
            return False

        data = fast_loads(blob)
        now = utc_now().isoformat()
        data["status"] = new_status

        if new_status == DLQCompressedStatus.STALE.value:
            data["stale_at"] = now
        elif new_status == DLQCompressedStatus.ARCHIVED.value:
            data["archived_at"] = now

        self._backend.set_blob(key, fast_dumps(data))
        return True

    def _record_compression_audit(
        self,
        source_ids: list[str],
        summaries: list[DLQCompressedEntry],
    ) -> None:
        """Record compression audit trail.

        The sorted ``source_ids`` are embedded directly in the audit details
        and persisted through the audit pipeline, which owns retention (and
        hot-tier TTL via the cascade). A defensive cap bounds a single event's
        wire size at emit: beyond ``_AUDIT_SOURCE_IDS_CAP`` ids the list is
        truncated and ``source_ids_truncated`` is set, but ``source_count`` and
        the order-independent ``source_ids_hash`` set fingerprint are always
        emitted in full.

        On opaque-string ids ``first_source_id``/``last_source_id`` become
        lexicographic min/max — accepted, as the authoritative set fingerprint
        is ``source_ids_hash`` and the first/last fields are
        forensic-convenience only.
        """
        import hashlib

        from baldur.utils.serialization import fast_canonical_dumps

        source_ids_sorted = sorted(source_ids)
        source_ids_hash = hashlib.sha256(
            fast_canonical_dumps(source_ids_sorted)
        ).hexdigest()

        audit_details: dict[str, Any] = {
            "source_count": len(source_ids),
            "source_ids_hash": f"sha256:{source_ids_hash}",
            "first_source_id": min(source_ids),
            "last_source_id": max(source_ids),
            "summaries": [
                {
                    "id": s.id,
                    "domain": s.domain,
                    "failure_type": s.failure_type,
                    "error_code": s.error_code,
                    "count": s.count,
                }
                for s in summaries
            ],
        }

        if len(source_ids_sorted) > _AUDIT_SOURCE_IDS_CAP:
            audit_details["source_ids"] = source_ids_sorted[:_AUDIT_SOURCE_IDS_CAP]
            audit_details["source_ids_truncated"] = True
            logger.warning(
                "dlq.compress_audit_source_ids_truncated",
                source_count=len(source_ids),
                cap=_AUDIT_SOURCE_IDS_CAP,
            )
        else:
            audit_details["source_ids"] = source_ids_sorted

        log_dlq_compress_audit(
            source_count=len(source_ids),
            summary_count=len(summaries),
            details=audit_details,
        )


def _deserialize_compressed_entry(data: dict) -> DLQCompressedEntry:
    """Deserialize a decoded compressed-entry blob dict to DLQCompressedEntry."""
    from baldur.utils.serialization import fast_loads

    return DLQCompressedEntry(
        id=data["id"],
        domain=data["domain"],
        failure_type=data["failure_type"],
        error_code=data["error_code"],
        count=int(data["count"]),
        first_seen=datetime.fromisoformat(data["first_seen"]),
        last_seen=datetime.fromisoformat(data["last_seen"]),
        sample_error_message=data.get("sample_error_message", ""),
        sample_context=fast_loads(data.get("sample_context", "{}")),
        status=data.get("status", DLQCompressedStatus.ACTIVE.value),
        compressed_at=datetime.fromisoformat(data["compressed_at"]),
        stale_at=(
            datetime.fromisoformat(data["stale_at"]) if data.get("stale_at") else None
        ),
        archived_at=(
            datetime.fromisoformat(data["archived_at"])
            if data.get("archived_at")
            else None
        ),
    )
