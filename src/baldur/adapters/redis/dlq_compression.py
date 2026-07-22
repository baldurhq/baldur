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
# Per-status + composite (status, domain) index families, mirroring the main
# DLQ's #544/#541 architecture. Scored by ``compressed_at`` epoch. Maintained
# on the write path (store + transition); no read routes through them yet —
# their consumer is the follow-up read switch. See D1.
_COMPRESSED_STATUS_PREFIX = "dlq:compressed:status:"
_COMPRESSED_STATUS_DOMAIN_PREFIX = "dlq:compressed:status_domain:"

# Chunk size for the summary's batched blob read (D4). Bounds one ``MGET``'s
# response so a single command cannot monopolise Redis's single thread — the
# same rationale and value as ``dlq_query._collect_pending_breakdown``. Not a
# settings field: an RTT/memory microtune with no operator-facing axis, unlike
# ``compress_summary_scan_cap`` (an exactness/cost trade the operator owns).
# Named-constant precedent in this file: ``_AUDIT_SOURCE_IDS_CAP``.
_SUMMARY_MGET_CHUNK = 500

# Reserved structural key names in the ``dlq:compressed:`` namespace. The by-id
# read interpolates ``entry_id`` into ``dlq:compressed:{entry_id}``; an
# ``entry_id`` colliding with a structural key (a sorted set) makes the raw
# ``GET`` raise ``WRONGTYPE``, which ``get_blob`` cannot tell apart from a
# connectivity failure — it would flip the whole backend into degraded mode
# from a read-only viewer request. Declared beside the key constants so adding
# a key and reserving it are one edit (registration ratchet). ``index`` is the
# lone bare name; the ``by_domain:`` / ``status:`` / ``status_domain:``
# families are prefix-reserved.
_COMPRESSED_RESERVED_NAMES = frozenset({"index"})
_COMPRESSED_RESERVED_PREFIXES = ("by_domain:", "status:", "status_domain:")

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
        """Store compressed entry as a STRING/blob + the index sorted sets.

        Mirrors the main-entry write: the summary dict is encoded to a single
        ``bytes`` blob (``fast_dumps``) and the blob + every index ``zadd`` are
        issued as one ``batch_write_ops`` call — all-or-nothing in normal mode
        (1 RTT), one fsync in degraded mode. ``set_blob`` writes to the bounded
        blob store, matching main-entry degraded semantics. The inner
        ``sample_context`` keeps its JSON-string form so the unchanged
        deserializer can ``fast_loads`` it.

        D1: alongside the permanent all-statuses ``index`` / ``by_domain`` sets
        the write now also maintains the per-status ``status:{status}`` set and
        the composite ``status_domain:{status}:{domain}`` set (skipped when
        ``domain`` is empty, mirroring the main DLQ's empty-domain guard), all
        scored by ``compressed_at``. Still one pipeline round trip — the extra
        ops cost bandwidth, not RTT. Blob-first ordering preserved so a prefix
        application never leaves an index member without its blob.
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
        status_key = f"{_COMPRESSED_STATUS_PREFIX}{entry.status}"
        ops: list[tuple[str, str, Any]] = [
            ("set_blob", key, encoded),
            ("zadd", _COMPRESSED_INDEX_KEY, {entry.id: score}),
            ("zadd", domain_key, {entry.id: score}),
            ("zadd", status_key, {entry.id: score}),
        ]
        if entry.domain:
            composite_key = (
                f"{_COMPRESSED_STATUS_DOMAIN_PREFIX}{entry.status}:{entry.domain}"
            )
            ops.append(("zadd", composite_key, {entry.id: score}))
        self._backend.batch_write_ops(ops)
        return True

    def get_compressed_entry(self, entry_id: str) -> DLQCompressedEntry | None:
        """Return a single compressed entry by id via a direct blob read (D2).

        Index-free: the blob is stored at ``dlq:compressed:{id}``, so this is
        one ``get_blob`` regardless of index size — and it returns entries the
        newest-first listing window would miss (the G3 404-despite-existing
        bug). Returns ``None`` on absence, matching the sibling ``get_by_id``.

        A reserved ``entry_id`` (one that would collide with a structural key
        of the namespace) returns ``None`` without a Redis read: interpolating
        it into the key would make ``get_blob`` hit a sorted set and raise
        ``WRONGTYPE``, which the backend answers by degrading the whole
        process. Returning ``None`` reproduces the 404 those ids already
        produce, so there is no observable contract change.
        """
        from baldur.utils.serialization import fast_loads

        if _is_reserved_compressed_id(entry_id):
            return None

        blob = self._backend.get_blob(f"{_COMPRESSED_PREFIX}{entry_id}")
        if blob is None:
            return None
        return _deserialize_compressed_entry(fast_loads(blob))

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

    def get_compressed_entries_before(
        self,
        *,
        status: str,
        before: datetime,
        limit: int = 100,
        offset: int = 0,
        after: datetime | None = None,
    ) -> list[DLQCompressedEntry]:
        """Query compressed entries in a cutoff window, oldest first.

        The index is scored by ``compressed_at``, so the window is a score
        range walked ascending, and the first entry at or after ``before`` ends
        the scan.

        ``after`` bounds the window from below, which is what keeps a paging
        caller off its own processed prefix. ``status`` lives in the entry blob
        and cannot be filtered index-side, so a scan that always started at the
        index head would re-read every already-transitioned entry — one round
        trip each — on every page, turning a drain into quadratic work.

        ``offset`` then only has to cover the boundary the score cannot
        express: matching entries at exactly ``after`` that the caller left in
        place. It counts matching entries, as it does on the SQL and memory
        adapters.
        """
        from baldur.utils.serialization import fast_loads

        entries: list[DLQCompressedEntry] = []
        matched = 0
        cursor = 0
        page_size = max(limit, 1)
        min_score = after.timestamp() if after is not None else float("-inf")
        max_score = before.timestamp()

        while len(entries) < limit:
            member_ids = self._backend.zrangebyscore(
                _COMPRESSED_INDEX_KEY,
                min_score,
                max_score,
                offset=cursor,
                count=page_size,
            )
            if not member_ids:
                break
            cursor += len(member_ids)

            for member_id in member_ids:
                blob = self._backend.get_blob(f"{_COMPRESSED_PREFIX}{member_id}")
                if blob is None:
                    continue
                entry = _deserialize_compressed_entry(fast_loads(blob))
                if entry.compressed_at >= before:
                    # Ascending order: nothing beyond this point is eligible.
                    return entries
                if entry.status != status:
                    continue
                matched += 1
                if matched <= offset:
                    continue
                entries.append(entry)
                if len(entries) >= limit:
                    break

        return entries

    def get_compressed_summary(self) -> dict[str, Any]:
        """Aggregate statistics of compressed entries.

        ``total_summaries`` is an O(1) ``zcard``. The ``by_status`` /
        ``total_compressed_items`` aggregates need every member's status and
        count, so the walk fetches blobs through ``get_blobs`` in chunks of
        ``_SUMMARY_MGET_CHUNK`` — exact, but with the round trips collapsed
        ~500x versus one ``get_blob`` per member (D4). A 50,000-entry index
        goes from 50,000 round trips to 100.

        A cap rail bounds the walk at ``compress_summary_scan_cap``: above it
        the walk covers the newest ``cap`` entries, the response carries
        ``summary_truncated`` and a WARNING is logged (D7). Below the cap —
        every realistic PRO deployment — the summary stays exact.
        """
        from baldur.utils.serialization import fast_loads

        total = self._backend.zcard(_COMPRESSED_INDEX_KEY)
        cap = _summary_scan_cap()
        truncated = total > cap

        # Newest-first, bounded by the cap. ``zrevrange(0, -1)`` walks every
        # member; ``zrevrange(0, cap - 1)`` keeps the newest ``cap`` when the
        # index is oversized.
        stop = cap - 1 if truncated else -1
        member_ids = self._backend.zrevrange(_COMPRESSED_INDEX_KEY, 0, stop)

        status_counts: dict[str, int] = {s.value: 0 for s in DLQCompressedStatus}
        total_compressed_items = 0

        for start in range(0, len(member_ids), _SUMMARY_MGET_CHUNK):
            chunk = member_ids[start : start + _SUMMARY_MGET_CHUNK]
            keys = [f"{_COMPRESSED_PREFIX}{member_id}" for member_id in chunk]
            for blob in self._backend.get_blobs(keys):
                if blob is None:
                    continue
                data = fast_loads(blob)
                st = data.get("status", DLQCompressedStatus.ACTIVE.value)
                status_counts[st] = status_counts.get(st, 0) + 1
                total_compressed_items += int(data.get("count", 0))

        summary: dict[str, Any] = {
            "total_summaries": total,
            "total_compressed_items": total_compressed_items,
            "by_status": status_counts,
        }
        if truncated:
            summary["summary_truncated"] = True
            logger.warning(
                "dlq.compressed_summary_truncated",
                total=total,
                cap=cap,
            )
        return summary

    def update_compressed_status(self, entry_id: str, new_status: str) -> bool:
        """Transition compressed entry lifecycle status.

        Rewrites the STRING/blob (GET → decode → mutate → encode) and relocates
        the entry's per-status / composite index membership to match. The
        permanent all-statuses ``index`` / ``by_domain`` sets are scored by
        ``compressed_at`` and are never touched here.

        Op order is blob-FIRST, then add-before-remove (D1 as amended by
        723 D9):
        ``[set_blob, zadd new-status, zadd new-composite, zrem old-status,
        zrem old-composite]``. Blob-first so a crash prefix that stops after
        ``set_blob`` leaves the blob naming the intended status with the
        indexes merely lagging (discoverable and repairable) rather than a blob
        still naming the old status while membership is new-key-only.
        Add-before-remove so every crash prefix keeps the entry in at least one
        per-status index (worst case transient dual membership), never absent
        from all of them.

        A **same-status** transition short-circuits to a blob-only write. This
        is required for correctness, not an optimization: with equal statuses
        the ``zadd``/``zrem`` address the same key and member, and a pipeline
        applies ops in order, so ``zadd status:X`` followed by
        ``zrem status:X`` annihilates — the entry would end in no per-status
        set at all. Reachable when two sweep executions overlap on one entry.
        """
        from baldur.utils.serialization import fast_dumps, fast_loads

        key = f"{_COMPRESSED_PREFIX}{entry_id}"
        blob = self._backend.get_blob(key)
        if blob is None:
            return False

        data = fast_loads(blob)
        old_status = data.get("status", DLQCompressedStatus.ACTIVE.value)
        now = utc_now().isoformat()
        data["status"] = new_status

        if new_status == DLQCompressedStatus.STALE.value:
            data["stale_at"] = now
        elif new_status == DLQCompressedStatus.ARCHIVED.value:
            data["archived_at"] = now

        encoded = fast_dumps(data)

        if new_status == old_status:
            # Blob-only: an add-then-remove on the same per-status key/member
            # in one pipeline would annihilate the membership (see docstring).
            self._backend.set_blob(key, encoded)
            return True

        domain = data.get("domain", "")
        score = _compressed_score(data)

        ops: list[tuple[str, str, Any]] = [
            ("set_blob", key, encoded),
            ("zadd", f"{_COMPRESSED_STATUS_PREFIX}{new_status}", {entry_id: score}),
        ]
        if domain:
            ops.append(
                (
                    "zadd",
                    f"{_COMPRESSED_STATUS_DOMAIN_PREFIX}{new_status}:{domain}",
                    {entry_id: score},
                )
            )
        ops.append(("zrem", f"{_COMPRESSED_STATUS_PREFIX}{old_status}", [entry_id]))
        if domain:
            ops.append(
                (
                    "zrem",
                    f"{_COMPRESSED_STATUS_DOMAIN_PREFIX}{old_status}:{domain}",
                    [entry_id],
                )
            )
        self._backend.batch_write_ops(ops)
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


def _is_reserved_compressed_id(entry_id: str) -> bool:
    """True when ``entry_id`` would collide with a structural key in the
    ``dlq:compressed:`` namespace (see ``_COMPRESSED_RESERVED_*``)."""
    return entry_id in _COMPRESSED_RESERVED_NAMES or entry_id.startswith(
        _COMPRESSED_RESERVED_PREFIXES
    )


def _summary_scan_cap() -> int:
    """Resolve the configured summary scan cap (re-read each call).

    Falls back to 5000 — matching the ``DLQSettings`` field default — if
    settings are unreachable, mirroring ``dlq.py``'s cardinality-alert
    threshold resolution.
    """
    try:
        from baldur.settings.dlq import get_dlq_settings

        return int(get_dlq_settings().compress_summary_scan_cap)
    except Exception:
        return 5000


def _compressed_score(data: dict) -> float:
    """Epoch score for a compressed entry's per-status index membership.

    Uses ``compressed_at`` (the same "recently created, not recently
    transitioned" choice as the main DLQ), falling back to now if the blob
    carries no parseable timestamp.
    """
    raw = data.get("compressed_at")
    try:
        return datetime.fromisoformat(raw).timestamp() if raw else utc_now().timestamp()
    except (ValueError, TypeError):
        return utc_now().timestamp()


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
