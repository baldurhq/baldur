"""
Tests for ReplayService.replay_on_circuit_close inflight lock (497 D4).

Covers:
- TestReplayInflightLockNameContract: format-stable lock name shape used
  across workers/pods.
- TestBatchReplayResultInflightSkippedContract: default + propagation
  of `inflight_skipped`; distinct from `governance_blocked`.
- TestDLQSettingsInflightTTLContract: default 300, bounds [10, 3600],
  env-var binding via `BALDUR_DLQ_CIRCUIT_CLOSE_INFLIGHT_TTL_SECONDS`.
- TestReplayServiceCacheLazyResolve: lazy_init + fail-open on
  resolver_raise + negative-resolution cached.
- TestReplayOnCircuitCloseInflightBehavior: DistributedLock-based inflight
  guard — Barrier concurrency, stale-TTL recovery (handled inside
  DistributedLock), cleanup on success/exception, fail-open when cache is
  None or `get_lock` raises, cross-instance share.
"""

from __future__ import annotations

import threading
import time
from datetime import timedelta
from unittest.mock import MagicMock, patch

import pytest
from structlog.testing import capture_logs

from baldur.adapters.cache.memory_adapter import InMemoryCacheAdapter
from baldur.interfaces.cache_provider import CacheProviderInterface
from baldur.services.event_bus.bus.event_types import EventType
from baldur.services.replay_service import ReplayService
from baldur.services.replay_service.models import BatchReplayResult
from baldur.services.replay_service.service import (
    REASON_CIRCUIT_CLOSE_INFLIGHT,
    _replay_inflight_lock_name,
)
from baldur.settings.dlq import DLQSettings

# =============================================================================
# Helpers
# =============================================================================


def _make_service(cache: CacheProviderInterface | None) -> ReplayService:
    """Build a ReplayService with mock repo + injected cache.

    The repository is stubbed so the post-inflight-acquire path is
    side-effect-free — the inflight branch is what's under test, not the
    sweep body. `find_replayable` returns an empty list so the inner
    `_replay_on_circuit_close_locked` returns BatchReplayResult(total=0).
    """
    repo = MagicMock()
    repo.find_replayable = MagicMock(return_value=[])
    svc = ReplayService(repository=repo, cache=cache)
    svc._event_bus = MagicMock()
    return svc


def _passthrough_governance():
    """Patch `check_all_governance` so it always returns allowed=True."""
    from baldur.models.governance import GovernanceCheckResult

    return patch(
        "baldur_pro.services.governance.checks.check_all_governance",
        return_value=GovernanceCheckResult(allowed=True),
    )


def _can_acquire(cache: CacheProviderInterface, service_name: str) -> bool:
    """Probe whether the inflight lock for `service_name` is currently free.

    A fresh `cache.get_lock(...).acquire(blocking=False)` succeeds iff no
    other holder owns the lock — used by tests to verify the inflight lock
    was released after the sweep (normal exit or exception path).
    """
    probe = cache.get_lock(_replay_inflight_lock_name(service_name))
    if probe.acquire(blocking=False):
        probe.release()
        return True
    return False


# =============================================================================
# Lock name contract (D4)
# =============================================================================


class TestReplayInflightLockNameContract:
    """`_replay_inflight_lock_name` produces a fixed-shape name."""

    def test_name_format_stable(self):
        # The exact shape is part of the contract — workers / pods sharing
        # the same cache backend compute the same DistributedLock name and
        # therefore the same storage key after adapter-level _make_key.
        assert (
            _replay_inflight_lock_name("payment.charge")
            == "replay:inflight:circuit_close:payment.charge"
        )

    def test_name_varies_by_service_name(self):
        assert _replay_inflight_lock_name("svc-a") != _replay_inflight_lock_name(
            "svc-b"
        )


# =============================================================================
# BatchReplayResult.inflight_skipped contract
# =============================================================================


class TestBatchReplayResultInflightSkippedContract:
    """`inflight_skipped` is a distinct, default-False field on BatchReplayResult."""

    def test_default_value_is_false(self):
        result = BatchReplayResult()
        assert result.inflight_skipped is False

    def test_distinct_from_governance_blocked(self):
        # The two flags are independent operator-facing categories — confirm
        # they can be set independently without collision.
        inflight_only = BatchReplayResult(inflight_skipped=True)
        governance_only = BatchReplayResult(governance_blocked=True)

        assert inflight_only.inflight_skipped is True
        assert inflight_only.governance_blocked is False
        assert governance_only.governance_blocked is True
        assert governance_only.inflight_skipped is False


# =============================================================================
# DLQSettings.circuit_close_inflight_ttl_seconds contract
# =============================================================================


class TestDLQSettingsInflightTTLContract:
    """Settings field — default, bounds, env-var binding."""

    def test_default_is_300_seconds(self):
        settings = DLQSettings()
        assert settings.circuit_close_inflight_ttl_seconds == 300

    def test_lower_bound_rejects_below_10(self):
        with pytest.raises(Exception):
            DLQSettings(circuit_close_inflight_ttl_seconds=9)

    def test_upper_bound_rejects_above_3600(self):
        with pytest.raises(Exception):
            DLQSettings(circuit_close_inflight_ttl_seconds=3601)

    def test_lower_bound_accepts_10(self):
        settings = DLQSettings(circuit_close_inflight_ttl_seconds=10)
        assert settings.circuit_close_inflight_ttl_seconds == 10

    def test_upper_bound_accepts_3600(self):
        settings = DLQSettings(circuit_close_inflight_ttl_seconds=3600)
        assert settings.circuit_close_inflight_ttl_seconds == 3600

    def test_env_var_binding(self, monkeypatch):
        # `make_settings_config("BALDUR_DLQ_")` wires the prefix; the field
        # name maps to `BALDUR_DLQ_CIRCUIT_CLOSE_INFLIGHT_TTL_SECONDS`.
        monkeypatch.setenv("BALDUR_DLQ_CIRCUIT_CLOSE_INFLIGHT_TTL_SECONDS", "600")
        settings = DLQSettings()
        assert settings.circuit_close_inflight_ttl_seconds == 600


# =============================================================================
# ReplayService.cache lazy resolution
# =============================================================================


class TestReplayServiceCacheLazyResolve:
    """Lazy resolution of the cache property + fail-open posture (D4)."""

    def test_injected_cache_returned_directly_without_resolution(self):
        cache = InMemoryCacheAdapter()
        svc = ReplayService(repository=MagicMock(), cache=cache)

        # Resolution flag is False because no lazy-resolve was needed.
        assert svc.cache is cache
        assert svc._cache_resolution_attempted is False

    def test_lazy_resolve_on_first_access_when_cache_not_injected(self):
        cache = InMemoryCacheAdapter()
        svc = ReplayService(repository=MagicMock(), cache=None)

        with patch(
            "baldur.factory.ProviderRegistry.get_cache", return_value=cache
        ) as mock_get:
            first = svc.cache
            second = svc.cache  # cached: no second resolver call.

        assert first is cache
        assert second is cache
        assert mock_get.call_count == 1
        assert svc._cache_resolution_attempted is True

    def test_fail_open_on_resolver_raise_returns_none_with_warning(self):
        svc = ReplayService(repository=MagicMock(), cache=None)

        with (
            patch(
                "baldur.factory.ProviderRegistry.get_cache",
                side_effect=RuntimeError("registry boom"),
            ),
            capture_logs() as cap_logs,
        ):
            resolved = svc.cache

        assert resolved is None
        matching = [
            entry
            for entry in cap_logs
            if entry.get("event") == "replay_service.inflight_cache_unavailable"
            and entry.get("reason") == "provider_resolution_failed"
        ]
        assert len(matching) == 1
        assert matching[0]["log_level"] == "warning"

    def test_negative_resolution_is_cached(self):
        # After a failed resolution, subsequent `cache` accesses should not
        # re-invoke the resolver — they short-circuit to None via the
        # `_cache_resolution_attempted` sentinel.
        svc = ReplayService(repository=MagicMock(), cache=None)

        with patch(
            "baldur.factory.ProviderRegistry.get_cache",
            side_effect=RuntimeError("boom"),
        ) as mock_get:
            _ = svc.cache
            _ = svc.cache
            _ = svc.cache

        assert mock_get.call_count == 1


# =============================================================================
# replay_on_circuit_close inflight guard — Behavior
# =============================================================================


class TestReplayOnCircuitCloseInflightBehavior:
    """Inflight DistributedLock acquire/block/release paths (D4)."""

    @pytest.fixture(autouse=True)
    def _require_pro(self):
        pytest.importorskip("baldur_pro")

    def test_first_call_acquires_and_releases_lock(self):
        cache = InMemoryCacheAdapter()
        svc = _make_service(cache=cache)

        # When: a single call completes normally.
        with _passthrough_governance():
            result = svc.replay_on_circuit_close(
                service_name="svc",
                service_failure_type_map={"svc": ["TYPE_A"]},
            )

        # Then: not skipped, and the inflight lock is released — a fresh
        # acquire on the same lock name succeeds.
        assert result.inflight_skipped is False
        assert _can_acquire(cache, "svc")

    def test_concurrent_callers_only_one_proceeds(self):
        # Given: two threads racing on the same service_name through the
        # same shared cache.
        cache = InMemoryCacheAdapter()
        svc = _make_service(cache=cache)

        # Hold the sweep so both threads race on the lock acquire, not just
        # the nearly-instant find_replayable path. Block inside the locked
        # body.
        gate = threading.Event()
        original_locked = svc._replay_on_circuit_close_locked

        def slow_locked(**kwargs):
            gate.wait(timeout=5.0)
            return original_locked(**kwargs)

        svc._replay_on_circuit_close_locked = slow_locked

        barrier = threading.Barrier(2)
        results: list[BatchReplayResult] = []
        results_lock = threading.Lock()

        def worker():
            barrier.wait()
            with _passthrough_governance():
                r = svc.replay_on_circuit_close(
                    service_name="svc",
                    service_failure_type_map={"svc": ["TYPE_A"]},
                )
            with results_lock:
                results.append(r)

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for t in threads:
            t.start()
        # Let both threads contend on the inflight lock, then release the
        # winner from its slow_locked body.
        time.sleep(0.1)
        gate.set()
        for t in threads:
            t.join()

        # Then: exactly one inflight_skipped=True (the loser); the other
        # proceeded normally.
        skipped = [r for r in results if r.inflight_skipped]
        proceeded = [r for r in results if not r.inflight_skipped]
        assert len(skipped) == 1
        assert len(proceeded) == 1

    def test_stale_lock_reclaimed_after_holder_expires(self):
        # Given: a holder acquires the inflight lock with a very short TTL,
        # then doesn't release. DistributedLock's auto-expiry should let a
        # later acquire reclaim the slot — this is the cross-pod / crash-
        # recovery guarantee that lets a stuck holder eventually be
        # superseded.
        cache = InMemoryCacheAdapter()
        svc = _make_service(cache=cache)

        stale_holder = cache.get_lock(
            _replay_inflight_lock_name("svc"),
            timeout=timedelta(milliseconds=50),
        )
        assert stale_holder.acquire(blocking=False) is True

        # Wait for the holder's TTL to lapse.
        time.sleep(0.1)

        # When: a fresh sweep runs after the stale holder expired.
        with _passthrough_governance():
            result = svc.replay_on_circuit_close(
                service_name="svc",
                service_failure_type_map={"svc": ["TYPE_A"]},
            )

        # Then: the sweep proceeds (DistributedLock reclaimed the expired
        # slot via its internal `_is_expired` path) and releases on exit.
        assert result.inflight_skipped is False
        assert _can_acquire(cache, "svc")

    def test_lock_released_on_inner_exception(self):
        # Given: the sweep body raises.
        cache = InMemoryCacheAdapter()
        svc = _make_service(cache=cache)
        svc._replay_on_circuit_close_locked = MagicMock(
            side_effect=RuntimeError("boom")
        )

        # When.
        with _passthrough_governance(), pytest.raises(RuntimeError, match="boom"):
            svc.replay_on_circuit_close(
                service_name="svc",
                service_failure_type_map={"svc": ["TYPE_A"]},
            )

        # Then: the `finally` clause releases the lock so subsequent CB
        # recoveries are not permanently blocked.
        assert _can_acquire(cache, "svc")

    def test_fail_open_when_cache_is_none(self):
        # Given: no cache available — the guard MUST NOT block legitimate
        # CB-recovery replay.
        svc = _make_service(cache=None)
        # Prevent lazy-resolution from picking up anything.
        svc._cache_resolution_attempted = True

        # When.
        with _passthrough_governance():
            result = svc.replay_on_circuit_close(
                service_name="svc",
                service_failure_type_map={"svc": ["TYPE_A"]},
            )

        # Then: the sweep proceeded (fail-open), inflight_skipped is False.
        assert result.inflight_skipped is False

    def test_fail_open_when_get_lock_raises_warning_logged(self):
        # Given: a cache whose `get_lock` raises — fail-open.
        cache = InMemoryCacheAdapter()
        cache.get_lock = MagicMock(side_effect=RuntimeError("cache down"))
        svc = _make_service(cache=cache)

        with _passthrough_governance(), capture_logs() as cap_logs:
            result = svc.replay_on_circuit_close(
                service_name="svc",
                service_failure_type_map={"svc": ["TYPE_A"]},
            )

        # Then: sweep proceeds, fail-open WARNING is logged with the
        # `lock_unavailable` reason.
        assert result.inflight_skipped is False
        matching = [
            entry
            for entry in cap_logs
            if entry.get("event") == "replay_service.inflight_cache_unavailable"
            and entry.get("reason") == "lock_unavailable"
        ]
        assert len(matching) == 1
        assert matching[0]["log_level"] == "warning"

    def test_blocked_call_emits_dlq_replay_blocked_with_inflight_reason(self):
        cache = InMemoryCacheAdapter()
        svc = _make_service(cache=cache)
        # Pre-acquire the lock so the next call hits the blocked branch.
        holder = cache.get_lock(_replay_inflight_lock_name("svc"))
        assert holder.acquire(blocking=False) is True

        try:
            with (
                _passthrough_governance(),
                patch(
                    "baldur.services.replay_service.service.log_dlq_replay_blocked_audit"
                ) as mock_audit,
            ):
                result = svc.replay_on_circuit_close(
                    service_name="svc",
                    service_failure_type_map={"svc": ["TYPE_A"]},
                )

            # Then: inflight_skipped=True, full 4-channel block surface emitted.
            assert result.inflight_skipped is True

            # Event channel.
            blocked_emits = [
                c
                for c in svc._event_bus.emit.call_args_list
                if c[0][0] == EventType.DLQ_REPLAY_BLOCKED
            ]
            assert len(blocked_emits) == 1
            data = blocked_emits[0][1]["data"]
            assert data["trigger"] == "circuit_close"
            assert data["service_name"] == "svc"
            assert data["block_reason"] == REASON_CIRCUIT_CLOSE_INFLIGHT

            # Audit channel.
            mock_audit.assert_called_once()
            audit_kwargs = mock_audit.call_args.kwargs
            assert audit_kwargs["reason"] == REASON_CIRCUIT_CLOSE_INFLIGHT
            assert audit_kwargs["service_name"] == "svc"
            assert audit_kwargs["trigger"] == "circuit_close"
        finally:
            holder.release()

    def test_cross_instance_lock_shared_via_cache(self):
        # Given: two ReplayService instances sharing the same InMemoryCache.
        # The cache-backed lock should span instances — surrogate for the
        # multi-pod deployment shape.
        cache = InMemoryCacheAdapter()
        _ = _make_service(cache=cache)  # svc_a — only used to prove sharing
        svc_b = _make_service(cache=cache)

        # Pre-acquire via a separate lock handle (surrogate for "svc_a
        # mid-sweep on another worker") on the same cache.
        holder = cache.get_lock(_replay_inflight_lock_name("svc"))
        assert holder.acquire(blocking=False) is True

        try:
            # When: svc_b attempts a sweep for the same service.
            with (
                _passthrough_governance(),
                patch(
                    "baldur.services.replay_service.service.log_dlq_replay_blocked_audit"
                ),
            ):
                result = svc_b.replay_on_circuit_close(
                    service_name="svc",
                    service_failure_type_map={"svc": ["TYPE_A"]},
                )

            # Then: svc_b is blocked — the cache-backed lock crosses instances.
            assert result.inflight_skipped is True
        finally:
            holder.release()
