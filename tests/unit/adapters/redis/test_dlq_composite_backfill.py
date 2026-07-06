"""Unit tests for the DLQ composite-index lazy backfill (544 D5).

Two warmup helpers drive read-path correctness on entries that pre-date the
composite index landing:

  - ``_warm_composite_if_needed(status, domain)`` -- process-local cache
    (``_composite_warmed``) mirrors the ``_zintercard_supported`` pattern.
    Cold path runs ``EXISTS`` first; if the composite is already populated
    (any other writer beat us), mark warmed and return. Otherwise run
    ``ZINTERSTORE`` over the per-status + by_domain ZSETs (server-side
    O(N1+N2)) to materialize the composite. Both inputs carry created_at
    epoch scores, so AGGREGATE MAX preserves canonical score (541 D6).

  - ``_warm_domains_registry_if_needed()`` -- one-time SCAN-then-ZADD batch
    that copies the legacy ``by_domain:*`` keyspace into the ``dlq:domains``
    ZSET registry. Subsequent unfiltered by_domain panel opens take a single
    ZRANGE, replacing the per-call keyspace SCAN.

Test classes:
    TestCompositeWarmupBehavior      -- 4-cell parametrize matrix on
        (EXISTS=0 / EXISTS=1) x (warmed_in_cache / not_in_cache),
        plus call-ordering (EXISTS before ZINTERSTORE) and idempotent re-warm.
    TestDomainsRegistryWarmupBehavior -- cold -> warmed flag flip, SCAN-then-
        ZADD batch interaction, idempotent re-call (no double ZADD).
"""

from __future__ import annotations

import itertools
from unittest.mock import MagicMock, patch

from baldur.adapters.redis.dlq import RedisDLQRepository
from baldur.adapters.redis.dlq_query import RedisDLQQuery


def _make_repo(
    backend: MagicMock | None = None, raw_client: MagicMock | None = None
) -> RedisDLQRepository:
    """Construct a RedisDLQRepository without running the real __init__.

    Bypasses the real connection factory; wires the small set of private
    attributes the warmup helpers consume. The raw-client property is
    patched per-test via a property override (see _patch_raw_client_property).
    """
    backend = backend or MagicMock()
    backend._get_full_key.side_effect = lambda key: key
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
    repo._pod_id = "pod-a"
    repo._pid = 100
    repo._run_nonce = "nonce0"
    repo._seq_counter = itertools.count()
    repo._compression_enabled = MagicMock(return_value=False)
    repo._raw_redis_client_test_override = raw_client
    repo.query = RedisDLQQuery(repo)
    return repo


def _with_raw_client(repo, raw_client):
    """Patch the ``_raw_redis_client`` property on the repo's class so the
    warmup helpers see the injected mock instead of the real backend chain."""
    return patch.object(
        type(repo),
        "_raw_redis_client",
        new=property(lambda self: raw_client),
    )


# =============================================================================
# Composite (status, domain) warmup
# =============================================================================


class TestCompositeWarmupBehavior:
    """``_warm_composite_if_needed`` lazy-backfill semantics."""

    def test_cache_hit_returns_true_without_redis_call(self):
        """Warm (s,d) in the cache short-circuits before any raw client call."""
        raw_client = MagicMock()
        repo = _make_repo(raw_client=raw_client)
        repo.query._composite_warmed.add(("pending", "payment"))

        with _with_raw_client(repo, raw_client):
            assert repo.query._warm_composite_if_needed("pending", "payment") is True

        raw_client.execute_command.assert_not_called()

    def test_cold_exists_one_marks_warmed_without_zinterstore(self):
        """EXISTS=1 -- a previous writer already populated the composite;
        no ZINTERSTORE is run. The cell flips warmed in the cache."""
        raw_client = MagicMock()
        raw_client.execute_command.return_value = 1
        repo = _make_repo(raw_client=raw_client)

        with _with_raw_client(repo, raw_client):
            result = repo.query._warm_composite_if_needed("resolved", "payment")

        assert result is True
        assert ("resolved", "payment") in repo.query._composite_warmed
        # Only one execute_command call -- EXISTS, not ZINTERSTORE.
        assert raw_client.execute_command.call_count == 1
        assert raw_client.execute_command.call_args.args[0] == "EXISTS"

    def test_cold_exists_zero_triggers_zinterstore_then_marks_warmed(self):
        """EXISTS=0 -- composite must be materialized server-side via
        ZINTERSTORE over the per-status + by_domain ZSETs."""
        raw_client = MagicMock()
        # First call: EXISTS -> 0; second call: ZINTERSTORE -> 5 (cardinality)
        raw_client.execute_command.side_effect = [0, 5]
        repo = _make_repo(raw_client=raw_client)

        with _with_raw_client(repo, raw_client):
            result = repo.query._warm_composite_if_needed("resolved", "payment")

        assert result is True
        assert ("resolved", "payment") in repo.query._composite_warmed
        # EXISTS came first, then ZINTERSTORE.
        commands = [c.args[0] for c in raw_client.execute_command.call_args_list]
        assert commands == ["EXISTS", "ZINTERSTORE"]
        # ZINTERSTORE uses the per-status + by_domain keys with AGGREGATE MAX.
        zinterstore_call = raw_client.execute_command.call_args_list[1]
        assert zinterstore_call.args[0] == "ZINTERSTORE"
        assert zinterstore_call.args[1] == "dlq:status_domain:resolved:payment"
        assert zinterstore_call.args[2] == 2
        assert zinterstore_call.args[3] == "dlq:status:resolved"
        assert zinterstore_call.args[4] == "dlq:by_domain:payment"
        # AGGREGATE MAX preserves the canonical score across both inputs.
        assert "AGGREGATE" in zinterstore_call.args
        assert "MAX" in zinterstore_call.args

    def test_cold_pending_status_uses_pending_key_not_status_prefix(self):
        """PENDING has its own dedicated key (``dlq:pending``), not the
        ``dlq:status:`` prefix family -- ZINTERSTORE must target the
        PENDING key for status=pending warmup."""
        raw_client = MagicMock()
        raw_client.execute_command.side_effect = [0, 3]
        repo = _make_repo(raw_client=raw_client)

        with _with_raw_client(repo, raw_client):
            repo.query._warm_composite_if_needed("pending", "payment")

        zinterstore_call = raw_client.execute_command.call_args_list[1]
        # The per-status arg is dlq:pending, not dlq:status:pending.
        assert zinterstore_call.args[3] == "dlq:pending"

    def test_raw_client_unavailable_returns_false(self):
        """Degraded mode / non-Redis backend has no raw client -- helper
        returns False so the caller falls back to the legacy code path."""
        repo = _make_repo(raw_client=None)

        with _with_raw_client(repo, None):
            result = repo.query._warm_composite_if_needed("resolved", "payment")

        assert result is False
        # Cache stays empty so subsequent retry can succeed once Redis is back.
        assert ("resolved", "payment") not in repo.query._composite_warmed

    def test_empty_status_or_domain_returns_false_without_redis(self):
        """Defensive guard -- an empty status or domain would produce a
        registry-like key (``dlq:status_domain::payment``) so the helper
        refuses and returns False without touching the raw client."""
        raw_client = MagicMock()
        repo = _make_repo(raw_client=raw_client)

        with _with_raw_client(repo, raw_client):
            assert repo.query._warm_composite_if_needed("", "payment") is False
            assert repo.query._warm_composite_if_needed("resolved", "") is False

        raw_client.execute_command.assert_not_called()

    def test_redis_exception_returns_false_without_marking_warmed(self):
        """A raw-client exception leaves the cache untouched so the next
        call retries; the helper returns False and the caller falls back."""
        raw_client = MagicMock()
        raw_client.execute_command.side_effect = RuntimeError("transient")
        repo = _make_repo(raw_client=raw_client)

        with _with_raw_client(repo, raw_client):
            result = repo.query._warm_composite_if_needed("resolved", "payment")

        assert result is False
        assert ("resolved", "payment") not in repo.query._composite_warmed

    def test_re_warm_is_idempotent_after_first_warmup(self):
        """Second call hits the cache and runs zero Redis commands -- the
        warmup is strictly write-once per (s,d)."""
        raw_client = MagicMock()
        raw_client.execute_command.side_effect = [0, 5]  # EXISTS=0, ZINTERSTORE=5
        repo = _make_repo(raw_client=raw_client)

        with _with_raw_client(repo, raw_client):
            repo.query._warm_composite_if_needed("resolved", "payment")
            # Cache flipped; next call must not touch raw client.
            repo.query._warm_composite_if_needed("resolved", "payment")
            repo.query._warm_composite_if_needed("resolved", "payment")

        # EXISTS + ZINTERSTORE on the first call only.
        assert raw_client.execute_command.call_count == 2

    def test_different_pairs_warm_independently(self):
        """``_composite_warmed`` is keyed on the (s,d) tuple, so warming
        (resolved, payment) does NOT also warm (resolved, auth)."""
        raw_client = MagicMock()
        # Two EXISTS calls (one per pair), both fast-path warmed (=1).
        raw_client.execute_command.return_value = 1
        repo = _make_repo(raw_client=raw_client)

        with _with_raw_client(repo, raw_client):
            repo.query._warm_composite_if_needed("resolved", "payment")
            repo.query._warm_composite_if_needed("resolved", "auth")

        assert ("resolved", "payment") in repo.query._composite_warmed
        assert ("resolved", "auth") in repo.query._composite_warmed
        # Two EXISTS calls -- one per pair, neither cached the other.
        assert raw_client.execute_command.call_count == 2


# =============================================================================
# Domain-registry warmup
# =============================================================================


class TestDomainsRegistryWarmupBehavior:
    """``_warm_domains_registry_if_needed`` cold-start semantics."""

    def test_cold_warmup_scans_then_zadds_then_flips_flag(self):
        """First call runs the legacy by_domain:* SCAN, ZADDs the registry
        once, flips the warmed flag, and returns True."""
        backend = MagicMock()
        backend._get_full_key.side_effect = lambda key: key
        backend.zcard.return_value = 3
        raw_client = MagicMock()
        raw_client.scan.return_value = (
            0,
            [
                b"dlq:by_domain:payment",
                b"dlq:by_domain:auth",
                b"dlq:by_domain:inventory",
            ],
        )
        repo = _make_repo(backend, raw_client)

        with _with_raw_client(repo, raw_client):
            with patch(
                "baldur.settings.dlq.get_dlq_settings",
                return_value=MagicMock(domain_cardinality_alert_threshold=1024),
            ):
                result = repo.query._warm_domains_registry_if_needed()

        assert result is True
        assert repo.query._domains_registry_warmed is True
        # ZADD invoked once with all three domains.
        raw_client.zadd.assert_called_once()
        zadd_call = raw_client.zadd.call_args
        # First positional arg = full key of dlq:domains.
        assert zadd_call.args[0] == "dlq:domains"
        # Second positional arg = {domain: score} dict.
        zadded = zadd_call.args[1]
        assert set(zadded.keys()) == {"payment", "auth", "inventory"}

    def test_warm_flag_short_circuits_subsequent_calls(self):
        """Second call returns True without any SCAN or ZADD -- the warmed
        flag is the sole arbiter on the hot path."""
        backend = MagicMock()
        backend._get_full_key.side_effect = lambda key: key
        backend.zcard.return_value = 1
        raw_client = MagicMock()
        raw_client.scan.return_value = (0, [b"dlq:by_domain:payment"])
        repo = _make_repo(backend, raw_client)

        with _with_raw_client(repo, raw_client):
            with patch(
                "baldur.settings.dlq.get_dlq_settings",
                return_value=MagicMock(domain_cardinality_alert_threshold=1024),
            ):
                repo.query._warm_domains_registry_if_needed()
                repo.query._warm_domains_registry_if_needed()
                repo.query._warm_domains_registry_if_needed()

        # SCAN + ZADD invoked exactly once across the three warm calls.
        assert raw_client.scan.call_count == 1
        assert raw_client.zadd.call_count == 1

    def test_empty_keyspace_still_flips_warmed_flag(self):
        """A fresh deploy with no legacy by_domain:* keys still completes
        warmup -- the flag must flip so the next call hits ZRANGE on the
        (still empty) registry, not the keyspace SCAN."""
        backend = MagicMock()
        backend._get_full_key.side_effect = lambda key: key
        backend.zcard.return_value = 0
        raw_client = MagicMock()
        raw_client.scan.return_value = (0, [])
        repo = _make_repo(backend, raw_client)

        with _with_raw_client(repo, raw_client):
            with patch(
                "baldur.settings.dlq.get_dlq_settings",
                return_value=MagicMock(domain_cardinality_alert_threshold=1024),
            ):
                result = repo.query._warm_domains_registry_if_needed()

        assert result is True
        assert repo.query._domains_registry_warmed is True
        # No ZADD when the scan returned no domains.
        raw_client.zadd.assert_not_called()

    def test_warmup_populates_known_domains_cache(self):
        """After warmup, ``_known_domains`` carries every legacy domain so
        a subsequent create() for those domains takes the known-domain
        hot path (no extra ZCARD)."""
        backend = MagicMock()
        backend._get_full_key.side_effect = lambda key: key
        backend.zcard.return_value = 2
        raw_client = MagicMock()
        raw_client.scan.return_value = (
            0,
            [b"dlq:by_domain:payment", b"dlq:by_domain:auth"],
        )
        repo = _make_repo(backend, raw_client)

        with _with_raw_client(repo, raw_client):
            with patch(
                "baldur.settings.dlq.get_dlq_settings",
                return_value=MagicMock(domain_cardinality_alert_threshold=1024),
            ):
                repo.query._warm_domains_registry_if_needed()

        assert "payment" in repo._known_domains
        assert "auth" in repo._known_domains

    def test_raw_client_unavailable_returns_false_without_flipping_flag(self):
        """No raw client -- helper returns False and leaves the flag down
        so a later call can retry once Redis is back."""
        repo = _make_repo(raw_client=None)

        with _with_raw_client(repo, None):
            result = repo.query._warm_domains_registry_if_needed()

        assert result is False
        assert repo.query._domains_registry_warmed is False

    def test_scan_exception_returns_false_without_flipping_flag(self):
        """A transient SCAN error leaves the flag down so the next read
        retries the warmup, not silently serves an empty registry."""
        backend = MagicMock()
        backend._get_full_key.side_effect = lambda key: key
        raw_client = MagicMock()
        raw_client.scan.side_effect = RuntimeError("transient")
        repo = _make_repo(backend, raw_client)

        with _with_raw_client(repo, raw_client):
            result = repo.query._warm_domains_registry_if_needed()

        assert result is False
        assert repo.query._domains_registry_warmed is False
