"""
Redis DLQ Query — query and search/filter operations.

Extracted from RedisDLQRepository for single-responsibility.
All methods receive the parent repository reference for access to
backend, keys, and helper methods.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

import structlog

from baldur.interfaces.repositories import (
    FailedOperationData,
    FailedOperationStatus,
)

if TYPE_CHECKING:
    from baldur.adapters.redis.dlq import RedisDLQRepository

logger = structlog.get_logger()

__all__ = ["RedisDLQQuery"]


class RedisDLQQuery:
    """DLQ query and search operations."""

    def __init__(self, repository: RedisDLQRepository) -> None:
        self._repo = repository
        # ZINTERCARD capability (Redis 7.0+). None = unprobed; cached after
        # the first scoped-facet call so a sub-7.0 deployment does not
        # retry-then-fail ZINTERCARD on every request (D4).
        self._zintercard_supported: bool | None = None
        # 544 D5: lazy-backfill caches. ``_composite_warmed`` holds the
        # ``(status, domain)`` pairs that have been verified (or freshly
        # materialized via ZINTERSTORE) to exist on Redis-side, so a
        # subsequent combo find/count/facet ZCARDs through the composite
        # without re-checking. ``_domains_registry_warmed`` gates the
        # one-time SCAN→ZADD warmup of ``dlq:domains`` from the legacy
        # ``by_domain:*`` keyspace.
        self._composite_warmed: set[tuple[str, str]] = set()
        self._domains_registry_warmed: bool = False

    @property
    def _backend(self):
        return self._repo._backend

    def get_pending_by_domain(
        self,
        domain: str,
        limit: int = 100,
    ) -> list[FailedOperationData]:
        """Get pending operations for a specific domain.

        544 D8: normal mode routes through the composite ``(pending,
        domain)`` ZSET — windowed ZRANGE on the warm composite, with
        ``_warm_composite_if_needed`` lazily backfilling cold (s,d)
        pairs via EXISTS+ZINTERSTORE. Degraded mode keeps the legacy
        ``by_domain`` + Python ``status==PENDING`` filter (composite
        warmup requires the raw client, unavailable in degraded mode).
        """
        if not self._backend.is_degraded and self._warm_composite_if_needed(
            FailedOperationStatus.PENDING.value, domain
        ):
            composite_key = self._repo._status_domain_key(
                FailedOperationStatus.PENDING.value, domain
            )
            entry_ids = self._backend.zrange(composite_key, 0, limit - 1)
            results = []
            for entry_id in entry_ids:
                data = self._repo._decode_entry(self._repo._load_blob(entry_id))
                if data:
                    results.append(self._repo._to_data(data))
            return results

        # Degraded path (or raw client unavailable): legacy by_domain
        # ZSET + Python status filter.
        domain_key = f"{self._repo.BY_DOMAIN_PREFIX}{domain}"
        entry_ids = self._backend.zrange(domain_key, 0, limit - 1)

        results = []
        for entry_id in entry_ids:
            data = self._repo._decode_entry(self._repo._load_blob(entry_id))
            if data and data.get("status") == FailedOperationStatus.PENDING.value:
                results.append(self._repo._to_data(data))

        return results

    def get_pending_count_by_domain(self, domain: str) -> int:
        """Get count of pending operations for a domain.

        544 D8: normal mode is O(1) ``ZCARD`` on the composite
        ``(pending, domain)`` ZSET (no blob loads). Degraded mode
        delegates to the legacy method for the Python status filter.
        """
        if not self._backend.is_degraded and self._warm_composite_if_needed(
            FailedOperationStatus.PENDING.value, domain
        ):
            return self._backend.zcard(
                self._repo._status_domain_key(
                    FailedOperationStatus.PENDING.value, domain
                )
            )
        return len(self.get_pending_by_domain(domain, limit=10000))

    def by_status(
        self,
        status: str,
        limit: int = 100,
    ) -> list[FailedOperationData]:
        """Get entries by status."""
        if status == FailedOperationStatus.PENDING.value:
            return self._repo.get_pending(limit)

        if self._backend.is_degraded:
            return self._get_by_status_from_memory(status, limit)

        # Use per-status sorted set index when available (O(limit) vs O(N) SCAN)
        if status in self._repo._STATUS_INDEXED:
            return self._get_by_status_from_index(status, limit)

        return self._get_by_status_from_redis(status, limit)

    def _get_by_status_from_memory(
        self,
        status: str,
        limit: int,
    ) -> list[FailedOperationData]:
        """Get entries by status from in-memory storage (degraded mode)."""
        results = []
        for key, value in self._backend._memory.items():
            if not self._repo._is_valid_entry_key(key):
                continue
            data = self._repo._decode_entry(value)
            if data and data.get("status") == status:
                results.append(self._repo._to_data(data))
                if len(results) >= limit:
                    break
        return results

    def _get_by_status_from_index(
        self,
        status: str,
        limit: int,
    ) -> list[FailedOperationData]:
        """Get entries by status using sorted set index (O(limit))."""
        status_key = self._repo._status_key(status)
        entry_ids = self._backend.zrange(status_key, 0, limit - 1)

        results = []
        for entry_id in entry_ids:
            data = self._repo._decode_entry(self._repo._load_blob(entry_id))
            if data:
                results.append(self._repo._to_data(data))

        return results

    def _get_by_status_from_redis(
        self,
        status: str,
        limit: int,
    ) -> list[FailedOperationData]:
        """Get entries by status from Redis (normal mode)."""
        results = []
        try:
            # 538 D6: glob the dedicated entry namespace exactly. Every
            # special-key family lives outside dlq:entry:, so the glob matches
            # all and only entry blobs — no dlq:compressed:* key (a separate
            # namespace, STRING/blob-typed since 586) is returned, so the
            # GET-after-scan reads only entry blobs. The _is_valid_entry_key
            # filter (same positive whitelist used by the degraded scan) keeps
            # both enumeration paths in lockstep.
            pattern = f"{self._backend.config.key_prefix}{self._repo.ENTRY_PREFIX}*"
            cursor = 0

            while len(results) < limit:
                cursor, keys = self._backend.raw_redis_client.scan(
                    cursor, match=pattern, count=100
                )

                for key in keys:
                    if isinstance(key, bytes):
                        key = key.decode()

                    if not self._repo._is_valid_entry_key(key):
                        continue

                    blob = self._backend.raw_redis_client.get(key)
                    decoded = self._repo._decode_entry(blob)
                    if decoded and decoded.get("status") == status:
                        results.append(self._repo._to_data(decoded))
                        if len(results) >= limit:
                            break

                if cursor == 0:
                    break

        except Exception as e:
            logger.exception("redis_dlq.error", error=e)

        return results

    def count_by_status(self, status: str) -> int:
        """Count entries by status (O(1) for indexed statuses)."""
        if status == FailedOperationStatus.PENDING.value:
            return self._backend.zcard(self._repo.PENDING_KEY)

        if status in self._repo._STATUS_INDEXED:
            return self._backend.zcard(self._repo._status_key(status))

        # Fallback for non-indexed transient statuses (e.g. "replaying")
        return len(self.by_status(status, limit=10000))

    def find_by_status(
        self,
        status: str,
        domain: str | None = None,
        failure_type: str | None = None,
        limit: int = 100,
    ) -> list[FailedOperationData]:
        """Find operations by status with optional filters."""
        entries = self.by_status(status, limit=limit * 2)

        results = []
        for entry in entries:
            if domain and entry.domain != domain:
                continue
            if failure_type and entry.failure_type != failure_type:
                continue
            results.append(entry)
            if len(results) >= limit:
                break

        return results

    def _driving_index_key(
        self,
        status: str | None,
        domain: str | None,
        *,
        composite_warm: bool = False,
    ) -> str:
        """Pick the single-dimension index that drives a find/count.

        Precedence: composite ``(status, domain)`` when both present and
        the composite is warm (544 D8) → per-status index → by_domain
        index → status-independent global index. All scores carry the
        created_at epoch (541 D6), so a zrevrange over any is created_at
        DESC.
        """
        if status is not None and domain is not None and composite_warm:
            return self._repo._status_domain_key(status, domain)
        if status is not None:
            if status == FailedOperationStatus.PENDING.value:
                return self._repo.PENDING_KEY
            return self._repo._status_key(status)
        if domain is not None:
            return f"{self._repo.BY_DOMAIN_PREFIX}{domain}"
        return self._repo.ALL_KEY

    @staticmethod
    def _residual_filters(
        status: str | None,
        domain: str | None,
        failure_type: str | None,
        *,
        composite_warm: bool = False,
    ) -> list:
        """Predicates for dimensions the driving index does NOT satisfy.

        544 D8: when the composite (status, domain) ZSET drives the
        query, both status AND domain are resolved by the index, so the
        domain predicate drops. ``failure_type`` is never indexed and
        always stays a Python residual.
        """
        residual = []
        if status is not None and domain is not None and not composite_warm:
            residual.append(lambda e: e.domain == domain)
        if failure_type is not None:
            residual.append(lambda e: e.failure_type == failure_type)
        return residual

    def find(
        self,
        *,
        status: str | None = None,
        domain: str | None = None,
        failure_type: str | None = None,
        offset: int = 0,
        limit: int = 100,
    ) -> list[FailedOperationData]:
        """Paginated cross-status query ordered by created_at DESC.

        No residual filter → windowed zrevrange (O(limit)). With a residual
        filter (failure_type, or the legacy status+domain combo before
        composite warmup) → scan the full driving index and slice the
        *filtered* set, since the page must be computed over the
        intersection, not the raw index window (541 D6 / SB-016).

        544 D8: when both status and domain are set, attempt lazy
        warmup of the composite ``(status, domain)`` ZSET; if the
        composite is warm, drive directly through it and drop the
        domain residual — combo find becomes a windowed zrevrange.
        """
        composite_warm = (
            status is not None
            and domain is not None
            and not self._backend.is_degraded
            and self._warm_composite_if_needed(status, domain)
        )
        key = self._driving_index_key(status, domain, composite_warm=composite_warm)
        residual = self._residual_filters(
            status, domain, failure_type, composite_warm=composite_warm
        )

        if not residual:
            entry_ids = self._backend.zrevrange(key, offset, offset + limit - 1)
            results = []
            for entry_id in entry_ids:
                data = self._repo._decode_entry(self._repo._load_blob(entry_id))
                if data:
                    results.append(self._repo._to_data(data))
            return results

        entry_ids = self._backend.zrevrange(key, 0, -1)
        matched: list[FailedOperationData] = []
        target = offset + limit
        for entry_id in entry_ids:
            data = self._repo._decode_entry(self._repo._load_blob(entry_id))
            if not data:
                continue
            entry = self._repo._to_data(data)
            if all(pred(entry) for pred in residual):
                matched.append(entry)
                if len(matched) >= target:
                    break
        return matched[offset : offset + limit]

    def count(
        self,
        *,
        status: str | None = None,
        domain: str | None = None,
        failure_type: str | None = None,
    ) -> int:
        """Count operations matching filters.

        No residual filter → O(1) ZCARD on the driving index. With a residual
        filter → full driving-index scan (count must scan fully — no early
        stop, unlike find).

        544 D8: when both status and domain are set and the composite
        is warm, combo count collapses to O(1) ZCARD on the composite
        (no domain residual scan).
        """
        composite_warm = (
            status is not None
            and domain is not None
            and not self._backend.is_degraded
            and self._warm_composite_if_needed(status, domain)
        )
        key = self._driving_index_key(status, domain, composite_warm=composite_warm)
        residual = self._residual_filters(
            status, domain, failure_type, composite_warm=composite_warm
        )

        if not residual:
            return self._backend.zcard(key)

        entry_ids = self._backend.zrevrange(key, 0, -1)
        total = 0
        for entry_id in entry_ids:
            data = self._repo._decode_entry(self._repo._load_blob(entry_id))
            if not data:
                continue
            entry = self._repo._to_data(data)
            if all(pred(entry) for pred in residual):
                total += 1
        return total

    def count_created_in_window(self, start: datetime, end: datetime) -> int:
        """Count entries created in the inclusive [start, end] via ZCOUNT.

        The status-independent global index (``dlq:all``) scores every member
        by the created_at epoch (541 D6), so a ZCOUNT over [start_ts, end_ts]
        is the windowed inflow count in O(log N). Reflects current membership:
        retention shorter than the window, or size-limit eviction, can
        undercount the window tail (non-conservative) — disclosed at the
        Error Budget wiring seam.
        """
        return self._backend.zcount(
            self._repo.ALL_KEY, start.timestamp(), end.timestamp()
        )

    def find_replayable(
        self,
        max_retries: int,
        domain: str | None = None,
        failure_type: str | None = None,
        limit: int = 100,
    ) -> list[FailedOperationData]:
        """Find operations that can be replayed."""
        pending = self.find_by_status(
            status=FailedOperationStatus.PENDING.value,
            domain=domain,
            failure_type=failure_type,
            limit=limit * 2,
        )

        results = []
        for entry in pending:
            if entry.retry_count < max_retries:
                results.append(entry)
                if len(results) >= limit:
                    break

        return results

    def find_sla_breached(
        self,
        current_time: datetime,
        sla_thresholds: dict[str, timedelta],
    ) -> list[FailedOperationData]:
        """Find operations that have breached their SLA."""
        pending = self._repo.get_pending(limit=1000)

        results = []
        default_sla = sla_thresholds.get("default", timedelta(hours=24))

        for entry in pending:
            if entry.created_at:
                threshold = sla_thresholds.get(entry.domain, default_sla)
                deadline = entry.created_at + threshold
                if current_time > deadline:
                    results.append(entry)

        return results

    def find_expired(self, current_time: datetime) -> list[FailedOperationData]:
        """Find operations past their retention period."""
        return self.get_expired_operations(current_time)

    def get_expired_operations(
        self,
        before_date: datetime,
        limit: int = 100,
    ) -> list[FailedOperationData]:
        """Get operations that have expired."""
        results = []
        pending = self._repo.get_pending(limit=limit * 2)

        for entry in pending:
            if entry.expires_at and entry.expires_at < before_date:
                results.append(entry)
                if len(results) >= limit:
                    break

        return results

    def get_statistics(self) -> dict[str, Any]:
        """Get statistics about failed operations.

        Baseline counts via O(1) ZCARD.

        D9: Adds pending breakdown for daily report:
        - pending_by_domain: {domain: count}
        - pending_by_domain_and_failure_type: {domain: {failure_type: count}}

        Breakdown iterates ZRANGE pending ZSET in chunks of 500 and issues
        hgetall() per entry. This is the first O(N) operation in Redis
        get_statistics() — others are O(1) ZCARD.

        Incident safety: breakdown computation is wrapped in try/except so
        an outage-scale pending backlog (tens of thousands of entries) that
        times out does NOT fail the whole call — baseline counts are still
        returned. Daily report fails the breakdown open: omitted on error
        while dlq_pending_count (O(1) ZCARD) is preserved. Scan limit
        rejected per D9 — silent count capping misleads operators.
        """
        pending_count = self._repo.count_pending()
        resolved_count = self.count_by_status(FailedOperationStatus.RESOLVED.value)
        requires_review_count = self.count_by_status(
            FailedOperationStatus.REQUIRES_REVIEW.value
        )
        rejected_count = self.count_by_status(FailedOperationStatus.REJECTED.value)
        archived_count = self.count_by_status(FailedOperationStatus.ARCHIVED.value)

        stats: dict[str, Any] = {
            # 541 D6: total is the status-independent global-index ZCARD
            # (all entries), matching the memory adapter's all-entries total
            # and including escalated/terminal statuses the 5-status partial
            # sum omitted.
            "total": self._backend.zcard(self._repo.ALL_KEY),
            "pending": pending_count,
            "pending_count": pending_count,
            "resolved": resolved_count,
            "resolved_count": resolved_count,
            "requires_review": requires_review_count,
            "reviewing_count": requires_review_count,
            "rejected": rejected_count,
            "rejected_count": rejected_count,
            "archived": archived_count,
            "archived_count": archived_count,
        }

        # D9: pending breakdown via chunked pipelining. Fail-open per call
        # site (daily report) — on exception, breakdown keys absent.
        try:
            pending_by_domain, pending_by_ft = self._collect_pending_breakdown()
            stats["pending_by_domain"] = pending_by_domain
            stats["pending_by_domain_and_failure_type"] = pending_by_ft
        except Exception as e:
            logger.warning("redis_dlq.pending_breakdown_failed", error=e)

        return stats

    def _collect_pending_breakdown(
        self,
        batch_size: int = 500,
    ) -> tuple[dict[str, int], dict[str, dict[str, int]]]:
        """Collect pending breakdowns via chunked ZRANGE + pipelined HGETALL.

        Returns:
            (pending_by_domain, pending_by_domain_and_failure_type)
        """
        pending_by_domain: dict[str, int] = {}
        pending_by_domain_and_ft: dict[str, dict[str, int]] = {}

        if self._backend.is_degraded:
            # In-memory fallback path
            for key, value in self._backend._memory.items():
                if not self._repo._is_valid_entry_key(key):
                    continue
                data = self._repo._decode_entry(value)
                if not data:
                    continue
                if data.get("status") != FailedOperationStatus.PENDING.value:
                    continue
                domain = data.get("domain", "")
                failure_type = data.get("failure_type", "")
                pending_by_domain[domain] = pending_by_domain.get(domain, 0) + 1
                ft_map = pending_by_domain_and_ft.setdefault(domain, {})
                ft_map[failure_type] = ft_map.get(failure_type, 0) + 1
            return pending_by_domain, pending_by_domain_and_ft

        # Normal Redis path — chunked ZRANGE + hgetall() per entry.
        # The ResilientStorageBackend doesn't expose a pipeline() method,
        # so we iterate entries per batch. Acceptable for daily report
        # (once/day); batch size caps memory footprint per iteration.
        total = self._backend.zcard(self._repo.PENDING_KEY)
        for start in range(0, total, batch_size):
            stop = start + batch_size - 1
            entry_ids = self._backend.zrange(self._repo.PENDING_KEY, start, stop)
            if not entry_ids:
                break

            for entry_id in entry_ids:
                data = self._repo._decode_entry(self._repo._load_blob(entry_id))
                if not data:
                    continue
                domain = data.get("domain", "")
                failure_type = data.get("failure_type", "")
                pending_by_domain[domain] = pending_by_domain.get(domain, 0) + 1
                ft_map = pending_by_domain_and_ft.setdefault(domain, {})
                ft_map[failure_type] = ft_map.get(failure_type, 0) + 1

        return pending_by_domain, pending_by_domain_and_ft

    # =========================================================================
    # Faceted counts (542 D4 — read-side only; hot write path unchanged)
    # =========================================================================

    def get_facet_counts(
        self,
        *,
        status: str | None = None,
        domain: str | None = None,
    ) -> dict[str, dict[str, int]]:
        """Faceted status×domain counts (D4 — read-side only).

        ``by_status`` is scoped by ``domain``; ``by_domain`` is scoped by
        ``status`` (D2); zero-count buckets are dropped. The hot DLQ
        create/_update/delete path and the Redis key footprint are untouched
        — all computation reads the two existing per-dimension ZSETs.

        Normal-mode scoped facets use server-side ZINTERCARD over those ZSETs
        (no blob loads, exact). Redis <7.0 (ZINTERCARD unknown command —
        capability cached) falls back to a bounded blob-bucket scan;
        degraded mode buckets the in-memory entries. Both fail-open partial.
        """
        if self._backend.is_degraded:
            return self._facet_from_memory(status, domain)
        return {
            "by_status": self._facet_by_status(domain),
            "by_domain": self._facet_by_domain(status),
        }

    def _status_index_key(self, status: str) -> str:
        """Relative ZSET index key for a status (PENDING has its own key)."""
        if status == FailedOperationStatus.PENDING.value:
            return self._repo.PENDING_KEY
        return self._repo._status_key(status)

    def _all_statuses(self) -> list[str]:
        """Every status with a dedicated index (PENDING + _STATUS_INDEXED)."""
        return [FailedOperationStatus.PENDING.value, *self._repo._STATUS_INDEXED]

    # ----- by_status facet (scoped by domain) -------------------------------

    def _facet_by_status(self, domain: str | None) -> dict[str, int]:  # noqa: C901
        """by_status facet for normal mode, optionally scoped to ``domain``.

        544 D7: scoped cells fast-path through the composite ``(status,
        domain)`` ZSET when warm — O(1) ``ZCARD`` per cell, no
        per-call ``ZINTERCARD``. Cold cells lazy-warm via
        ``_warm_composite_if_needed`` (EXISTS or ZINTERSTORE) on first
        access; subsequent calls hit the cache. ZINTERCARD remains the
        cold-path fallback for the case where the warmup helper cannot
        materialize the composite (e.g. raw-client miss). ZINTERCARD
        unsupported (Redis <7.0) falls back to the bounded blob scan.
        """
        if domain is None:
            # Unfiltered: O(1) ZCARD per status, no blob loads.
            counts: dict[str, int] = {}
            for s in self._all_statuses():
                n = self.count_by_status(s)
                if n:
                    counts[s] = n
            return counts

        # 544 D7 fast path: composite ZCARD per status (warm + cold-warmup).
        counts: dict[str, int] = {}
        composite_fallback = False
        for s in self._all_statuses():
            if self._warm_composite_if_needed(s, domain):
                n = self._backend.zcard(self._repo._status_domain_key(s, domain))
                if n:
                    counts[s] = n
            else:
                composite_fallback = True
                break

        if not composite_fallback:
            return counts

        # Composite warmup unavailable (raw client missing). Fall back
        # to ZINTERCARD (Redis 7.0+) or the bounded blob scan.
        if self._zintercard_supported is not False:
            try:
                domain_full = self._backend._get_full_key(
                    f"{self._repo.BY_DOMAIN_PREFIX}{domain}"
                )
                counts = {}
                for s in self._all_statuses():
                    status_full = self._backend._get_full_key(self._status_index_key(s))
                    n = self._zintercard(domain_full, status_full)
                    if n:
                        counts[s] = n
                self._zintercard_supported = True
                return counts
            except Exception as e:
                self._handle_zintercard_error(e, "facet_by_status_failed")

        return self._facet_by_status_scan(domain)

    def _facet_by_status_scan(self, domain: str) -> dict[str, int]:
        """Fallback: load domain ``X``'s entries and bucket by status.

        Used for Redis <7.0 (no ZINTERCARD). Bounded by max_size_per_domain
        so a pathological domain cannot scan unbounded.
        """
        domain_key = f"{self._repo.BY_DOMAIN_PREFIX}{domain}"
        entry_ids = self._backend.zrange(domain_key, 0, self._max_size_per_domain() - 1)
        counts: dict[str, int] = {}
        for entry_id in entry_ids:
            data = self._repo._decode_entry(self._repo._load_blob(entry_id))
            if not data:
                continue
            s = data.get("status", "")
            if s:
                counts[s] = counts.get(s, 0) + 1
        return counts

    # ----- by_domain facet (scoped by status) -------------------------------

    def _facet_by_domain(self, status: str | None) -> dict[str, int]:  # noqa: C901
        """by_domain facet for normal mode, optionally scoped to ``status``.

        544 D7: domain enumeration uses ``ZRANGE dlq:domains`` (warm
        registry) instead of the legacy ``by_domain:*`` keyspace SCAN.
        First call lazy-warms the registry via a one-time SCAN→ZADD
        batch. Scoped cells fast-path through composite ZCARDs when
        warm; cold cells lazy-warm via EXISTS+ZINTERSTORE.
        """
        domains = self._enumerate_domains()

        if status is None:
            counts: dict[str, int] = {}
            for d in domains:
                n = self._backend.zcard(f"{self._repo.BY_DOMAIN_PREFIX}{d}")
                if n:
                    counts[d] = n
            return counts

        # 544 D7 scoped fast path: composite ZCARD per domain.
        counts: dict[str, int] = {}
        composite_fallback = False
        for d in domains:
            if self._warm_composite_if_needed(status, d):
                n = self._backend.zcard(self._repo._status_domain_key(status, d))
                if n:
                    counts[d] = n
            else:
                composite_fallback = True
                break

        if not composite_fallback:
            return counts

        # Composite warmup unavailable. Fall back to ZINTERCARD or the
        # bounded blob scan.
        if self._zintercard_supported is not False:
            try:
                status_full = self._backend._get_full_key(
                    self._status_index_key(status)
                )
                counts = {}
                for d in domains:
                    domain_full = self._backend._get_full_key(
                        f"{self._repo.BY_DOMAIN_PREFIX}{d}"
                    )
                    n = self._zintercard(domain_full, status_full)
                    if n:
                        counts[d] = n
                self._zintercard_supported = True
                return counts
            except Exception as e:
                self._handle_zintercard_error(e, "facet_by_domain_failed")

        return self._facet_by_domain_scan(status)

    def _enumerate_domains(self) -> list[str]:
        """Enumerate domains via the registry ZSET (``dlq:domains``).

        544 D7: replaces the prior ``_scan_domain_keys`` keyspace SCAN
        on every panel open. First call lazy-warms the registry via
        SCAN→ZADD; subsequent calls hit the ZRANGE fast path.
        """
        if not self._warm_domains_registry_if_needed():
            # Raw client unavailable — fall back to the one-shot SCAN
            # (same shape as the legacy path; cheap because domain
            # cardinality is low).
            return self._scan_domain_keys_raw()

        members = self._backend.zrange(self._repo.DOMAINS_KEY, 0, -1)
        return list(members)

    def _facet_by_domain_scan(self, status: str) -> dict[str, int]:
        """Fallback: load status ``Y``'s entries and bucket by domain.

        Used for Redis <7.0 (no ZINTERCARD). Bounded by max_size.
        """
        entries = self.by_status(status, limit=self._max_size())
        counts: dict[str, int] = {}
        for entry in entries:
            if entry.domain:
                counts[entry.domain] = counts.get(entry.domain, 0) + 1
        return counts

    # ----- degraded-mode single-scan facet ----------------------------------

    def _facet_from_memory(
        self, status: str | None, domain: str | None
    ) -> dict[str, dict[str, int]]:
        """Degraded-mode facet: one scan of ``_backend._memory`` entry blobs,
        bucketing by status (scoped to ``domain``) and domain (scoped to
        ``status``). Precedent: ``_get_by_status_from_memory`` /
        ``_collect_pending_breakdown`` degraded paths.
        """
        by_status: dict[str, int] = {}
        by_domain: dict[str, int] = {}
        for key, value in self._backend._memory.items():
            if not self._repo._is_valid_entry_key(key):
                continue
            data = self._repo._decode_entry(value)
            if not data:
                continue
            s = data.get("status", "")
            d = data.get("domain", "")
            if s and (domain is None or d == domain):
                by_status[s] = by_status.get(s, 0) + 1
            if d and (status is None or s == status):
                by_domain[d] = by_domain.get(d, 0) + 1
        return {"by_status": by_status, "by_domain": by_domain}

    # ----- raw-client helpers ------------------------------------------------

    def _zintercard(self, full_key_a: str, full_key_b: str) -> int:
        """Server-side ZINTERCARD of two full keys (Redis 7.0+).

        Raises on transport/response error so the caller can fall back.
        """
        client = self._repo._raw_redis_client
        if client is None:
            raise RuntimeError("raw redis client unavailable")
        result = client.execute_command("ZINTERCARD", 2, full_key_a, full_key_b)
        return int(result)

    def _handle_zintercard_error(self, exc: Exception, event: str) -> None:
        """Cache <7.0 incapability on an unknown-command error; log otherwise.

        An unknown-command ResponseError means ZINTERCARD is unsupported
        (Redis <7.0) — cache it so we stop retrying. Any other error (e.g.
        a transient transport blip) is logged and falls through to the
        bounded fallback this request only, without disabling ZINTERCARD.
        """
        if "unknown command" in str(exc).lower():
            self._zintercard_supported = False
            logger.warning("redis_dlq.zintercard_unsupported", error=exc)
        else:
            logger.warning("redis_dlq.facet_query_failed", op=event, error=exc)

    # =========================================================================
    # 544 D5: lazy backfill (EXISTS + ZINTERSTORE / SCAN + ZADD)
    # =========================================================================

    def _warm_composite_if_needed(self, status: str, domain: str) -> bool:
        """Ensure the composite ``(status, domain)`` ZSET exists on Redis.

        Process-local cache (``_composite_warmed``) mirrors
        ``_zintercard_supported``. Cold path: ``EXISTS`` — if 1 a
        previous writer already populated it (mark warmed and return).
        If 0, ``ZINTERSTORE`` over the per-status and by_domain ZSETs
        materializes the composite (server-side O(N1+N2)). Both sides
        carry created_at-epoch scores, so ``AGGREGATE MAX`` preserves
        the canonical score. Mark warmed.

        Returns True when the composite is usable (warmed in cache);
        False when the raw client is unavailable (degraded / non-Redis
        backend), so the caller can fall back to the legacy code path.

        Risk window (D5 accepted): between ``EXISTS=0`` and
        ``ZINTERSTORE`` completion, a concurrent CREATE's composite
        ``zadd`` can be overwritten by ZINTERSTORE. The lost entry is
        auto-recovered by the next ``_update`` on it. Mitigation cost
        (Lua atomic wrapper) violates the SB-003 Cluster precedent.
        """
        key = (status, domain)
        if key in self._composite_warmed:
            return True
        if not status or not domain:
            return False

        client = self._repo._raw_redis_client
        if client is None:
            return False

        try:
            composite_full = self._backend._get_full_key(
                self._repo._status_domain_key(status, domain)
            )
            # EXISTS first — a populated composite (any other writer beat
            # us here) is the cheap fast path.
            exists = client.execute_command("EXISTS", composite_full)
            if int(exists) >= 1:
                self._composite_warmed.add(key)
                return True

            # Materialize from the existing per-status + by_domain ZSETs.
            # AGGREGATE MAX is identical to MIN here (both inputs carry the
            # same created_at score), choosing one explicitly for clarity.
            status_full = self._backend._get_full_key(self._status_index_key(status))
            domain_full = self._backend._get_full_key(
                f"{self._repo.BY_DOMAIN_PREFIX}{domain}"
            )
            client.execute_command(
                "ZINTERSTORE",
                composite_full,
                2,
                status_full,
                domain_full,
                "AGGREGATE",
                "MAX",
            )
            self._composite_warmed.add(key)
            return True
        except Exception as exc:
            logger.warning(
                "redis_dlq.composite_warmup_failed",
                status=status,
                healing_domain=domain,
                error=exc,
            )
            return False

    def _warm_domains_registry_if_needed(self) -> bool:
        """Ensure ``dlq:domains`` is populated; first call backfills from
        the legacy ``by_domain:*`` keyspace SCAN.

        Subsequent calls hit the flag and skip. After warmup completes
        the D3 cardinality alert fires once if ``ZCARD > threshold``.

        Returns True when the registry is ready for ZRANGE; False when
        the raw client is unavailable.
        """
        if self._domains_registry_warmed:
            return True

        client = self._repo._raw_redis_client
        if client is None:
            return False

        try:
            domains = self._scan_domain_keys_raw()
            if domains:
                # ZADD in one batch — score = current time as a
                # last_seen_at marker; subsequent CREATEs update the
                # score with their own timestamp.
                now = time.time()
                domains_full = self._backend._get_full_key(self._repo.DOMAINS_KEY)
                client.zadd(domains_full, dict.fromkeys(domains, now))
                # Update process-local known-domains cache so create()
                # treats these as already observed and the post-create
                # cardinality check stays on the per-new-domain cadence.
                self._repo._known_domains.update(domains)

            self._domains_registry_warmed = True

            # Fire the cardinality alert on warmup completion (D3).
            self._repo._maybe_emit_cardinality_alert(trigger="warmup_complete")
            return True
        except Exception as exc:
            logger.warning("redis_dlq.domain_registry_warmup_failed", error=exc)
            return False

    def _scan_domain_keys_raw(self) -> list[str]:
        """Enumerate domains from ``by_domain:*`` via SCAN (one-shot, used
        only by ``_warm_domains_registry_if_needed``).

        Replaces the removed ``_scan_domain_keys`` public path; kept
        here as a private helper since warmup is the sole remaining
        keyspace SCAN site.
        """
        client = self._repo._raw_redis_client
        if client is None:
            return []
        full_prefix = self._backend._get_full_key(self._repo.BY_DOMAIN_PREFIX)
        pattern = f"{full_prefix}*"
        domains: list[str] = []
        cursor = 0
        while True:
            cursor, keys = client.scan(cursor, match=pattern, count=100)
            for k in keys:
                if isinstance(k, bytes):
                    k = k.decode()
                if k.startswith(full_prefix):
                    domains.append(k[len(full_prefix) :])
            if cursor == 0:
                break
        return domains

    def _max_size(self) -> int:
        try:
            from baldur.settings.dlq import get_dlq_settings

            return int(get_dlq_settings().max_size)
        except Exception:
            return 100_000

    def _max_size_per_domain(self) -> int:
        try:
            from baldur.settings.dlq import get_dlq_settings

            return int(get_dlq_settings().max_size_per_domain)
        except Exception:
            return 20_000
