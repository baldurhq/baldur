"""
Redis-based DLQ (Dead Letter Queue) Repository.

Core CRUD operations with composition-based delegation to sub-modules:
- dlq_query.py: Query and search/filter operations
- dlq_lifecycle.py: State transitions and replay management
- dlq_maintenance.py: Archiving, cleanup, size limit operations
- dlq_compression.py: Compression operations

D8: ABC method names are canonical. Redis "native" names removed.
D9: FailedOperationRepository ABC is NOT split. Sub-modules are
    implementation-level composition only.

Storage layout (#502, #538):
- dlq:entry:{id} → STRING (orjson-encoded entry, optionally zlib-compressed;
  magic-byte 0x78 marks compressed payloads). The id is the
  process-namespaced composite token {pod_id}:{pid}:{run_nonce}:{seq} (538 D2).
- dlq:pending → Sorted Set (pending queue, score=created_at epoch)
- dlq:status:{status} → Sorted Set (per-status index, score=created_at epoch)
- dlq:by_domain:{domain} → Sorted Set (items by domain for filtering)
- dlq:all → Sorted Set (status-independent global index, score=created_at
  epoch; powers cross-status paginated find/count and the statistics total,
  #541 D6)

The dedicated ``dlq:entry:`` sub-namespace makes entry keys structurally
disjoint from every special-key family, so normal-mode SCAN globs
``dlq:entry:*`` exactly and entry-vs-special discrimination is a positive
whitelist (538 D6) rather than a reserved-word blacklist.
"""

from __future__ import annotations

import itertools
import os
import secrets
import time
import zlib
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

import structlog

from baldur.interfaces.repositories import (
    DLQCompressedEntry,
    FailedOperationData,
    FailedOperationRepository,
    FailedOperationStatus,
)
from baldur.utils.time import utc_now

if TYPE_CHECKING:
    from baldur.adapters.resilient.backend import ResilientStorageBackend

logger = structlog.get_logger()


# Magic byte for zlib stream (default-window deflate). orjson output always
# starts with `{` (0x7b) for dict payloads, so 0x78 is a safe discriminator.
_ZLIB_MAGIC_BYTE = 0x78

# Top-level FailedOperationData fields whose values are dropped pre-encode
# when they match these defaults. Restored via dataclass defaults on decode.
_ENTRY_FIELD_DEFAULTS: dict[str, Any] = {
    "entity_type": "",
    "entity_id": "",
    "entity_refs": {},
    "user_id": None,
    "snapshot_data": {},
    "error_code": "",
    "error_message": "",
    "request_data": {},
    "response_data": {},
    "metadata": {},
    "last_retry_at": None,
    "resolved_at": None,
    "resolved_by_id": None,
    "resolution_type": "",
    "resolution_note": "",
    "next_action_hint": "",
    "recommended_action": "",
    "expires_at": None,
}


class RedisDLQRepository(
    FailedOperationRepository
):  # verified-by: test_degraded_mode_writes_wal
    """
    Redis-based Dead Letter Queue Repository.

    Uses ResilientStorageBackend for:
    - Normal mode: Redis storage with sorted sets for efficient querying
    - Degraded mode: Memory + WAL (zero data loss)

    Sub-module composition (D9: ABC stays unified, composition is internal):
    - self.query: RedisDLQQuery (query + search/filter)
    - self.lifecycle: RedisDLQLifecycle (state transitions + replay)
    - self.maintenance: RedisDLQMaintenance (archiving + cleanup)
    - self.compression: RedisDLQCompression (compression)

    Reference: docs/baldur/middleware_system/70_MULTI_CLUSTER_ARCHITECTURE.md
    """

    _BASE_PREFIX = "dlq"
    _PENDING_SUFFIX = "pending"
    _ENTRY_SUFFIX = "entry"
    _BY_DOMAIN_PREFIX = "by_domain"
    _STATUS_PREFIX = "status"
    # 544 D1: composite (status, domain) ZSET prefix —
    # ``dlq:status_domain:{status}:{domain}``. PENDING is treated as a
    # first-class indexed status (D2), so the composite covers it too via
    # ``dlq:status_domain:pending:{domain}``. Score = created_at epoch
    # matching every other ZSET index (541 D6).
    _STATUS_DOMAIN_PREFIX = "status_domain"
    # 544 D3: domain registry ZSET (``dlq:domains``). Score = last_seen_at
    # epoch. Replaces the keyspace SCAN of ``by_domain:*`` for the
    # unfiltered ``by_domain`` facet panel.
    _DOMAINS_SUFFIX = "domains"
    # Status-independent global index (relative key "all" → "dlq:all").
    # Outside the dlq:entry: namespace, so the entry SCAN glob and the
    # _is_valid_entry_key whitelist are unaffected (541 D6).
    _ALL_SUFFIX = "all"

    # Statuses with dedicated sorted set indexes for O(limit) find / O(1)
    # ZCARD count. PENDING uses its own PENDING_KEY, so it is the one enum
    # value not listed here; every other status is index-served (541 D6) so
    # no status filter falls back to an O(N) keyspace SCAN. REVIEWING /
    # REPLAYED indexes stay empty in Redis-only deployments (produced only by
    # the Django ORM model); _update auto-maintains them if a future path sets
    # them. REPLAYING is maintained by the atomic acquire block in
    # dlq_lifecycle (which bypasses _update).
    _STATUS_INDEXED = frozenset(
        {
            FailedOperationStatus.REPLAYING.value,
            FailedOperationStatus.REVIEWING.value,
            FailedOperationStatus.REPLAYED.value,
            FailedOperationStatus.REQUIRES_REVIEW.value,
            FailedOperationStatus.RESOLVED.value,
            FailedOperationStatus.REJECTED.value,
            FailedOperationStatus.ARCHIVED.value,
            FailedOperationStatus.EXPIRED.value,
            FailedOperationStatus.PERMANENTLY_FAILED.value,
        }
    )

    def __init__(
        self,
        backend: ResilientStorageBackend,
        *,
        pod_id: str | None = None,
        pid: int | None = None,
        run_nonce: str | None = None,
    ):
        self._backend = backend
        self._key_prefix = self._build_key_prefix()
        self._pending_key = f"{self._key_prefix}{self._PENDING_SUFFIX}"
        self._entry_prefix = f"{self._key_prefix}{self._ENTRY_SUFFIX}:"
        self._by_domain_prefix = f"{self._key_prefix}{self._BY_DOMAIN_PREFIX}:"
        self._status_prefix = f"{self._key_prefix}{self._STATUS_PREFIX}:"
        self._status_domain_prefix = f"{self._key_prefix}{self._STATUS_DOMAIN_PREFIX}:"
        self._all_key = f"{self._key_prefix}{self._ALL_SUFFIX}"
        self._domains_key = f"{self._key_prefix}{self._DOMAINS_SUFFIX}"

        # 544 D3: process-local cache for cardinality-alert dedup. Flipped
        # on first observation of a domain (via create or warmup); a known
        # domain skips the post-ZADD ZCARD check on the hot path.
        self._known_domains: set[str] = set()

        # Process-namespaced composite ID identity (538 D2). Captured once
        # at construction so _allocate_id is a pure read of instance state.
        # Injectable seams let a test simulate N worker processes in-process
        # by constructing N repos with distinct identities (no subprocess).
        from baldur.core.cluster_identity import get_cluster_identity

        if pod_id is None:
            try:
                pod_id = get_cluster_identity().pod_id
            except Exception:
                pod_id = "unknown"
        self._pod_id = pod_id
        self._pid = pid if pid is not None else os.getpid()
        # run_nonce makes the namespace unique per process *start* — a
        # container running the app as pid 1 with a persistent wal_dir would
        # otherwise re-allocate {pod}:1:0... after a restart, colliding with
        # not-yet-recovered pre-restart entries (538 D2).
        self._run_nonce = run_nonce if run_nonce is not None else secrets.token_hex(8)
        self._seq_counter = itertools.count()

        # Composition: sub-modules
        from baldur.adapters.redis.dlq_compression import RedisDLQCompression
        from baldur.adapters.redis.dlq_lifecycle import RedisDLQLifecycle
        from baldur.adapters.redis.dlq_maintenance import RedisDLQMaintenance
        from baldur.adapters.redis.dlq_query import RedisDLQQuery

        self.query = RedisDLQQuery(self)
        self.lifecycle = RedisDLQLifecycle(self)
        self.maintenance = RedisDLQMaintenance(self)
        self.compression = RedisDLQCompression(self)

    def _build_key_prefix(self) -> str:
        """Build component key prefix (namespace handled by backend)."""
        return f"{self._BASE_PREFIX}:"

    @property
    def KEY_PREFIX(self) -> str:
        return self._key_prefix

    @property
    def PENDING_KEY(self) -> str:
        return self._pending_key

    @property
    def ENTRY_PREFIX(self) -> str:
        """Relative key prefix for entry blobs (``dlq:entry:``)."""
        return self._entry_prefix

    @property
    def BY_DOMAIN_PREFIX(self) -> str:
        return self._by_domain_prefix

    @property
    def ALL_KEY(self) -> str:
        """Relative key for the status-independent global index (``dlq:all``)."""
        return self._all_key

    def _status_key(self, status: str) -> str:
        """Return sorted set key for a status index."""
        return f"{self._status_prefix}{status}"

    def _status_domain_key(self, status: str, domain: str) -> str:
        """Return sorted set key for the composite (status, domain) index.

        544 D1/D2: ``dlq:status_domain:{status}:{domain}``. Covers every
        enum status including PENDING — combo find of ``status=pending``
        otherwise misses entries (PENDING uses its own dedicated key in
        the dimension-only indexes, so the composite mirrors the same
        treatment).
        """
        return f"{self._status_domain_prefix}{status}:{domain}"

    @property
    def DOMAINS_KEY(self) -> str:
        """Relative key for the domain registry ZSET (``dlq:domains``)."""
        return self._domains_key

    def _allocate_id(self) -> str:
        """Allocate a process-namespaced composite entry ID (538 D2).

        Returns ``{pod_id}:{pid}:{run_nonce}:{seq}`` — collide-free across
        uncoordinated worker processes (including restart with pid reuse,
        disambiguated by run_nonce) without a Redis/WAL round-trip. ZSET
        index scores are ``time.time()`` so opaque composite members order
        correctly; the id is never parsed numerically.
        """
        seq = next(self._seq_counter)
        return f"{self._pod_id}:{self._pid}:{self._run_nonce}:{seq}"

    def _make_key(self, entry_id: str) -> str:
        """Generate storage key for entry under the ``dlq:entry:`` namespace."""
        return f"{self._entry_prefix}{entry_id}"

    def _is_valid_entry_key(self, key: str) -> bool:
        """Positive-match whitelist: a key is an entry iff it lives under the
        dedicated ``dlq:entry:`` namespace (538 D6).

        Immune to the reserved-set maintenance bug class — anything not under
        ``dlq:entry:`` (special-key families, legacy ``dlq:42`` orphans) is by
        definition not an entry. The backend ``key_prefix`` is stripped first
        so the relative key starts with ``dlq:``.
        """
        prefix = self._backend.config.key_prefix
        if isinstance(prefix, str) and prefix and key.startswith(prefix):
            relative = key[len(prefix) :]
        else:
            relative = key
        return relative.startswith(self._entry_prefix)

    # =========================================================================
    # Blob encode / decode (#502 D5+D6)
    # =========================================================================

    def _encode_entry(self, data: dict[str, Any]) -> bytes:
        """Encode entry dict to a single Redis STRING value.

        Pipeline (D6): default-drop top-level fields → orjson encode →
        optional zlib compress when ``entry_payload_compression_enabled``
        is True.
        """
        from baldur.utils.serialization import fast_dumps_str_compact

        encoded = fast_dumps_str_compact(data, defaults=_ENTRY_FIELD_DEFAULTS).encode(
            "utf-8"
        )

        if self._compression_enabled():
            return zlib.compress(encoded)
        return encoded

    def _decode_entry(self, blob: bytes | str | None) -> dict[str, Any]:
        """Decode a Redis STRING value back to an entry dict.

        Auto-detects zlib compression via the leading magic byte (0x78);
        orjson's `{` (0x7b) discriminates the uncompressed case. Returns
        an empty dict when the blob is missing or unparseable so the
        caller can treat the entry as absent.
        """
        if blob is None:
            return {}

        if isinstance(blob, str):
            blob = blob.encode("utf-8")

        if not isinstance(blob, (bytes, bytearray)) or len(blob) == 0:
            return {}

        try:
            if blob[0] == _ZLIB_MAGIC_BYTE:
                blob = zlib.decompress(blob)
        except zlib.error:
            logger.exception("redis_dlq.decode_zlib_failed")
            return {}

        try:
            from baldur.utils.serialization import fast_loads

            decoded = fast_loads(blob)
        except (ValueError, TypeError):
            logger.exception("redis_dlq.decode_json_failed")
            return {}

        return decoded if isinstance(decoded, dict) else {}

    def _compression_enabled(self) -> bool:
        """Return the current compression toggle (re-read each call)."""
        try:
            from baldur.settings.dlq import get_dlq_settings

            return bool(get_dlq_settings().entry_payload_compression_enabled)
        except Exception:
            return True

    def _store_blob(self, entry_id: str, blob: bytes) -> None:
        """Store the encoded entry under the entry key.

        Thin delegator to ``ResilientStorageBackend.set_blob``, which owns
        the raw-bytes path: REDIS raw ``set`` (wire payload == our exact
        bytes, no orjson encoding) and DEGRADED WAL-First with a
        base64-wrapped value (so degraded-mode DLQ entries survive a crash
        and replay to Redis on recovery — #470 D5).
        """
        self._backend.set_blob(self._make_key(entry_id), blob)

    def _load_blob(self, entry_id: str) -> bytes | None:
        """Load the encoded entry via ``ResilientStorageBackend.get_blob``
        (Redis raw ``get`` or memory in degraded mode)."""
        return self._backend.get_blob(self._make_key(entry_id))

    # =========================================================================
    # Core CRUD
    # =========================================================================

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
        """Create a new DLQ entry."""
        entry_id = self._allocate_id()
        now = utc_now()

        data: dict[str, Any] = {
            "id": entry_id,
            "domain": domain,
            "failure_type": failure_type,
            "error_message": error_message,
            "error_code": error_code,
            "status": FailedOperationStatus.PENDING.value,
            "entity_type": entity_type or "",
            "entity_id": entity_id or "",
            "entity_refs": entity_refs or {},
            "user_id": user_id,
            "snapshot_data": snapshot_data or {},
            "request_data": request_data or {},
            "response_data": response_data or {},
            "metadata": metadata or {},
            "retry_count": retry_count,
            "max_retries": max_retries,
            "next_action_hint": next_action_hint,
            "recommended_action": recommended_action,
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
            "expires_at": expires_at.isoformat() if expires_at else None,
        }

        # 538 D3: blob + ZSET index writes as one transactional grouped op.
        # Degraded create drops to 1 fsync (batch_write_entries); a mid-create
        # normal-mode Redis failure lands the entry fully in degraded
        # WAL+memory (no Redis-blob / degraded-zadd split).
        #
        # 541 D6: score every index by the created_at epoch (not time.time())
        # so the PENDING, by_domain, composite, and global (dlq:all) sets all
        # carry created_at — cross-status score-merge == created_at-exact.
        #
        # 544 D1/D2: composite (status=pending, domain) ZSET so combo
        # find/count routes through a windowed ZRANGE, not a per-status
        # full scan + Python residual filter (SB-016). 544 D3: domain
        # registry ZSET so the unfiltered by_domain facet skips the
        # ``by_domain:*`` keyspace SCAN (SB-018). 543 D1: the pipelined
        # batch is 1 RTT regardless of op count, so the +2 ops are
        # absorbed at zero extra round-trips.
        ts = now.timestamp()
        domain_key = f"{self.BY_DOMAIN_PREFIX}{domain}"
        composite_key = self._status_domain_key(
            FailedOperationStatus.PENDING.value, domain
        )
        self._backend.batch_write_ops(
            [
                ("set_blob", self._make_key(entry_id), self._encode_entry(data)),
                ("zadd", self.PENDING_KEY, {entry_id: ts}),
                ("zadd", domain_key, {entry_id: ts}),
                ("zadd", self.ALL_KEY, {entry_id: ts}),
                ("zadd", composite_key, {entry_id: ts}),
                ("zadd", self.DOMAINS_KEY, {domain: ts}),
            ]
        )

        # 544 D3: cardinality alert on new domain. Process-local
        # _known_domains caches first-observation so the post-ZADD ZCARD
        # is one RTT *per new domain*, not per create — hot path is
        # unaffected. Threshold-crossed warning is fail-open: an alert
        # failure does not break the create.
        if domain and domain not in self._known_domains:
            self._known_domains.add(domain)
            self._maybe_emit_cardinality_alert(trigger="create_new_domain")

        logger.info(
            "redis_dlq.created_entry",
            entry_id=entry_id,
            healing_domain=domain,
            failure_type=failure_type,
        )

        return self._to_data(data)

    def _cardinality_alert_threshold(self) -> int:
        """Resolve the configured domain-cardinality alert threshold.

        Re-read each call so a runtime settings change applies without
        repository rebuild. Falls back to 1024 (matching the field
        default) if settings are unreachable.
        """
        try:
            from baldur.settings.dlq import get_dlq_settings

            return int(get_dlq_settings().domain_cardinality_alert_threshold)
        except Exception:
            return 1024

    def _maybe_emit_cardinality_alert(self, *, trigger: str) -> None:
        """Emit ``redis_dlq.domain_cardinality_alert`` when ZCARD exceeds the
        configured threshold.

        544 D3: soft observability alert (does not enforce input
        validation). Fail-open: any error reading ZCARD is swallowed so
        the alert path cannot break the hot write path.
        """
        try:
            threshold = self._cardinality_alert_threshold()
            domain_count = self._backend.zcard(self.DOMAINS_KEY)
            if domain_count > threshold:
                logger.warning(
                    "redis_dlq.domain_cardinality_alert",
                    domain_count=domain_count,
                    threshold=threshold,
                    trigger=trigger,
                )
        except Exception:
            logger.debug("redis_dlq.cardinality_alert_check_failed")

    def get_by_id(self, id: str) -> FailedOperationData | None:
        """Get a failed operation by ID (D8: canonical ABC name)."""
        data = self._decode_entry(self._load_blob(id))
        if not data:
            return None
        return self._to_data(data)

    def _update(  # noqa: C901, PLR0912, PLR0915
        self,
        entry_id: str,
        status: str | None = None,
        retry_count: int | None = None,
        last_retry_at: datetime | None = None,
        resolution_type: str | None = None,
        resolution_note: str | None = None,
        resolved_at: datetime | None = None,
        resolved_by_id: int | None = None,
        metadata: dict[str, Any] | None = None,
        recommended_action: str | None = None,
    ) -> bool:
        """Internal update helper — used by sub-modules.

        STRING-encoded entries require GET → mutate → SET; partial-field
        writes are no longer possible. Index-set maintenance still uses
        the backend ZSET primitives so degraded mode keeps working.

        544 D6: a status transition collapses to a single
        ``batch_write_ops`` call covering ``zrem`` old per-status,
        ``zadd`` new per-status, ``zrem`` old composite, ``zadd`` new
        composite, ``set_blob`` new blob — 1 RTT total (vs 5 RTTs with
        per-op zrem/zadd + separate blob set).
        """
        existing = self._decode_entry(self._load_blob(entry_id))
        if not existing:
            return False

        now = utc_now()
        existing["updated_at"] = now.isoformat()

        status_changing = status is not None
        if status_changing:
            old_status = existing.get("status", "")
            existing["status"] = status

        if retry_count is not None:
            existing["retry_count"] = retry_count
        if last_retry_at is not None:
            existing["last_retry_at"] = last_retry_at.isoformat()
        if resolution_type is not None:
            existing["resolution_type"] = resolution_type
        if resolution_note is not None:
            existing["resolution_note"] = resolution_note
        if resolved_at is not None:
            existing["resolved_at"] = resolved_at.isoformat()
        if resolved_by_id is not None:
            existing["resolved_by_id"] = resolved_by_id
        if recommended_action is not None:
            existing["recommended_action"] = recommended_action
        if metadata is not None:
            existing_meta = existing.get("metadata") or {}
            if not isinstance(existing_meta, dict):
                existing_meta = {}
            existing_meta.update(metadata)
            existing["metadata"] = existing_meta

        encoded = self._encode_entry(existing)
        blob_op = ("set_blob", self._make_key(entry_id), encoded)

        if not status_changing:
            # Pure field update — no index work, no batch benefit; the
            # standalone set_blob keeps the degraded path's per-op
            # accounting + observability unchanged.
            self._store_blob(entry_id, encoded)
            return True

        entry_id_str = str(entry_id)
        domain = existing.get("domain", "")

        # 541 D6: score the per-status / composite indexes by the
        # created_at epoch (not the transition time) so per-status find
        # is "recently created", not "recently transitioned". The global
        # dlq:all index is NOT touched here — the entry stays in it
        # across all transitions.
        created_raw = existing.get("created_at")
        try:
            ts = (
                datetime.fromisoformat(created_raw).timestamp()
                if created_raw
                else time.time()
            )
        except (ValueError, TypeError):
            ts = time.time()

        # 544 D6: build a single batch covering [zrem old per-status,
        # zadd new per-status, zrem old composite, zadd new composite,
        # set_blob new blob]. Index-first / blob-last ordering matches
        # the prior per-op convention.
        ops: list[tuple[str, str, Any]] = []

        # Old per-status zrem
        if old_status == FailedOperationStatus.PENDING.value:
            ops.append(("zrem", self.PENDING_KEY, [entry_id_str]))
        elif old_status in self._STATUS_INDEXED:
            ops.append(("zrem", self._status_key(old_status), [entry_id_str]))

        # New per-status zadd
        if status == FailedOperationStatus.PENDING.value:
            ops.append(("zadd", self.PENDING_KEY, {entry_id_str: ts}))
        elif status in self._STATUS_INDEXED:
            ops.append(("zadd", self._status_key(status), {entry_id_str: ts}))

        # 544 D1/D2: composite (status, domain) transition. The composite
        # covers every enum status including PENDING. When ``domain`` is
        # empty (degenerate entry — _to_data accepts empty domain), the
        # composite ops are skipped to avoid creating a ``:`` -suffixed
        # registry-like key.
        if domain:
            if old_status:
                ops.append(
                    (
                        "zrem",
                        self._status_domain_key(old_status, domain),
                        [entry_id_str],
                    )
                )
            if status:
                ops.append(
                    (
                        "zadd",
                        self._status_domain_key(status, domain),
                        {entry_id_str: ts},
                    )
                )

        ops.append(blob_op)
        self._backend.batch_write_ops(ops)
        return True

    def update_status(
        self,
        id: str,
        status: str,
        resolution_type: str = "",
        resolution_note: str = "",
        resolved_by_id: int | None = None,
        recommended_action: str = "",
    ) -> bool:
        """Update the status of a failed operation (D8: canonical ABC name)."""
        return self._update(
            entry_id=id,
            status=status,
            resolution_type=resolution_type if resolution_type else None,
            resolution_note=resolution_note if resolution_note else None,
            resolved_by_id=resolved_by_id,
            resolved_at=utc_now() if resolved_by_id else None,
            recommended_action=recommended_action if recommended_action else None,
        )

    def delete(self, entry_id: str) -> bool:
        """Delete DLQ entry.

        544 D6: collapses to a single ``batch_write_ops`` call covering
        the blob delete + 5 zrems (domain, status-or-PENDING, ALL,
        composite, and whichever PENDING-or-status zrem the entry's
        status did not need) — 1 RTT total (vs the prior 1-RTT batch +
        1-RTT separate blob delete). Blob-first ordering: a mid-batch
        failure leaves orphan indexes (zrem-recoverable) rather than
        orphan blobs.
        """
        data = self._decode_entry(self._load_blob(entry_id))
        entry_id_str = str(entry_id)
        if not data:
            # Entry already absent from the blob store — fall through to
            # backend.delete to remain idempotent (returns False).
            return self._backend.delete(self._make_key(entry_id))

        domain = data.get("domain", "")
        status = data.get("status", "")

        # Blob-first so a prefix-application failure can never leave an
        # orphaned blob with stale index entries.
        ops: list[tuple[str, str, Any]] = [
            ("delete", self._make_key(entry_id), None),
        ]

        if domain:
            domain_key = f"{self.BY_DOMAIN_PREFIX}{domain}"
            ops.append(("zrem", domain_key, [entry_id_str]))

        if status == FailedOperationStatus.PENDING.value:
            ops.append(("zrem", self.PENDING_KEY, [entry_id_str]))
        elif status in self._STATUS_INDEXED:
            ops.append(("zrem", self._status_key(status), [entry_id_str]))

        # 541 D6: global index zrem is unconditional — all purge/evict/
        # compress paths route through delete(), so this is the single
        # global-index removal point regardless of the entry's status.
        ops.append(("zrem", self.ALL_KEY, [entry_id_str]))

        # 544 D1/D2: composite zrem mirrors the per-status / PENDING
        # treatment — covers every enum status including PENDING.
        if domain and status:
            ops.append(
                ("zrem", self._status_domain_key(status, domain), [entry_id_str])
            )

        self._backend.batch_write_ops(ops)
        return True

    def get_pending(self, limit: int = 100) -> list[FailedOperationData]:
        """Get pending entries."""
        pending_ids = self._backend.zrange(self.PENDING_KEY, 0, limit - 1)

        results = []
        for entry_id in pending_ids:
            data = self._decode_entry(self._load_blob(entry_id))
            if data and data.get("status") == FailedOperationStatus.PENDING.value:
                results.append(self._to_data(data))

        return results

    def count_pending(self) -> int:
        """Get count of pending entries."""
        return self._backend.zcard(self.PENDING_KEY)

    # =========================================================================
    # ABC Delegation — delegates to sub-modules
    # =========================================================================

    # --- Query delegation ---
    def get_pending_by_domain(
        self, domain: str, limit: int = 100
    ) -> list[FailedOperationData]:
        return self.query.get_pending_by_domain(domain, limit)

    def get_pending_count_by_domain(self, domain: str) -> int:
        return self.query.get_pending_count_by_domain(domain)

    def get_by_status(self, status: str, limit: int = 100) -> list[FailedOperationData]:
        return self.query.by_status(status, limit)

    def count_by_status(self, status: str) -> int:
        return self.query.count_by_status(status)

    def find_by_status(
        self,
        status: str,
        domain: str | None = None,
        failure_type: str | None = None,
        limit: int = 100,
    ) -> list[FailedOperationData]:
        return self.query.find_by_status(status, domain, failure_type, limit)

    def find(
        self,
        *,
        status: str | None = None,
        domain: str | None = None,
        failure_type: str | None = None,
        offset: int = 0,
        limit: int = 100,
    ) -> list[FailedOperationData]:
        return self.query.find(
            status=status,
            domain=domain,
            failure_type=failure_type,
            offset=offset,
            limit=limit,
        )

    def count(
        self,
        *,
        status: str | None = None,
        domain: str | None = None,
        failure_type: str | None = None,
    ) -> int:
        return self.query.count(status=status, domain=domain, failure_type=failure_type)

    def count_created_in_window(self, start: datetime, end: datetime) -> int:
        return self.query.count_created_in_window(start, end)

    def find_replayable(
        self,
        max_retries: int,
        domain: str | None = None,
        failure_type: str | None = None,
        limit: int = 100,
    ) -> list[FailedOperationData]:
        return self.query.find_replayable(max_retries, domain, failure_type, limit)

    def find_sla_breached(
        self, current_time: datetime, sla_thresholds: dict[str, timedelta]
    ) -> list[FailedOperationData]:
        return self.query.find_sla_breached(current_time, sla_thresholds)

    def find_expired(self, current_time: datetime) -> list[FailedOperationData]:
        return self.query.find_expired(current_time)

    def get_expired_operations(
        self, before_date: datetime, limit: int = 100
    ) -> list[FailedOperationData]:
        return self.query.get_expired_operations(before_date, limit)

    def get_statistics(self) -> dict[str, Any]:
        return self.query.get_statistics()

    def get_facet_counts(
        self,
        *,
        status: str | None = None,
        domain: str | None = None,
    ) -> dict[str, dict[str, int]]:
        return self.query.get_facet_counts(status=status, domain=domain)

    # --- Lifecycle delegation ---
    def mark_as_resolved(
        self,
        id: str,
        resolution_type: str,
        resolution_note: str = "",
        resolved_by_id: int | None = None,
    ) -> bool:
        return self.lifecycle.mark_as_resolved(
            id, resolution_type, resolution_note, resolved_by_id
        )

    def mark_rejected(
        self, entry_id: str, reason: str = "", rejected_by_id: int | None = None
    ) -> bool:
        return self.lifecycle.mark_rejected(entry_id, reason, rejected_by_id)

    def increment_retry_count(self, id: str) -> bool:
        return self.lifecycle.increment_retry_count(id)

    def try_acquire_for_replay(
        self, id: str, max_retries: int, force: bool = False
    ) -> FailedOperationData | None:
        return self.lifecycle.try_acquire_for_replay(id, max_retries, force=force)

    def complete_replay(
        self,
        id: str,
        success: bool,
        resolution_type: str = "",
        note: str = "",
        resolved_by_id: int | None = None,
        error_details: dict[str, Any] | None = None,
    ) -> bool:
        return self.lifecycle.complete_replay(
            id, success, resolution_type, note, resolved_by_id, error_details
        )

    def release_stale_replaying(self, older_than_minutes: int = 30) -> int:
        return self.lifecycle.release_stale_replaying(older_than_minutes)

    def bulk_update_status(self, ids: list[str], status: str) -> int:
        return self.lifecycle.bulk_update_status(ids, status)

    # --- Maintenance delegation ---
    def archive_old_resolved(self, older_than_days: int = 30) -> int:
        return self.maintenance.archive_old_resolved(older_than_days)

    def purge_archived(
        self, ids: list[str] | None = None, older_than_days: int | None = None
    ) -> int:
        return self.maintenance.purge_archived(ids, older_than_days)

    def count_all(self) -> int:
        return self.maintenance.count_all()

    def count_by_domain(self, domain: str) -> int:
        return self.maintenance.count_by_domain(domain)

    def get_oldest_ids(self, count: int, domain: str | None = None) -> list[str]:
        return self.maintenance.get_oldest_ids(count, domain)

    def evict_oldest(self, count: int, domain: str | None = None) -> int:
        return self.maintenance.evict_oldest(count, domain)

    def get_cleanup_stats(self) -> dict[str, Any]:
        return self.maintenance.get_cleanup_stats()

    def count_archived_older_than(self, older_than_days: int) -> int:
        return self.maintenance.count_archived_older_than(older_than_days)

    # --- Compression delegation ---
    def compress_and_evict_oldest(self, count: int, domain: str | None = None) -> int:
        return self.compression.compress_and_evict_oldest(count, domain)

    def store_compressed_entry(self, entry: DLQCompressedEntry) -> bool:
        return self.compression.store_compressed_entry(entry)

    def get_compressed_entries(
        self, domain: str | None = None, status: str | None = None, limit: int = 100
    ) -> list[DLQCompressedEntry]:
        return self.compression.get_compressed_entries(domain, status, limit)

    def get_compressed_summary(self) -> dict[str, Any]:
        return self.compression.get_compressed_summary()

    def update_compressed_status(self, entry_id: str, new_status: str) -> bool:
        return self.compression.update_compressed_status(entry_id, new_status)

    # =========================================================================
    # Redis Access Delegation
    # =========================================================================

    def _ensure_redis_available(self) -> bool:
        """Delegate to backend's lazy Redis init via its public seam."""
        return self._backend.ensure_redis()

    @property
    def _raw_redis_client(self):
        """Get raw Redis client for pipeline / WATCH operations, or None.

        Routes through the backend's public ``raw_redis_client`` seam.
        ``getattr`` with a None default keeps tolerance for mock backends
        that do not expose the property.
        """
        return getattr(self._backend, "raw_redis_client", None)

    # =========================================================================
    # Helper Methods
    # =========================================================================

    def _to_data(self, data: dict[str, Any]) -> FailedOperationData:  # noqa: C901
        """Convert decoded dict to FailedOperationData."""

        def parse_dict(value: Any) -> dict[str, Any]:
            if isinstance(value, dict):
                return value
            if not value or value == "":
                return {}
            if isinstance(value, str):
                try:
                    from baldur.utils.serialization import fast_loads

                    parsed = fast_loads(value)
                    return parsed if isinstance(parsed, dict) else {}
                except (ValueError, TypeError):
                    return {}
            return {}

        def parse_int(value: Any) -> int:
            if value is None or value == "":
                return 0
            try:
                return int(value)
            except (ValueError, TypeError):
                return 0

        def parse_datetime(value: Any) -> datetime | None:
            if not value or value == "":
                return None
            if isinstance(value, datetime):
                return value
            try:
                return datetime.fromisoformat(value)
            except (ValueError, TypeError):
                return None

        raw_id = data.get("id")
        return FailedOperationData(
            id=str(raw_id) if raw_id is not None else "",
            domain=data.get("domain", ""),
            failure_type=data.get("failure_type", ""),
            status=data.get("status", FailedOperationStatus.PENDING.value),
            entity_type=data.get("entity_type") or None,
            entity_id=data.get("entity_id") or None,
            entity_refs=parse_dict(data.get("entity_refs")),
            user_id=parse_int(data.get("user_id")) or None,
            snapshot_data=parse_dict(data.get("snapshot_data")),
            error_code=data.get("error_code", ""),
            error_message=data.get("error_message", ""),
            retry_count=parse_int(data.get("retry_count")),
            max_retries=parse_int(data.get("max_retries")) or 2,
            last_retry_at=parse_datetime(data.get("last_retry_at")),
            request_data=parse_dict(data.get("request_data")),
            response_data=parse_dict(data.get("response_data")),
            metadata=parse_dict(data.get("metadata")),
            resolved_at=parse_datetime(data.get("resolved_at")),
            resolved_by_id=parse_int(data.get("resolved_by_id")) or None,
            resolution_type=data.get("resolution_type", ""),
            resolution_note=data.get("resolution_note", ""),
            next_action_hint=data.get("next_action_hint", ""),
            recommended_action=data.get("recommended_action", ""),
            created_at=parse_datetime(data.get("created_at")),
            updated_at=parse_datetime(data.get("updated_at")),
            expires_at=parse_datetime(data.get("expires_at")),
        )


# Singleton
import threading

_redis_dlq_repo: RedisDLQRepository | None = None
_redis_dlq_repo_lock = threading.Lock()


def get_redis_dlq_repo(
    backend: ResilientStorageBackend | None = None,
) -> RedisDLQRepository:
    """Get singleton Redis DLQ Repository."""
    global _redis_dlq_repo

    if _redis_dlq_repo is None:
        with _redis_dlq_repo_lock:
            if _redis_dlq_repo is None:
                if backend is None:
                    from baldur.adapters.resilient.backend import (
                        get_storage_backend,
                    )

                    backend = get_storage_backend()
                _redis_dlq_repo = RedisDLQRepository(backend)

    return _redis_dlq_repo


def reset_redis_dlq_repo() -> None:
    """Reset singleton (for testing)."""
    global _redis_dlq_repo
    with _redis_dlq_repo_lock:
        _redis_dlq_repo = None
