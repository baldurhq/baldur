"""Unit tests for RedisDLQRepository.get_facet_counts (542 D4).

Redis facets are **read-side only** — the hot DLQ write path (create/update/
delete) is unchanged. Scoped facets use server-side ``ZINTERCARD`` over the two
existing per-dimension ZSETs (``by_domain:*`` and the per-status indexes); no
blob loads, exact. On Redis <7.0 (``ZINTERCARD`` raises an unknown-command
``ResponseError`` — capability cached after the first failure) **or** in
degraded mode the implementation falls back to a bounded blob-bucket / in-
memory scan, fail-open partial.

Coverage axes (Test Assessment 542):
- Normal mode unfiltered (ZCARD per status + SCAN by_domain)
- Normal mode scoped via ZINTERCARD (parity with fallback bucketing)
- Redis <7.0: capability cache flips False on first unknown-command error
  and stays sticky on the next call (no retry-then-fail loop)
- Degraded mode: ``_facet_from_memory`` single-scan + zero-drop
- ``_scan_domain_keys`` cursor loop + prefix-strip
- D5 — ``RedisDLQMaintenance.get_cleanup_stats`` complete ``by_status``:
  every present indexed status counted, zero-count statuses omitted
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from redis.exceptions import ResponseError

from baldur.adapters.redis.dlq_maintenance import RedisDLQMaintenance
from baldur.adapters.redis.dlq_query import RedisDLQQuery
from baldur.interfaces.repositories import (
    FailedOperationData,
    FailedOperationStatus,
)


def _blob(data: dict) -> bytes:
    return json.dumps(data).encode("utf-8")


def _make_repo(backend: MagicMock | None = None, raw_client: MagicMock | None = None):
    """Construct a RedisDLQRepository with mock backend + raw client.

    Bypasses __init__ to avoid pulling in the real connection factory; mirrors
    the helper in ``test_dlq_sub_modules.py`` and patches ``_raw_redis_client``
    so ``RedisDLQQuery._zintercard`` / ``_scan_domain_keys`` can exercise the
    raw-command path without a live Redis.
    """
    from baldur.adapters.redis.dlq import RedisDLQRepository

    backend = backend or MagicMock()
    with patch.object(RedisDLQRepository, "__init__", lambda self, **kw: None):
        repo = RedisDLQRepository.__new__(RedisDLQRepository)
    repo._backend = backend
    repo._key_prefix = "dlq:"
    repo._pending_key = "dlq:pending"
    repo._entry_prefix = "dlq:entry:"
    repo._by_domain_prefix = "dlq:by_domain:"
    repo._status_prefix = "dlq:status:"
    repo._status_domain_prefix = "dlq:status_domain:"
    repo._all_key = "dlq:all"
    repo._domains_key = "dlq:domains"
    repo._known_domains = set()

    # Mock the raw-client property used by ZINTERCARD/SCAN.
    type(repo).__dict__  # noqa: B018 — keep linters quiet about the next patch
    repo.__class__._raw_redis_client_test_override = raw_client or MagicMock()

    # Override the property with a plain attribute lookup on the instance.
    # Patching at the class level for _raw_redis_client preserves
    # _make_repo isolation across tests.
    repo._raw_redis_client_test_override = raw_client or MagicMock()

    # Stub _get_full_key on backend to pass keys through unchanged (no
    # dynamic prefix in tests).
    backend._get_full_key.side_effect = lambda key: key

    repo.query = RedisDLQQuery(repo)
    repo.maintenance = RedisDLQMaintenance(repo)
    return repo


@pytest.fixture(autouse=True)
def _patch_raw_redis_client_property():
    """Replace ``_raw_redis_client`` so tests can inject a fake client.

    The real property reads ``backend._redis._redis``; mocking that chain is
    noisier than patching the property to read a per-instance override.
    """
    from baldur.adapters.redis.dlq import RedisDLQRepository

    original = RedisDLQRepository._raw_redis_client
    RedisDLQRepository._raw_redis_client = property(
        lambda self: getattr(self, "_raw_redis_client_test_override", None)
    )
    try:
        yield
    finally:
        RedisDLQRepository._raw_redis_client = original


_PENDING = FailedOperationStatus.PENDING.value
_RESOLVED = FailedOperationStatus.RESOLVED.value
_REPLAYING = FailedOperationStatus.REPLAYING.value
_EXPIRED = FailedOperationStatus.EXPIRED.value


# =============================================================================
# Normal-mode unfiltered
# =============================================================================


class TestRedisFacetCountsUnfilteredBehavior:
    """Unfiltered call: ZCARD per status; SCAN by_domain:* then ZCARD each."""

    def test_unfiltered_returns_zcard_per_indexed_status(self):
        """``by_status`` reads ``count_by_status`` for PENDING + every indexed
        status; zero counts are dropped (D4)."""
        backend = MagicMock()
        backend.is_degraded = False
        raw_client = MagicMock()
        # Domain SCAN: one batch with no domains (unfiltered by_domain
        # exercised separately).
        raw_client.scan.return_value = (0, [])
        repo = _make_repo(backend, raw_client)

        # count_by_status returns deterministic values per status.
        counts = {_PENDING: 7, _RESOLVED: 3, _REPLAYING: 0, _EXPIRED: 2}
        repo.query.count_by_status = MagicMock(side_effect=lambda s: counts.get(s, 0))

        result = repo.query.get_facet_counts()

        # Zero-count REPLAYING dropped; PENDING/RESOLVED/EXPIRED present.
        assert result["by_status"][_PENDING] == 7
        assert result["by_status"][_RESOLVED] == 3
        assert result["by_status"][_EXPIRED] == 2
        assert _REPLAYING not in result["by_status"]

    def test_unfiltered_by_domain_uses_registry_zrange_then_zcard_per_domain(self):
        """544 D7: ``by_domain`` enumerates domains via ``ZRANGE
        dlq:domains`` (warm registry), then ZCARDs each
        ``by_domain:<name>`` key. Zero-count domains are dropped. Cold
        registry triggers a one-time SCAN→ZADD warmup."""
        backend = MagicMock()
        backend.is_degraded = False
        # ZCARD returns by domain name; ZRANGE returns the warmed registry.
        backend.zcard.side_effect = lambda key: {
            "dlq:by_domain:payment": 5,
            "dlq:by_domain:inventory": 0,  # drained
            "dlq:by_domain:auth": 2,
        }.get(key, 0)
        backend.zrange.side_effect = lambda key, start, end: (
            ["payment", "inventory", "auth"] if key == "dlq:domains" else []
        )

        raw_client = MagicMock()
        # One-time warmup SCAN returns the legacy by_domain:* keys.
        raw_client.scan.return_value = (
            0,
            [
                b"dlq:by_domain:payment",
                b"dlq:by_domain:inventory",
                b"dlq:by_domain:auth",
            ],
        )
        repo = _make_repo(backend, raw_client)
        repo.query.count_by_status = MagicMock(return_value=0)

        result = repo.query.get_facet_counts()

        # Drained inventory dropped; registry-enumerated payment/auth present.
        assert result["by_domain"] == {"payment": 5, "auth": 2}
        # Warmup ZADD-ed the registry once during the first by_domain call.
        raw_client.zadd.assert_called_once()


# =============================================================================
# Normal-mode scoped — ZINTERCARD
# =============================================================================


class TestRedisFacetCountsZintercard:
    """Scoped facets compute via server-side ZINTERCARD (no blob loads)."""

    def test_domain_scope_calls_zintercard_per_status_and_returns_int(self):
        """``by_status`` for ``domain=X`` runs one ZINTERCARD per status."""
        backend = MagicMock()
        backend.is_degraded = False

        # ZINTERCARD returns vary per (domain_key, status_key) call. Use a
        # side_effect that inspects the args.
        def execute(cmd, numkeys, *keys):
            assert cmd == "ZINTERCARD"
            assert numkeys == 2
            domain_key, status_key = keys
            assert domain_key == "dlq:by_domain:payment"
            return {
                "dlq:pending": 4,
                "dlq:status:resolved": 2,
                "dlq:status:replaying": 0,
            }.get(status_key, 0)

        raw_client = MagicMock()
        raw_client.execute_command.side_effect = execute
        repo = _make_repo(backend, raw_client)

        result = repo.query._facet_by_status(domain="payment")

        # Server-side intersection cardinality; zero buckets dropped.
        assert result == {_PENDING: 4, _RESOLVED: 2}
        # Capability cache flipped to True (success path).
        assert repo.query._zintercard_supported is True

    def test_status_scope_uses_composite_zcard_per_enumerated_domain(self):
        """544 D7: ``by_domain`` for ``status=Y`` enumerates domains via
        the registry then reads composite ZCARDs per (status, domain) —
        no ZINTERCARD on the warm path. Cold cells warm via
        ``EXISTS`` + ``ZINTERSTORE`` then ZCARD."""
        backend = MagicMock()
        backend.is_degraded = False
        # Domain registry warmup SCAN → ZADD; subsequent ZRANGE returns the
        # warmed list.
        backend.zrange.side_effect = lambda key, start, end: (
            ["payment", "inventory", "auth"] if key == "dlq:domains" else []
        )
        # ZCARD on the composite (resolved, *) ZSETs.
        backend.zcard.side_effect = lambda key: {
            "dlq:status_domain:resolved:payment": 3,
            "dlq:status_domain:resolved:inventory": 1,
            "dlq:status_domain:resolved:auth": 0,
        }.get(key, 0)

        raw_client = MagicMock()
        # Warmup SCAN populates the registry once.
        raw_client.scan.return_value = (
            0,
            [
                b"dlq:by_domain:payment",
                b"dlq:by_domain:inventory",
                b"dlq:by_domain:auth",
            ],
        )
        # Composite EXISTS=1 for each (resolved, domain) → no ZINTERSTORE call.
        raw_client.execute_command.return_value = 1
        repo = _make_repo(backend, raw_client)

        result = repo.query._facet_by_domain(status=_RESOLVED)

        # Zero-count auth dropped; payment/inventory present via composite ZCARD.
        assert result == {"payment": 3, "inventory": 1}
        # Composite warmed for all three (resolved, domain) cells.
        assert (_RESOLVED, "payment") in repo.query._composite_warmed
        assert (_RESOLVED, "inventory") in repo.query._composite_warmed
        assert (_RESOLVED, "auth") in repo.query._composite_warmed


# =============================================================================
# Redis <7.0: capability cache + fallback parity
# =============================================================================


class TestRedisFacetCountsZintercardUnsupported:
    """ZINTERCARD raising 'unknown command' flips _zintercard_supported False."""

    def test_unknown_command_response_error_caches_unsupported_flag(self):
        """544 D7: when composite warmup fails (EXISTS raises) AND the
        ZINTERCARD fallback also raises 'unknown command' (Redis <7.0),
        ``_zintercard_supported`` flips to False so the next call skips
        the ZINTERCARD attempt and goes straight to the bounded blob scan.
        The composite warmup is retried per cell (separately cached on
        success), but a raw-client that uniformly raises 'unknown
        command' for every command will continue to fail warmup — that
        cell falls through to the bounded scan."""
        backend = MagicMock()
        backend.is_degraded = False
        # Fallback path needs ZRANGE → entry blobs.
        backend.zrange.return_value = []

        raw_client = MagicMock()
        raw_client.execute_command.side_effect = ResponseError(
            "unknown command 'ZINTERCARD'"
        )
        repo = _make_repo(backend, raw_client)

        # First call: composite warmup raises → fallback to ZINTERCARD
        # → ZINTERCARD also raises 'unknown command' → cache flips False
        # → bounded blob-bucket scan (empty memory → empty result).
        result1 = repo.query._facet_by_status(domain="payment")
        assert repo.query._zintercard_supported is False
        assert result1 == {}

        # Second call: the ``_zintercard_supported is False`` short-
        # circuits the ZINTERCARD branch — execute_command must not be
        # called for ZINTERCARD. (Composite warmup still tries EXISTS
        # per cell on the first lookup, but once a cell warms it stays
        # cached; here we re-set the side_effect to ensure subsequent
        # calls would surface as failures, but the test asserts the
        # specific ZINTERCARD command is not invoked.)
        raw_client.execute_command.reset_mock()
        repo.query._facet_by_status(domain="payment")
        zintercard_calls = [
            c
            for c in raw_client.execute_command.call_args_list
            if c.args and c.args[0] == "ZINTERCARD"
        ]
        assert zintercard_calls == []

    def test_fallback_scan_parity_with_zintercard_path(self):
        """Same seeded set → bounded blob-bucket scan returns identical counts
        to what ZINTERCARD would have returned (D4 fallback drift check)."""
        backend = MagicMock()
        backend.is_degraded = False

        # ZRANGE on the domain key returns three entry ids.
        backend.zrange.return_value = ["e1", "e2", "e3"]
        raw_client = MagicMock()
        # Force the fallback path by raising unknown-command on first call.
        raw_client.execute_command.side_effect = ResponseError(
            "unknown command 'ZINTERCARD'"
        )
        repo = _make_repo(backend, raw_client)

        # Mock _load_blob/_decode_entry so the fallback can bucket by status.
        repo._load_blob = MagicMock(side_effect=lambda eid: _blob({"status": "?"}))
        statuses = {
            "e1": _PENDING,
            "e2": _PENDING,
            "e3": _RESOLVED,
        }
        repo._decode_entry = MagicMock(
            side_effect=lambda blob: (
                {"status": statuses.get(blob[0:2].decode(), "")} if blob else {}
            )
        )

        # ZRANGE returns the ids; _load_blob keyed on id; _decode_entry
        # inspects the first 2 bytes of the blob to look up the seeded status.
        # Replace with an id-aware seam instead so the test stays clear:
        repo._load_blob = MagicMock(side_effect=lambda eid: eid.encode())
        repo._decode_entry = MagicMock(
            side_effect=lambda blob: (
                {"status": statuses.get(blob.decode(), "")} if blob else {}
            )
        )

        result = repo.query._facet_by_status(domain="payment")

        # 2 PENDING + 1 RESOLVED = the same shape a ZINTERCARD path would
        # have surfaced from the same seeded ZSETs.
        assert result == {_PENDING: 2, _RESOLVED: 1}

    def test_transient_error_does_not_cache_unsupported_flag(self):
        """A non 'unknown command' error (e.g. transient transport blip)
        falls through to the bounded fallback **this request only** but does
        NOT disable ZINTERCARD for future requests (D4)."""
        backend = MagicMock()
        backend.is_degraded = False
        backend.zrange.return_value = []

        raw_client = MagicMock()
        # First call: transient ResponseError without the 'unknown command'
        # marker. The error must be logged + bounded-fallback used, but the
        # capability cache should NOT flip — the next request retries
        # ZINTERCARD normally.
        raw_client.execute_command.side_effect = ResponseError(
            "ERR transient backend blip"
        )
        repo = _make_repo(backend, raw_client)

        repo.query._facet_by_status(domain="payment")

        # Cache stays unset (None) — ZINTERCARD is retried next call.
        assert repo.query._zintercard_supported is None


# =============================================================================
# Degraded-mode single-scan facet
# =============================================================================


class TestRedisFacetCountsDegraded:
    """Degraded mode buckets ``_backend._memory`` in one scan (D4)."""

    def test_degraded_mode_single_scan_buckets_status_and_domain(self):
        """One pass over ``_backend._memory`` builds both facet maps.
        Status scope filters ``by_domain`` only; domain scope filters
        ``by_status`` only (D2 — each axis filters by the OTHER)."""
        backend = MagicMock()
        backend.is_degraded = True
        backend._memory = {
            "dlq:entry:1": _blob({"status": _PENDING, "domain": "payment"}),
            "dlq:entry:2": _blob({"status": _PENDING, "domain": "inventory"}),
            "dlq:entry:3": _blob({"status": _RESOLVED, "domain": "payment"}),
            "dlq:entry:4": _blob({"status": _RESOLVED, "domain": "payment"}),
            # Non-entry key — must be skipped via _is_valid_entry_key.
            "dlq:pending": b"ignored",
        }
        repo = _make_repo(backend)

        result = repo.query.get_facet_counts()

        assert result["by_status"] == {_PENDING: 2, _RESOLVED: 2}
        assert result["by_domain"] == {"payment": 3, "inventory": 1}

    def test_degraded_mode_with_domain_scope_narrows_by_status(self):
        """In degraded mode, ``domain=X`` keeps ``by_domain`` complete but
        scopes ``by_status`` to entries in domain X (D2)."""
        backend = MagicMock()
        backend.is_degraded = True
        backend._memory = {
            "dlq:entry:1": _blob({"status": _PENDING, "domain": "payment"}),
            "dlq:entry:2": _blob({"status": _RESOLVED, "domain": "payment"}),
            "dlq:entry:3": _blob({"status": _PENDING, "domain": "inventory"}),
        }
        repo = _make_repo(backend)

        result = repo.query.get_facet_counts(domain="payment")

        # by_status scoped to payment (1 PENDING + 1 RESOLVED).
        assert result["by_status"] == {_PENDING: 1, _RESOLVED: 1}
        # by_domain unscoped (D2).
        assert result["by_domain"] == {"payment": 2, "inventory": 1}

    def test_degraded_mode_skips_non_entry_keys(self):
        """``_is_valid_entry_key`` filters out non-``dlq:entry:`` keys so
        special keys (PENDING_KEY, status indexes) never bucket as entries."""
        backend = MagicMock()
        backend.is_degraded = True
        backend._memory = {
            "dlq:pending": b"not_a_blob",
            "dlq:status:resolved": b"also_not_a_blob",
            "dlq:entry:1": _blob({"status": _PENDING, "domain": "payment"}),
        }
        repo = _make_repo(backend)

        result = repo.query.get_facet_counts()

        # Only the single entry blob was counted.
        assert result == {
            "by_status": {_PENDING: 1},
            "by_domain": {"payment": 1},
        }


# =============================================================================
# _scan_domain_keys cursor loop
# =============================================================================


class TestRedisFacetCountsScanDomains:
    """SCAN cursor loop returns every domain across multiple batches."""

    def test_scan_iterates_cursor_until_zero(self):
        """SCAN may return data across multiple cursor steps; the helper
        must accumulate until the cursor wraps to 0."""
        backend = MagicMock()
        backend._get_full_key.side_effect = lambda k: k
        raw_client = MagicMock()
        # Two-step cursor: first batch has 2 keys, second has 1, then cursor=0.
        raw_client.scan.side_effect = [
            (5, [b"dlq:by_domain:payment", b"dlq:by_domain:inventory"]),
            (0, [b"dlq:by_domain:auth"]),
        ]
        repo = _make_repo(backend, raw_client)

        domains = repo.query._scan_domain_keys_raw()

        assert sorted(domains) == ["auth", "inventory", "payment"]

    def test_scan_strips_full_prefix_from_returned_keys(self):
        """The raw key carries the dynamic prefix; the helper returns the bare
        domain name (the part after ``by_domain:``)."""
        backend = MagicMock()
        raw_client = MagicMock()
        raw_client.scan.return_value = (
            0,
            [b"baldur:dlq:by_domain:payment", b"baldur:dlq:by_domain:auth"],
        )
        repo = _make_repo(backend, raw_client)
        # Re-apply the dynamic-prefix override AFTER _make_repo (which sets
        # pass-through). Simulates ResilientStorageBackend's `xtest:` /
        # `baldur:` runtime prefixes.
        backend._get_full_key.side_effect = lambda k: f"baldur:{k}"

        domains = repo.query._scan_domain_keys_raw()

        assert sorted(domains) == ["auth", "payment"]

    def test_scan_returns_empty_when_raw_client_unavailable(self):
        """If the raw client is missing (backend not yet connected), the
        helper returns an empty list — the caller produces an empty by_domain
        rather than crashing."""
        repo = _make_repo(MagicMock(), raw_client=None)
        # Explicitly null out the override so the property returns None.
        repo._raw_redis_client_test_override = None

        assert repo.query._scan_domain_keys_raw() == []


# =============================================================================
# D5 — get_cleanup_stats complete by_status
# =============================================================================


def _make_failed_op(status: str, resolved_at: datetime | None = None):
    return FailedOperationData(
        id="1",
        domain="payment",
        failure_type="timeout",
        status=status,
        retry_count=0,
        max_retries=3,
        resolved_at=resolved_at,
    )


class TestRedisCleanupStatsCompleteByStatus:
    """D5 — ``by_status`` includes every present indexed status, drops zeros."""

    def test_includes_status_outside_the_prior_5_subset(self):
        """Pre-542 the dict was hardcoded to pending/resolved/requires_review/
        rejected/archived. D5 must surface a non-subset status (e.g.
        REPLAYING, EXPIRED, PERMANENTLY_FAILED) when it has entries."""
        backend = MagicMock()
        backend.is_degraded = False
        repo = _make_repo(backend)
        repo.query.get_statistics = MagicMock(return_value={"total": 5})

        counts = {
            _PENDING: 2,
            _REPLAYING: 1,  # ← non-subset status, was silently omitted
            _EXPIRED: 1,  # ← non-subset status
            _RESOLVED: 1,
        }
        repo.query.count_by_status = MagicMock(side_effect=lambda s: counts.get(s, 0))
        repo.query.by_status = MagicMock(return_value=[])

        with patch(
            "baldur.adapters.redis.dlq_maintenance.utc_now",
            return_value=datetime(2026, 3, 16, tzinfo=UTC),
        ):
            stats = repo.maintenance.get_cleanup_stats()

        # All four present statuses surfaced.
        assert stats["by_status"][_PENDING] == 2
        assert stats["by_status"][_REPLAYING] == 1
        assert stats["by_status"][_EXPIRED] == 1
        assert stats["by_status"][_RESOLVED] == 1

    def test_zero_count_statuses_are_omitted_not_zero(self):
        """A status with zero entries must be absent from ``by_status``,
        not surfaced as ``:0`` — matches the memory/SQL adapter parity (D5)."""
        backend = MagicMock()
        backend.is_degraded = False
        repo = _make_repo(backend)
        repo.query.get_statistics = MagicMock(return_value={"total": 1})

        # Only PENDING has entries; every other indexed status returns 0.
        repo.query.count_by_status = MagicMock(
            side_effect=lambda s: 1 if s == _PENDING else 0
        )
        repo.query.by_status = MagicMock(return_value=[])

        with patch(
            "baldur.adapters.redis.dlq_maintenance.utc_now",
            return_value=datetime(2026, 3, 16, tzinfo=UTC),
        ):
            stats = repo.maintenance.get_cleanup_stats()

        # Only PENDING present; every other status omitted (no `: 0`).
        assert stats["by_status"] == {_PENDING: 1}
        # No zero values lingering anywhere in by_status.
        assert all(count > 0 for count in stats["by_status"].values())

    def test_age_bucketed_counts_preserved(self):
        """D5 changes only by_status — the age-bucketed resolved_older_than_30
        and archived_older_than_90 fields must stay correct."""
        backend = MagicMock()
        backend.is_degraded = False
        repo = _make_repo(backend)
        repo.query.get_statistics = MagicMock(return_value={"total": 2})
        repo.query.count_by_status = MagicMock(return_value=1)

        now = datetime(2026, 3, 16, tzinfo=UTC)
        old_resolved = _make_failed_op(_RESOLVED, resolved_at=now - timedelta(days=45))
        recent_resolved = _make_failed_op(
            _RESOLVED, resolved_at=now - timedelta(days=10)
        )
        old_archived = _make_failed_op(
            FailedOperationStatus.ARCHIVED.value,
            resolved_at=now - timedelta(days=100),
        )

        def by_status_side_effect(status, limit=10000):
            if status == _RESOLVED:
                return [old_resolved, recent_resolved]
            if status == FailedOperationStatus.ARCHIVED.value:
                return [old_archived]
            return []

        repo.query.by_status = MagicMock(side_effect=by_status_side_effect)

        with patch(
            "baldur.adapters.redis.dlq_maintenance.utc_now",
            return_value=now,
        ):
            stats = repo.maintenance.get_cleanup_stats()

        assert stats["resolved_older_than_30_days"] == 1
        assert stats["archived_older_than_90_days"] == 1
        assert stats["total"] == 2
