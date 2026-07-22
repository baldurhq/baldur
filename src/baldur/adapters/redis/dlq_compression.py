"""
Redis DLQ Compression — compressed entry storage and management.

Extracted from RedisDLQRepository for single-responsibility.
"""

from __future__ import annotations

from dataclasses import dataclass, field
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
# on the write path (store + transition) and read by the status-filtered
# listing and the lifecycle sweep once the completion marker is set. See D1.
_COMPRESSED_STATUS_PREFIX = "dlq:compressed:status:"
_COMPRESSED_STATUS_DOMAIN_PREFIX = "dlq:compressed:status_domain:"

# Migration signalling for the per-status family (D5). Both are blobs.
#
# ``_COMPRESSED_MARKER_KEY`` presence means the per-status family covered
# every index member as of a verified walk. It gates every read that routes to
# those keys: absent — or unreadable — routes back to the pre-migration shape,
# so the failure direction is always "as slow as before", never "fast and
# silently incomplete".
#
# ``_COMPRESSED_WATERMARK_KEY`` holds ``{added, walk_time,
# reconciled_through_score}``: the last verified full walk's stability record
# plus the highest score any verified scan has reconciled. The score anchors
# the post-marker repair scan's window to observed progress rather than to
# wall clock or release version — a rollback freezes the watermark, so the
# first scan after re-upgrade covers the whole rolled-back window.
_COMPRESSED_MARKER_KEY = "dlq:compressed:status_index_ready"
_COMPRESSED_WATERMARK_KEY = "dlq:compressed:backfill_watermark"

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
_COMPRESSED_RESERVED_NAMES = frozenset(
    {"index", "status_index_ready", "backfill_watermark"}
)
_COMPRESSED_RESERVED_PREFIXES = ("by_domain:", "status:", "status_domain:")

# Lower bound slack for the post-marker repair scan (D12). Widens the walked
# score window below the reconciliation watermark so a writer whose clock runs
# behind, or an entry constructed well before it was stored, still falls inside
# it. A gap older than this is healed by ``baldur dlq migrate-compressed``,
# which is the documented operator action for it.
_BACKFILL_RESCAN_SLACK_SECONDS = 86400

# Floor on the span between the two zero-add walks that conclude the migration
# (D12). Two walks in quick succession prove nothing about a rolling upgrade
# still in progress: the old-code writers that would break coverage may simply
# not have written yet. Against the daily sweep cadence the effective span is
# ~24h; the floor only stops manually-triggered back-to-back runs from
# concluding early.
_BACKFILL_STABILITY_MIN_SECONDS = 21600

# Ceiling on the entry IDs named in one ``dlq.compressed_index_repaired``
# event. The IDs are a debugging lead ("which entries, and why is the blob
# unreadable"), not a manifest — the members keep their ``index`` /
# ``by_domain`` membership, so the authoritative list is always recoverable by
# walking the index. Same wire-size rationale as ``_AUDIT_SOURCE_IDS_CAP``.
_REPAIR_REMOVED_IDS_SAMPLE_CAP = 20

# Defensive ceiling on the number of source IDs embedded in a single compress
# audit event. Bounds the event's wire size at emit so an oversized batch does
# not exceed collector/transport message-size limits. A hard safety rail, not a
# tunable setting — the authoritative set identity (``source_ids_hash`` +
# ``source_count``) is always emitted in full; truncation drops only the
# forensic-convenience ID list.
_AUDIT_SOURCE_IDS_CAP = 5000


@dataclass
class _CutoffWalk:
    """State a cutoff walk threads through its chunks.

    ``matched`` counts entries matching the walked status, which is what
    ``offset`` steps over — index positions would drift by however many
    non-matching members the page happened to contain. ``relocate`` and
    ``unreadable`` accumulate repairs to issue after the walk ends;
    ``repairing`` is false on the pre-migration route, where a status
    disagreement is expected rather than a defect.
    """

    status: str
    before: datetime
    limit: int
    offset: int
    repairing: bool
    matched: int = 0
    entries: list[DLQCompressedEntry] = field(default_factory=list)
    relocate: list[tuple[str, str, str, float]] = field(default_factory=list)
    unreadable: list[str] = field(default_factory=list)


class RedisDLQCompression:
    """DLQ compression operations."""

    def __init__(self, repository: RedisDLQRepository) -> None:
        self._repo = repository
        # Only a *positive* marker observation is cached: completion is
        # monotonic, so once the per-status family has covered the index it
        # never stops covering it (every later store self-indexes). A negative
        # observation is re-read per call — one GET in front of the legacy
        # walk it gates is noise against that walk's cost.
        self._status_index_ready = False
        # Set by a verified full backfill walk in this process. The sweep
        # honours it so a run that just proved coverage itself does not also
        # pay the legacy walk; listings never honour it (a console read must
        # not depend on which worker served it).
        self._verified_full_walk = False

    @property
    def _backend(self):
        return self._repo._backend

    # =========================================================================
    # Migration signalling (D5)
    # =========================================================================

    def _is_status_index_ready(self) -> bool:
        """True when the per-status index family is known to cover the index.

        A read failure is reported as "not ready": the backend answers a
        failed read by degrading and serving its process-local view, so an
        absent-looking marker is indistinguishable from a real absence — and
        the safe interpretation of both is the pre-migration route.
        """
        if self._status_index_ready:
            return True
        try:
            blob = self._backend.get_blob(_COMPRESSED_MARKER_KEY)
        except Exception:
            return False
        if blob is None:
            return False
        self._status_index_ready = True
        return True

    def _read_watermark(self) -> dict[str, Any]:
        """Return the reconciliation watermark, or an empty record."""
        from baldur.utils.serialization import fast_loads

        try:
            blob = self._backend.get_blob(_COMPRESSED_WATERMARK_KEY)
            if blob is None:
                return {}
            record = fast_loads(blob)
            return record if isinstance(record, dict) else {}
        except Exception:
            return {}

    def _degrade_epoch(self) -> int:
        """Sample the backend's degrade counter (0 when it exposes none)."""
        return int(getattr(self._backend, "degrade_count", 0))

    def _redis_available(self) -> bool:
        """Report backend availability, defaulting to available."""
        return bool(getattr(self._backend, "is_redis_available", True))

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
        """Query compressed entries from Redis sorted set index (D1).

        The key the ``limit`` window is taken from depends on the filter:

        ==========  ========  ==========================  ================
        domain      status    key                         marker-gated
        ==========  ========  ==========================  ================
        --          --        ``index``                   no
        d           --        ``by_domain:{d}``           no
        --          s         ``status:{s}``              yes
        d           s         ``status_domain:{s}:{d}``   yes
        ==========  ========  ==========================  ================

        All four are scored by ``compressed_at``, so the newest-first listing
        order is the same whichever one answers. The unfiltered rows never
        consult the marker — the all-statuses family is complete by
        construction. The filtered rows do, because the per-status family only
        covers entries compressed after it started being maintained until the
        migration has walked the rest; before then they fall back to slicing
        the all-statuses key and filtering afterwards, which is what makes an
        archived listing return almost nothing on an install whose archive is
        older than the newest page. That is the pre-migration behaviour, kept
        deliberately: the alternative failure — serving a *silently* partial
        filtered page off an incomplete index — is worse than serving a slow
        one.

        The blob's status is re-checked on every route, not just the legacy
        one. A crash between an entry's index writes can leave it in a key its
        blob disagrees with; such a member is dropped here (never served under
        the wrong filter) and repaired by the lifecycle sweep's walk. This
        method never writes — a console read must not pay a repair's cost.
        """
        from baldur.utils.serialization import fast_loads

        if status and self._is_status_index_ready():
            key = (
                f"{_COMPRESSED_STATUS_DOMAIN_PREFIX}{status}:{domain}"
                if domain
                else f"{_COMPRESSED_STATUS_PREFIX}{status}"
            )
        elif domain:
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

        Once the per-status family is known complete (D3) the walk moves onto
        ``status:{status}`` and the ``after``/``offset``/``limit`` contract
        carries over unchanged: a transition removes the entry from the walked
        key, which is the same effect the blob-status filter had on the
        all-statuses walk, so ``offset`` still counts left-behind *matching*
        entries at the cursor. What changes is what the walk no longer touches
        — the terminal ARCHIVED prefix is not in the walked key at all, so it
        stops being re-read once per run for the lifetime of the install.

        Blobs are fetched one chunk per round trip rather than one per member
        (D13). That widens the blast radius of a failed read from a single
        member to a whole chunk, and the caller acts on what this returns — so
        a chunk whose fetch coincided with the backend leaving REDIS mode
        contributes nothing: no entries, no repair. The walk continues past
        it, and the skipped members keep their membership and their blob, so
        the next run picks them up.
        """
        use_per_status = self._verified_full_walk or self._is_status_index_ready()
        walked_key = (
            f"{_COMPRESSED_STATUS_PREFIX}{status}"
            if use_per_status
            else _COMPRESSED_INDEX_KEY
        )
        walk = _CutoffWalk(
            status=status,
            before=before,
            limit=limit,
            offset=offset,
            repairing=use_per_status,
        )
        # Epoch first, availability second — a degrade landing between the two
        # reads must be caught by the availability read, not hidden by a
        # sample taken after the bump it would have detected.
        walk_epoch = self._degrade_epoch()
        walk_started_available = self._redis_available()
        cursor = 0
        page_size = max(limit, 1)
        min_score = after.timestamp() if after is not None else float("-inf")
        max_score = before.timestamp()

        try:
            while len(walk.entries) < limit:
                member_ids = self._backend.zrangebyscore(
                    walked_key,
                    min_score,
                    max_score,
                    offset=cursor,
                    count=page_size,
                )
                if not member_ids:
                    break
                cursor += len(member_ids)

                if any(
                    self._consume_walk_chunk(
                        walk, member_ids[start : start + _SUMMARY_MGET_CHUNK]
                    )
                    for start in range(0, len(member_ids), _SUMMARY_MGET_CHUNK)
                ):
                    break

            return walk.entries
        finally:
            walk_verified = (
                walk_started_available and self._degrade_epoch() == walk_epoch
            )
            self._flush_index_repair(
                walk.relocate,
                walk.unreadable if walk_verified else [],
            )

    def _consume_walk_chunk(self, walk: _CutoffWalk, chunk: list[str]) -> bool:
        """Fold one chunk into a cutoff walk; True ends the whole walk."""
        from baldur.utils.serialization import fast_loads

        epoch_before = self._degrade_epoch()
        blobs = self._backend.get_blobs(
            [f"{_COMPRESSED_PREFIX}{member_id}" for member_id in chunk]
        )
        # A degrade across this fetch means every blob in it may have come from
        # the process-local store: too weak to transition on, and far too weak
        # to repair from.
        chunk_verified = self._degrade_epoch() == epoch_before
        collect_repairs = walk.repairing and chunk_verified

        for member_id, blob in zip(chunk, blobs, strict=True):
            if blob is None:
                if collect_repairs:
                    walk.unreadable.append(member_id)
                continue
            entry = _deserialize_compressed_entry(fast_loads(blob))
            if entry.compressed_at >= walk.before:
                # Ascending order: nothing beyond this point is eligible.
                return True
            if entry.status != walk.status:
                if collect_repairs:
                    walk.relocate.append(
                        (
                            member_id,
                            entry.status,
                            entry.domain,
                            entry.compressed_at.timestamp(),
                        )
                    )
                continue
            if not chunk_verified:
                continue
            walk.matched += 1
            if walk.matched <= walk.offset:
                continue
            walk.entries.append(entry)
            if len(walk.entries) >= walk.limit:
                return True
        return False

    def _flush_index_repair(
        self,
        relocate: list[tuple[str, str, str, float]],
        unreadable: list[str],
    ) -> None:
        """Apply the repairs a walk collected, after the walk has finished.

        Issued here rather than inside the paging loop on purpose: the walk
        pages by position, and a ``zrem`` mid-loop shifts every later member
        left, so the next page's offset would step over entries.

        A **relocation** moves a member whose blob disagrees with the key it
        was found in. The blob is the truth — with the blob-first transition
        order the only reachable disagreement is a blob that has moved ahead
        of its indexes, so the repair completes an interrupted transition
        rather than undoing one. Add first, then blindly remove from the other
        statuses' keys: an absent member makes ``zrem`` a no-op, so no read is
        needed to know where the member used to be.

        An **unreadable** member is dropped from the per-status keys only. The
        permanent ``index`` / ``by_domain`` family is never touched, so this is
        not a delete — the ID stays recoverable by walking the index — it just
        stops the member occupying a page slot and a ``zcard`` count forever.
        The caller gates this half on the walk having stayed on Redis
        throughout: a blip serves a whole chunk from the process-local blob
        store, which returns ``None`` for anything it has evicted, and those
        members are alive.

        Fail-open: a repair that does not land leaves the strays for the next
        walk, and the read paths filter them out meanwhile.
        """
        if not relocate and not unreadable:
            return

        all_statuses = [s.value for s in DLQCompressedStatus]
        try:
            for member_id, blob_status, domain, score in relocate:
                ops: list[tuple[str, str, Any]] = [
                    (
                        "zadd",
                        f"{_COMPRESSED_STATUS_PREFIX}{blob_status}",
                        {member_id: score},
                    )
                ]
                if domain:
                    ops.append(
                        (
                            "zadd",
                            f"{_COMPRESSED_STATUS_DOMAIN_PREFIX}{blob_status}:{domain}",
                            {member_id: score},
                        )
                    )
                for other in all_statuses:
                    if other == blob_status:
                        continue
                    ops.append(
                        ("zrem", f"{_COMPRESSED_STATUS_PREFIX}{other}", [member_id])
                    )
                    if domain:
                        ops.append(
                            (
                                "zrem",
                                f"{_COMPRESSED_STATUS_DOMAIN_PREFIX}{other}:{domain}",
                                [member_id],
                            )
                        )
                self._backend.batch_write_ops(ops)

            for member_id in unreadable:
                # Domain is unknown (the blob is what carries it), so only the
                # global per-status keys can be addressed. The composite keys
                # keep a cosmetic residue, which the domain-scoped read's
                # blob check already filters out.
                for st in all_statuses:
                    self._backend.zrem(f"{_COMPRESSED_STATUS_PREFIX}{st}", member_id)

            logger.info(
                "dlq.compressed_index_repaired",
                relocated=len(relocate),
                removed_unreadable=len(unreadable),
                removed_unreadable_ids=unreadable[:_REPAIR_REMOVED_IDS_SAMPLE_CAP],
            )
        except Exception as exc:
            logger.warning(
                "dlq.compressed_index_repair_failed",
                relocated=len(relocate),
                removed_unreadable=len(unreadable),
                error=str(exc),
            )

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

        Once the per-status index family is known complete, ``by_status``
        comes from three cardinality reads instead of the walk (D6): constant
        cost at any volume, and exact *above* the cap where the walk is only a
        window. ``summary_truncated`` then describes
        ``total_compressed_items`` alone, which keeps the walk permanently —
        summing item counts needs the blobs.

        Expect ``total_summaries`` and ``sum(by_status)`` to differ slightly
        once ``by_status`` is answered this way. Two populations account for
        the gap and both are transient: an entry caught mid-transition can sit
        in two status keys at once (counted twice), and an entry whose blob is
        unreadable is counted here where the walk skipped it. The lifecycle
        sweep's walk repairs both. A gap that *persists* across sweep runs is
        a real signal — strays whose keys no sweep lane reaches, or an
        unreadable-blob population that is not being cleared — and is worth
        investigating rather than ignoring.
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

        if self._is_status_index_ready():
            status_counts = {
                s.value: self._backend.zcard(f"{_COMPRESSED_STATUS_PREFIX}{s.value}")
                for s in DLQCompressedStatus
            }

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

    def backfill_compressed_status_index(
        self,
        *,
        operator_initiated: bool = False,
    ) -> dict[str, Any]:
        """Reconcile the per-status index family with the stored entries (D4).

        Entries compressed before the per-status family started being
        maintained live only in the all-statuses ``index``. A status-filtered
        read routed to the per-status keys would not see them at all, so this
        walk is what makes that routing safe to switch on — not an
        optimisation, a precondition.

        Two modes, both ``zadd``-only. Re-adding a member with its own score
        is a no-op, so either mode is safe to run any number of times, and
        neither ever removes anything or touches the all-statuses family.

        **Full** (the marker is unset, and always for an operator run) pages
        the whole index by position. The index is append-only and scored by
        write time, so a concurrent store lands at the tail: the worst a
        clock-skewed writer can do is shift members right and have this read
        one twice, never skip one.

        **Tail** (post-marker) re-scans from the reconciliation watermark
        minus a day's slack. This is the net for gaps a one-shot migration
        cannot see: a store whose pipeline was applied in part before the
        process died, an old-code worker still writing during a rolling
        upgrade, and every write during a rollback window — the watermark
        freezes while rolled-back code runs, so the first scan after
        re-upgrade covers the whole window however long it lasted. Anchoring
        the window to wall clock instead would let a sweep outage age a gap
        out of it permanently.

        Progress is only recorded when the scan is *verified*: available
        before the first page and no degrade transition across the whole walk.
        An unverified scan's ``zadd``s are still welcome — they are
        idempotent, and some of them reached Redis — but its counts describe a
        process-local view rather than the index, so concluding anything from
        them is exactly how a migration reports success on an install where
        nothing was reconciled.

        Concluding the migration needs more than one verified walk: this walk
        must have added nothing (which *is* the coverage property, observed
        rather than inferred — every member already held its per-status
        membership when the walk passed it), and a previous zero-add walk must
        be at least ``_BACKFILL_STABILITY_MIN_SECONDS`` old. The wait exists
        because a rolling upgrade's old-code workers write no per-status
        membership, and a walk cannot see a worker that has not written yet.
        An operator run skips the wait: it is the automatic substitute for the
        operator's own judgement that the upgrade has finished, and on an
        operator run that judgement has already been made.
        """
        marker_present = self._is_status_index_ready()
        watermark = self._read_watermark()

        # Epoch sample strictly before the availability read. Reversed, a
        # degrade landing between them yields a post-bump "before" sample and
        # an availability read taken while still up — after which every page
        # can be served from memory with no further bump, and a walk that
        # reconciled nothing looks like a walk that found nothing to do.
        epoch_start = self._degrade_epoch()
        started_available = self._redis_available()

        mode = "full" if (operator_initiated or not marker_present) else "tail"
        source = "cli" if operator_initiated else "sweep"
        walked = added = skipped_unreadable = 0
        max_score_seen = float("-inf")

        try:
            for chunk in self._backfill_pages(mode, watermark):
                page_walked, page_added, page_skipped, page_max = self._reconcile_chunk(
                    chunk
                )
                walked += page_walked
                added += page_added
                skipped_unreadable += page_skipped
                max_score_seen = max(max_score_seen, page_max)
        except Exception as exc:
            logger.warning(
                "dlq.compressed_backfill_failed",
                mode=mode,
                walked=walked,
                added=added,
                degrade_delta=self._degrade_epoch() - epoch_start,
                error=str(exc),
            )
            return {
                "complete": marker_present,
                "mode": mode,
                "walked": walked,
                "added": added,
                "skipped_unreadable": skipped_unreadable,
                "verified": False,
                "marker_set": marker_present,
            }

        verified = (
            started_available
            and self._degrade_epoch() == epoch_start
            and self._redis_available()
        )
        marker_set = marker_present
        if verified:
            if mode == "full":
                self._verified_full_walk = True
            marker_set = self._record_backfill_progress(
                mode=mode,
                watermark=watermark,
                added=added,
                max_score_seen=max_score_seen,
                marker_present=marker_present,
                operator_initiated=operator_initiated,
                source=source,
            )
        else:
            logger.warning(
                "dlq.compressed_backfill_failed",
                mode=mode,
                walked=walked,
                added=added,
                degrade_delta=self._degrade_epoch() - epoch_start,
                reason="scan_not_verified",
            )

        logger.info(
            "dlq.compressed_backfill_completed",
            mode=mode,
            walked=walked,
            added=added,
            skipped_unreadable=skipped_unreadable,
            verified=verified,
        )
        return {
            "complete": marker_set,
            "mode": mode,
            "walked": walked,
            "added": added,
            "skipped_unreadable": skipped_unreadable,
            "verified": verified,
            "marker_set": marker_set,
        }

    def _backfill_pages(self, mode: str, watermark: dict):
        """Yield index pages for the requested scan mode.

        Full mode pages by position over the append-only index; tail mode
        pages the score range from the reconciliation watermark, widened by a
        slack allowance, up to the head.
        """
        if mode == "full":
            position = 0
            while True:
                chunk = self._backend.zrange(
                    _COMPRESSED_INDEX_KEY,
                    position,
                    position + _SUMMARY_MGET_CHUNK - 1,
                )
                if not chunk:
                    return
                position += len(chunk)
                yield chunk
            return

        floor = watermark.get("reconciled_through_score")
        min_score = (
            float(floor) - _BACKFILL_RESCAN_SLACK_SECONDS
            if floor is not None
            else float("-inf")
        )
        cursor = 0
        while True:
            chunk = self._backend.zrangebyscore(
                _COMPRESSED_INDEX_KEY,
                min_score,
                float("inf"),
                offset=cursor,
                count=_SUMMARY_MGET_CHUNK,
            )
            if not chunk:
                return
            cursor += len(chunk)
            yield chunk

    def _reconcile_chunk(self, chunk: list[str]) -> tuple[int, int, int, float]:
        """File one chunk of members under the status their blob carries.

        Returns ``(walked, added, skipped_unreadable, highest_score)``. The
        ``zadd``s are grouped per destination key so the call count is a
        handful per chunk rather than one per member, and so the new-element
        counts Redis returns can be summed — that sum is the signal the
        completion criterion reads.
        """
        from baldur.utils.serialization import fast_loads

        blobs = self._backend.get_blobs(
            [f"{_COMPRESSED_PREFIX}{member_id}" for member_id in chunk]
        )
        grouped: dict[str, dict[str, float]] = {}
        walked = 0
        skipped_unreadable = 0
        max_score = float("-inf")

        for member_id, blob in zip(chunk, blobs, strict=True):
            walked += 1
            if blob is None:
                # Invisible to every reader as it stands, so leaving it out of
                # the per-status keys loses nothing.
                skipped_unreadable += 1
                continue
            data = fast_loads(blob)
            entry_status = data.get("status", DLQCompressedStatus.ACTIVE.value)
            score = _compressed_score(data)
            max_score = max(max_score, score)
            grouped.setdefault(f"{_COMPRESSED_STATUS_PREFIX}{entry_status}", {})[
                member_id
            ] = score
            domain = data.get("domain", "")
            if domain:
                composite = f"{_COMPRESSED_STATUS_DOMAIN_PREFIX}{entry_status}:{domain}"
                grouped.setdefault(composite, {})[member_id] = score

        added = 0
        for key, mapping in grouped.items():
            added += int(self._backend.zadd(key, mapping) or 0)
        return walked, added, skipped_unreadable, max_score

    def _record_backfill_progress(
        self,
        *,
        mode: str,
        watermark: dict,
        added: int,
        max_score_seen: float,
        marker_present: bool,
        operator_initiated: bool,
        source: str,
    ) -> bool:
        """Persist a verified scan's progress. Returns the marker's state.

        Only ever called for a verified scan, which is what makes a degraded
        write here harmless: whatever it records was already true when the
        scan ended, so a write that survives only in the write-ahead log
        publishes a true fact when it replays. A stale replayed watermark can
        only move the reconciliation point backwards, which widens the next
        scan's window — more idempotent re-reading, never a missed member.
        """
        from baldur.utils.serialization import fast_dumps

        now = utc_now()
        record = dict(watermark)
        if mode == "full":
            chain_open = (
                watermark.get("added") == 0 and watermark.get("walk_time") is not None
            )
            record["added"] = added
            if added or not chain_open:
                # A walk that added members restarts the stability chain; a
                # zero-add walk continuing an open chain keeps the chain's
                # start time, so the floor measures the chain's span rather
                # than the gap between the last two runs.
                record["walk_time"] = now.isoformat()
        if max_score_seen > float(
            record.get("reconciled_through_score", float("-inf"))
        ):
            record["reconciled_through_score"] = max_score_seen

        marker_set = marker_present
        if not marker_present and (
            operator_initiated
            or (added == 0 and _stability_chain_matured(watermark, now))
        ):
            self._backend.set_blob(
                _COMPRESSED_MARKER_KEY,
                fast_dumps({"stamped_at": now.isoformat(), "source": source}),
            )
            self._status_index_ready = True
            marker_set = True
            logger.info("dlq.compressed_index_ready", source=source)

        self._backend.set_blob(_COMPRESSED_WATERMARK_KEY, fast_dumps(record))
        return marker_set

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


def _stability_chain_matured(watermark: dict, now: datetime) -> bool:
    """True when a previous zero-add walk is old enough to conclude coverage.

    The chain starts at the first verified zero-add walk and is reset by any
    walk that adds members. Maturity is measured from that start, not from the
    previous run, so a sweep triggered repeatedly by hand cannot walk its way
    to a conclusion inside a live rolling upgrade.
    """
    if watermark.get("added") != 0:
        return False
    raw = watermark.get("walk_time")
    if not raw:
        return False
    try:
        started = datetime.fromisoformat(raw)
    except (ValueError, TypeError):
        return False
    return (now - started).total_seconds() >= _BACKFILL_STABILITY_MIN_SECONDS


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
