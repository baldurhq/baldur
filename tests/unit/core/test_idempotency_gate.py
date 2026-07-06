"""Unit tests for core/idempotency_gate.py — IdempotencyGate.

Verification techniques applied:
- Contract: IdempotencyDecision enum values, IDEMPOTENCY_DEFAULT_TTL_SECONDS
- State transition: CONTINUE→SKIP (via mark_completed), CONTINUE→ABORT (concurrent)
- Idempotency: duplicate check returns SKIP
- Edge case: cache=None (no-op mode), non-dict existing value
- Singleton lifecycle: get_idempotency_gate / reset_idempotency_gate
- 595 D3/D5: ``mark_*`` optional ``ttl`` (None → settings memory default,
  explicit → forwarded to cas_dict_field); constructor window split
  (``execution_ttl_seconds`` / ``memory_ttl_seconds``); per-use settings
  resolution (env retune via reset_idempotency_settings).
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from unittest.mock import patch

import pytest
from hypothesis import given
from hypothesis import settings as hyp_settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, rule

from baldur.core.exceptions import AdapterConnectionError, ConfigurationError
from baldur.core.idempotency_gate import (
    IDEMPOTENCY_DEFAULT_TTL_SECONDS,
    IdempotencyDecision,
    IdempotencyGate,
    get_idempotency_gate,
    reset_idempotency_gate,
)


class _FakeAtomicCache:
    """Fake cache with atomic setnx + cas_dict_field + cas_takeover for testing.

    Implements just enough to pass _validate_atomic_setnx /
    _validate_atomic_cas_dict_field / _validate_atomic_cas_takeover and support
    test scenarios with controllable return values.

    ``cas_takeover_return`` forces the atomic takeover to lose (return False,
    simulating another process winning the race) regardless of record state.
    """

    def __init__(self, setnx_return: bool = True, cas_takeover_return: bool = True):
        self._store: dict[str, Any] = {}
        self._setnx_return = setnx_return
        self._cas_takeover_return = cas_takeover_return
        self._setnx_calls: list[tuple] = []
        self._set_calls: list[tuple] = []
        self._delete_calls: list[str] = []
        self._get_calls: list[str] = []
        self._cas_calls: list[tuple] = []
        self._cas_takeover_calls: list[tuple] = []

    def setnx(self, key: str, value: Any, ttl=None) -> bool:
        self._setnx_calls.append((key, value, ttl))
        if key not in self._store:
            if self._setnx_return:
                self._store[key] = value
                return True
            return False
        return False

    def get(self, key: str) -> Any:
        self._get_calls.append(key)
        return self._store.get(key)

    def set(self, key: str, value: Any, ttl=None) -> bool:
        self._set_calls.append((key, value, ttl))
        self._store[key] = value
        return True

    def delete(self, key: str) -> bool:
        self._delete_calls.append(key)
        return self._store.pop(key, None) is not None

    def exists(self, key: str) -> bool:
        return key in self._store

    def cas_dict_field(
        self,
        key: str,
        field: str,
        expected: Any,
        new_value: dict[str, Any],
        ttl=None,
    ) -> bool:
        self._cas_calls.append((key, field, expected, new_value, ttl))
        existing = self._store.get(key)
        if not isinstance(existing, dict):
            return False
        if existing.get(field) != expected:
            return False
        self._store[key] = new_value
        return True

    def cas_takeover(
        self,
        key: str,
        new_record: dict[str, Any],
        *,
        stale_before: float,
        ttl=None,
    ) -> bool:
        self._cas_takeover_calls.append((key, new_record, stale_before, ttl))
        if not self._cas_takeover_return:
            return False
        existing = self._store.get(key)
        if not isinstance(existing, dict):
            return False
        status = existing.get("status")
        takeable = status == "failed" or (
            status == "executing" and existing.get("started_at", 0) < stale_before
        )
        if not takeable:
            return False
        self._store[key] = new_record
        return True


def _make_mock_cache(setnx_return: bool = True) -> _FakeAtomicCache:
    """Create a fake cache with atomic setnx for IdempotencyGate tests."""
    return _FakeAtomicCache(setnx_return=setnx_return)


# ── Contract Tests ──────────────────────────────────────────


class TestIdempotencyGateContract:
    """Design contract values for IdempotencyGate."""

    def test_default_ttl_is_1800_seconds(self):
        """IDEMPOTENCY_DEFAULT_TTL_SECONDS design contract: 1800 (30 min)."""
        assert IDEMPOTENCY_DEFAULT_TTL_SECONDS == 1800

    def test_decision_continue_value(self):
        """IdempotencyDecision.CONTINUE == 'continue'."""
        assert IdempotencyDecision.CONTINUE == "continue"

    def test_decision_skip_value(self):
        """IdempotencyDecision.SKIP == 'skip'."""
        assert IdempotencyDecision.SKIP == "skip"

    def test_decision_abort_value(self):
        """IdempotencyDecision.ABORT == 'abort'."""
        assert IdempotencyDecision.ABORT == "abort"

    def test_decision_is_str_enum(self):
        """IdempotencyDecision values are JSON-serializable strings."""
        for member in IdempotencyDecision:
            assert isinstance(member.value, str)

    def test_ctor_execution_default_is_module_constant(self):
        """595 D5 ctor split: ``execution_ttl_seconds`` defaults to the plain
        module constant (1800 s), NOT a settings read."""
        gate = IdempotencyGate()
        assert gate._execution_ttl_seconds == IDEMPOTENCY_DEFAULT_TTL_SECONDS

    def test_ctor_memory_default_is_none_sentinel(self):
        """595 D5 ctor split: ``memory_ttl_seconds`` defaults to the ``None``
        sentinel (→ per-use settings resolution at mark time)."""
        gate = IdempotencyGate()
        assert gate._memory_ttl_seconds is None

    def test_check_and_acquire_none_ttl_uses_execution_default_not_memory_setting(
        self, monkeypatch
    ):
        """595 D2/D5: the acquire path's default window is the execution
        constant — tuning the memory setting must not change it."""
        from baldur.settings.idempotency import reset_idempotency_settings

        monkeypatch.setenv("BALDUR_IDEMPOTENCY_GATE_MEMORY_TTL_SECONDS", "120")
        reset_idempotency_settings()
        try:
            cache = _make_mock_cache(setnx_return=True)
            gate = IdempotencyGate(cache=cache)

            gate.check_and_acquire("key-exec-default")

            assert cache._setnx_calls[0][2] == timedelta(
                seconds=IDEMPOTENCY_DEFAULT_TTL_SECONDS
            )
        finally:
            reset_idempotency_settings()


# ── Behavior Tests ──────────────────────────────────────────


class TestIdempotencyGateNoCacheBehavior:
    """Behavior when cache is None (no-op mode)."""

    def test_no_cache_always_returns_continue(self):
        """cache=None always returns CONTINUE."""
        gate = IdempotencyGate(cache=None)
        result = gate.check_and_acquire("any-key")
        assert result.decision == IdempotencyDecision.CONTINUE

    def test_no_cache_mark_completed_is_noop(self):
        """mark_completed is a no-op when cache=None."""
        gate = IdempotencyGate(cache=None)
        gate.mark_completed("key", {"result": "ok"})  # Should not raise

    def test_no_cache_mark_failed_is_noop(self):
        """mark_failed is a no-op when cache=None."""
        gate = IdempotencyGate(cache=None)
        gate.mark_failed("key", "error")  # Should not raise


class TestIdempotencyGateCheckAndAcquireBehavior:
    """State transition behavior for check_and_acquire."""

    def test_first_check_returns_continue(self):
        """First check: setnx succeeds → CONTINUE via the fast path (no get)."""
        cache = _make_mock_cache(setnx_return=True)
        gate = IdempotencyGate(cache=cache)
        result = gate.check_and_acquire("key-1")
        assert result.decision == IdempotencyDecision.CONTINUE
        # The winning fast-path acquire reads nothing and issues exactly one
        # setnx; skipping the initial setnx would fall through to the get()-based
        # existing-record check instead of returning straight away.
        assert cache._get_calls == []
        assert len(cache._setnx_calls) == 1

    def test_completed_status_returns_skip_with_cached_result(self):
        """Key in completed status → SKIP + cached_result."""
        cache = _make_mock_cache(setnx_return=False)
        # Pre-populate with completed record
        cache._store["key-done"] = {
            "status": "completed",
            "result": {"output": "success"},
            "retry_count": 1,
        }

        gate = IdempotencyGate(cache=cache)
        result = gate.check_and_acquire("key-done")

        assert result.decision == IdempotencyDecision.SKIP
        assert result.cached_result == {"output": "success"}
        assert result.retry_count == 1

    def test_executing_status_within_ttl_returns_abort(self):
        """executing status within TTL → ABORT, with no takeover.

        setnx_return=True so that a mis-computed elapsed (e.g. a sign flip
        treating a fresh claim as stale) would visibly re-acquire and CONTINUE;
        the in-TTL claim must instead be left untouched — no delete, no
        re-acquire — so a double execution cannot slip through.
        """
        import time

        cache = _make_mock_cache(setnx_return=True)
        cache._store["key-running"] = {
            "status": "executing",
            "started_at": time.time(),
            "retry_count": 0,
        }

        gate = IdempotencyGate(cache=cache)
        result = gate.check_and_acquire("key-running")
        assert result.decision == IdempotencyDecision.ABORT
        assert "key-running" not in cache._delete_calls

    def test_failed_status_returns_continue_via_cas_takeover(self):
        """failed status → atomic cas_takeover wins → CONTINUE, retry incremented."""
        cache = _make_mock_cache(setnx_return=True)
        cache._store["key-failed"] = {
            "status": "failed",
            "retry_count": 2,
        }

        gate = IdempotencyGate(cache=cache)
        result = gate.check_and_acquire("key-failed")

        assert result.decision == IdempotencyDecision.CONTINUE
        assert result.retry_count == 3
        # Single atomic takeover — no delete+setnx two-step (that non-atomic
        # rebuild is exactly the retry-path double-execute this fix closes).
        assert cache._delete_calls == []
        assert len(cache._cas_takeover_calls) == 1
        # The takeover must claim the SAME key with a fresh executing record for
        # the execution window — a wrong key/value/ttl means the claim is not on
        # the deduplicated operation (duplicate execution risk).
        call = cache._cas_takeover_calls[-1]
        assert call[0] == "key-failed"
        assert call[1]["status"] == "executing"
        assert call[1]["retry_count"] == 3
        assert call[3] == timedelta(seconds=IDEMPOTENCY_DEFAULT_TTL_SECONDS)

    def test_failed_status_cas_takeover_race_returns_abort(self):
        """failed status → another process wins the atomic takeover → ABORT."""
        cache = _FakeAtomicCache(cas_takeover_return=False)
        cache._store["key-failed"] = {
            "status": "failed",
            "retry_count": 1,
        }

        gate = IdempotencyGate(cache=cache)
        result = gate.check_and_acquire("key-failed")

        # cas_takeover_return=False simulates another process winning the race.
        assert result.decision == IdempotencyDecision.ABORT

    def test_stale_executing_returns_continue_via_cas_takeover(self):
        """executing status past TTL → atomic cas_takeover wins → CONTINUE."""
        import time

        cache = _make_mock_cache(setnx_return=True)
        cache._store["key-stale"] = {
            "status": "executing",
            "started_at": time.time() - 7200,  # 2 hours ago (TTL=1800s)
            "retry_count": 1,
        }

        gate = IdempotencyGate(cache=cache)
        result = gate.check_and_acquire("key-stale")

        assert result.decision == IdempotencyDecision.CONTINUE
        assert result.retry_count == 2
        assert cache._delete_calls == []
        assert len(cache._cas_takeover_calls) == 1
        # The stale-takeover must claim the SAME key as a fresh executing record.
        call = cache._cas_takeover_calls[-1]
        assert call[0] == "key-stale"
        assert call[1]["status"] == "executing"
        assert call[3] == timedelta(seconds=IDEMPOTENCY_DEFAULT_TTL_SECONDS)

    def test_stale_executing_cas_takeover_race_returns_abort(self):
        """executing status past TTL → atomic takeover lost to a peer → ABORT."""
        import time

        cache = _FakeAtomicCache(cas_takeover_return=False)
        cache._store["key-stale"] = {
            "status": "executing",
            "started_at": time.time() - 7200,
            "retry_count": 0,
        }

        gate = IdempotencyGate(cache=cache)
        result = gate.check_and_acquire("key-stale")

        assert result.decision == IdempotencyDecision.ABORT

    def test_unknown_status_returns_abort(self):
        """Unknown status → ABORT (defensive)."""
        cache = _make_mock_cache(setnx_return=False)
        cache._store["key-weird"] = {"status": "unknown_state"}

        gate = IdempotencyGate(cache=cache)
        result = gate.check_and_acquire("key-weird")
        assert result.decision == IdempotencyDecision.ABORT

    def test_non_dict_existing_value_returns_abort(self):
        """Non-dict existing value → ABORT."""
        cache = _make_mock_cache(setnx_return=False)
        cache._store["key-bad"] = "not-a-dict"

        gate = IdempotencyGate(cache=cache)
        result = gate.check_and_acquire("key-bad")
        assert result.decision == IdempotencyDecision.ABORT

    def test_race_existing_expired_between_setnx_and_get_reacquires(self):
        """Race window: the initial setnx loses (key held) but get() returns
        None because the key expired in the gap → a single re-setnx re-acquires
        and the decision is CONTINUE (not a spurious ABORT)."""

        class _RaceCache(_FakeAtomicCache):
            # Initial acquire loses the race (and does NOT store); the post-get
            # retry wins — modelling a key that expired between setnx and get.
            def setnx(self, key, value, ttl=None):
                self._setnx_calls.append((key, value, ttl))
                if len(self._setnx_calls) == 1:
                    return False
                self._store[key] = value
                return True

        cache = _RaceCache()
        gate = IdempotencyGate(cache=cache)

        result = gate.check_and_acquire("key-race")

        assert result.decision == IdempotencyDecision.CONTINUE
        assert cache._get_calls == ["key-race"]  # the existing-None path ran
        assert len(cache._setnx_calls) == 2  # initial loss + winning retry
        retry_call = cache._setnx_calls[-1]
        assert retry_call[0] == "key-race"
        assert retry_call[1]["status"] == "executing"
        assert retry_call[2] == timedelta(seconds=IDEMPOTENCY_DEFAULT_TTL_SECONDS)

    def test_race_existing_none_retry_lost_aborts(self):
        """Race window: initial setnx loses, get() returns None (expired), and
        the re-setnx ALSO loses (another process won) → ABORT."""
        # setnx_return=False: every setnx loses and never stores, so the store
        # stays empty → get() returns None → the existing-None branch is taken.
        cache = _make_mock_cache(setnx_return=False)

        gate = IdempotencyGate(cache=cache)
        result = gate.check_and_acquire("key-race-lost")

        assert result.decision == IdempotencyDecision.ABORT
        assert cache._get_calls == ["key-race-lost"]
        assert len(cache._setnx_calls) == 2  # initial loss + retry loss

    def test_stale_executing_at_exact_ttl_is_not_yet_stale(self):
        """Boundary: elapsed == ttl exactly is NOT yet stale (strict ``>``), so
        the in-flight claim is still honored → ABORT, no takeover. One instant
        later it would be stale; pinning the exact boundary catches a ``>`` →
        ``>=`` drift that would take over a claim a tick too early."""
        cache = _make_mock_cache(setnx_return=True)
        cache._store["key-bnd"] = {
            "status": "executing",
            "started_at": 1000.0,
            "retry_count": 0,
        }
        gate = IdempotencyGate(cache=cache, execution_ttl_seconds=1800)

        # elapsed = now(2800) - started_at(1000) == ttl(1800) exactly.
        with patch(
            "baldur.core.idempotency_gate.time.time", return_value=1000.0 + 1800
        ):
            result = gate.check_and_acquire("key-bnd")

        assert result.decision == IdempotencyDecision.ABORT
        assert "key-bnd" not in cache._delete_calls

    def test_explicit_ttl_is_forwarded_to_acquire_setnx(self):
        """A per-call ttl on check_and_acquire bounds the EXECUTING claim — it
        must reach the acquiring setnx, not be dropped for the default window."""
        cache = _make_mock_cache(setnx_return=True)
        gate = IdempotencyGate(cache=cache)
        custom = timedelta(seconds=42)

        gate.check_and_acquire("key-customttl", ttl=custom)

        assert cache._setnx_calls[0][2] == custom


class TestIdempotencyGateMarkBehavior:
    """mark_completed / mark_failed transition behavior."""

    def test_mark_completed_sets_completed_status(self):
        """mark_completed stores the record with status='completed'."""
        cache = _make_mock_cache()
        cache._store["key-1"] = {"status": "executing", "retry_count": 1}

        gate = IdempotencyGate(cache=cache)
        gate.mark_completed("key-1", result={"data": "ok"}, retry_count=1)

        saved = cache._store["key-1"]
        assert saved["status"] == "completed"
        assert saved["result"] == {"data": "ok"}
        assert saved["retry_count"] == 1

    def test_mark_failed_sets_failed_status(self):
        """mark_failed stores the record with status='failed'."""
        cache = _make_mock_cache()
        cache._store["key-1"] = {"status": "executing", "retry_count": 0}

        gate = IdempotencyGate(cache=cache)
        gate.mark_failed("key-1", error="step crashed")

        saved = cache._store["key-1"]
        assert saved["status"] == "failed"
        assert saved["error"] == "step crashed"

    def test_mark_completed_does_not_read_via_get(self):
        """mark_completed does not read via cache.get (G1 regression guard)."""
        cache = _make_mock_cache()
        cache._store["key-1"] = {"status": "executing", "retry_count": 0}

        gate = IdempotencyGate(cache=cache)
        gate.mark_completed("key-1", result={"data": "ok"})

        assert "key-1" not in cache._get_calls
        assert any(call[0] == "key-1" for call in cache._cas_calls)

    def test_mark_failed_does_not_read_via_get(self):
        """mark_failed does not read via cache.get (G2 regression guard)."""
        cache = _make_mock_cache()
        cache._store["key-1"] = {"status": "executing", "retry_count": 0}

        gate = IdempotencyGate(cache=cache)
        gate.mark_failed("key-1", error="boom")

        assert "key-1" not in cache._get_calls
        assert any(call[0] == "key-1" for call in cache._cas_calls)

    def test_mark_completed_cas_conflict_skips_write(self):
        """mark_completed does not overwrite the record when status is not executing."""
        cache = _make_mock_cache()
        cache._store["key-1"] = {"status": "completed", "retry_count": 0}

        gate = IdempotencyGate(cache=cache)
        gate.mark_completed("key-1", result={"data": "ok"})

        saved = cache._store["key-1"]
        assert saved["status"] == "completed"
        assert "result" not in saved or saved.get("result") != {"data": "ok"}


class TestIdempotencyGateValidationBehavior:
    """Atomic setnx + cas_dict_field validation behavior."""

    def test_non_atomic_setnx_raises_configuration_error(self):
        """A non-atomic setnx implementation raises ConfigurationError."""
        from baldur.interfaces.cache_provider import CacheProviderInterface

        class BadCache(CacheProviderInterface):
            """Cache that inherits non-atomic setnx from base."""

            @property
            def provider_name(self) -> str:
                return "bad_cache"

            def get(self, key): ...
            def set(self, key, value, ttl=None): ...
            def delete(self, key): ...
            def exists(self, key): ...
            def incr(self, key, amount=1): ...
            def decr(self, key, amount=1): ...
            def expire(self, key, ttl): ...
            def ttl(self, key): ...
            def get_lock(self, name, timeout=None, blocking_timeout=None): ...
            def mget(self, keys): ...
            def mset(self, mapping, ttl=None): ...
            def health_check(self): ...
            def flush_all(self): ...
            def ping(self): ...

        with pytest.raises(ConfigurationError, match="atomic setnx"):
            IdempotencyGate(cache=BadCache())

    def test_non_atomic_cas_dict_field_raises_configuration_error(self):
        """A non-atomic cas_dict_field implementation raises ConfigurationError."""
        from baldur.interfaces.cache_provider import CacheProviderInterface

        class BadCacheCAS(CacheProviderInterface):
            """Cache with atomic setnx but non-atomic cas_dict_field (base default)."""

            @property
            def provider_name(self) -> str:
                return "bad_cache_cas"

            def setnx(self, key, value, ttl=None):
                return True

            def get(self, key): ...
            def set(self, key, value, ttl=None): ...
            def delete(self, key): ...
            def exists(self, key): ...
            def incr(self, key, amount=1): ...
            def decr(self, key, amount=1): ...
            def expire(self, key, ttl): ...
            def ttl(self, key): ...
            def get_lock(self, name, timeout=None, blocking_timeout=None): ...
            def mget(self, keys): ...
            def mset(self, mapping, ttl=None): ...
            def health_check(self): ...
            def flush_all(self): ...
            def ping(self): ...

        with pytest.raises(ConfigurationError, match="atomic cas_dict_field"):
            IdempotencyGate(cache=BadCacheCAS())

    def test_non_atomic_cas_takeover_raises_configuration_error(self):
        """673 D1: a cache with atomic setnx + cas_dict_field but the inherited
        base (non-atomic) ``cas_takeover`` is rejected at construction — the
        takeover validator runs AFTER the setnx/cas_dict_field checks, so this
        cache clears those and fails specifically on cas_takeover."""
        from baldur.interfaces.cache_provider import CacheProviderInterface

        class BadCacheTakeover(CacheProviderInterface):
            """Atomic setnx + cas_dict_field, but inherits base cas_takeover."""

            @property
            def provider_name(self) -> str:
                return "bad_cache_takeover"

            def setnx(self, key, value, ttl=None):
                return True

            def cas_dict_field(self, key, field, expected, new_value, ttl=None):
                return True

            # inherits the non-atomic base cas_takeover

            def get(self, key): ...
            def set(self, key, value, ttl=None): ...
            def delete(self, key): ...
            def exists(self, key): ...
            def incr(self, key, amount=1): ...
            def decr(self, key, amount=1): ...
            def expire(self, key, ttl): ...
            def ttl(self, key): ...
            def get_lock(self, name, timeout=None, blocking_timeout=None): ...
            def mget(self, keys): ...
            def mset(self, mapping, ttl=None): ...
            def health_check(self): ...
            def flush_all(self): ...
            def ping(self): ...

        with pytest.raises(ConfigurationError, match="atomic cas_takeover"):
            IdempotencyGate(cache=BadCacheTakeover())

    def test_metrics_wrapped_non_atomic_adapter_still_raises(self):
        """A non-atomic adapter is caught even behind the metrics decorator.

        Registry-resolved caches arrive wrapped in ``MetricsAwareCacheAdapter``,
        which overrides setnx/cas_dict_field to delegate — so an un-unwrapped
        validator would always pass and silently admit a non-atomic underlying
        adapter. The gate unwraps to the concrete adapter before validating, so
        the check still fires on the wrapped shape.
        """
        from baldur.adapters.cache.metrics_decorator import (
            MetricsAwareCacheAdapter,
        )
        from baldur.interfaces.cache_provider import CacheProviderInterface

        class NonAtomicAdapter(CacheProviderInterface):
            """Inherits the non-atomic setnx/cas_dict_field base defaults."""

            @property
            def provider_name(self) -> str:
                return "non_atomic"

            def get(self, key): ...
            def set(self, key, value, ttl=None): ...
            def delete(self, key): ...
            def exists(self, key): ...
            def incr(self, key, amount=1): ...
            def decr(self, key, amount=1): ...
            def expire(self, key, ttl): ...
            def ttl(self, key): ...
            def get_lock(self, name, timeout=None, blocking_timeout=None): ...
            def mget(self, keys): ...
            def mset(self, mapping, ttl=None): ...
            def health_check(self): ...
            def flush_all(self): ...
            def ping(self): ...

        wrapped = MetricsAwareCacheAdapter(NonAtomicAdapter())
        with pytest.raises(ConfigurationError, match="atomic setnx"):
            IdempotencyGate(cache=wrapped)

    def test_metrics_wrapped_atomic_adapter_passes_and_is_stored_verbatim(self):
        """A metrics-wrapped atomic adapter (the production shape) validates.

        Unwrapping is for validation only — ``_cache`` retains the wrapped
        instance so cache ops still flow through the metrics decorator.
        """
        from baldur.adapters.cache.memory_adapter import InMemoryCacheAdapter
        from baldur.adapters.cache.metrics_decorator import (
            MetricsAwareCacheAdapter,
        )

        wrapped = MetricsAwareCacheAdapter(InMemoryCacheAdapter(key_prefix="t:"))
        gate = IdempotencyGate(cache=wrapped)

        assert gate._cache is wrapped


class TestIdempotencyGateDecisionMetricBehavior:
    """566 D9 — ``check_and_acquire`` records the decision on the real-cache path.

    The decision counter (``baldur_idempotency_gate_decision_total{decision}``)
    is recorded once at the gate, the single choke point shared by every
    consumer. The ``cache=None`` no-op path is deliberately un-metered so "no
    gate installed" is not conflated with "a real gate said continue".
    """

    def test_real_cache_continue_records_continue_decision(self):
        """setnx success → CONTINUE → records ``continue`` (D9)."""
        cache = _make_mock_cache(setnx_return=True)
        gate = IdempotencyGate(cache=cache)

        with patch("baldur.metrics.prometheus.get_metrics") as mock_get:
            result = gate.check_and_acquire("key-1")

        assert result.decision == IdempotencyDecision.CONTINUE
        mock_get.return_value.idempotency.record_gate_decision.assert_called_once_with(
            "continue"
        )

    def test_real_cache_skip_records_skip_decision(self):
        """completed record → SKIP → records ``skip`` (D9)."""
        cache = _make_mock_cache(setnx_return=False)
        cache._store["key-done"] = {
            "status": "completed",
            "result": {"output": "ok"},
            "retry_count": 0,
        }
        gate = IdempotencyGate(cache=cache)

        with patch("baldur.metrics.prometheus.get_metrics") as mock_get:
            result = gate.check_and_acquire("key-done")

        assert result.decision == IdempotencyDecision.SKIP
        mock_get.return_value.idempotency.record_gate_decision.assert_called_once_with(
            "skip"
        )

    def test_real_cache_abort_records_abort_decision(self):
        """executing-within-TTL record → ABORT → records ``abort`` (D9)."""
        import time

        cache = _make_mock_cache(setnx_return=False)
        cache._store["key-run"] = {
            "status": "executing",
            "started_at": time.time(),
            "retry_count": 0,
        }
        gate = IdempotencyGate(cache=cache)

        with patch("baldur.metrics.prometheus.get_metrics") as mock_get:
            result = gate.check_and_acquire("key-run")

        assert result.decision == IdempotencyDecision.ABORT
        mock_get.return_value.idempotency.record_gate_decision.assert_called_once_with(
            "abort"
        )

    def test_no_cache_noop_does_not_record(self):
        """The ``cache=None`` no-op path never touches the metrics registry (D9)."""
        gate = IdempotencyGate(cache=None)

        with patch("baldur.metrics.prometheus.get_metrics") as mock_get:
            result = gate.check_and_acquire("any-key")

        assert result.decision == IdempotencyDecision.CONTINUE
        # The early return precedes any metrics import — get_metrics is untouched.
        mock_get.assert_not_called()

    def test_metrics_failure_does_not_break_dedup(self):
        """Best-effort recording: a metrics failure cannot break the dedup path (R5)."""
        cache = _make_mock_cache(setnx_return=True)
        gate = IdempotencyGate(cache=cache)

        with patch(
            "baldur.metrics.prometheus.get_metrics",
            side_effect=RuntimeError("metrics down"),
        ):
            # No raise — the gate swallows observability failures.
            result = gate.check_and_acquire("key-1")

        assert result.decision == IdempotencyDecision.CONTINUE


class TestGateReleaseBehavior:
    """621 D6 — ``release()`` deletes the record, re-arming a future acquisition.

    Unlike ``mark_completed`` (which leaves a COMPLETED record that makes the
    next ``check_and_acquire`` SKIP), ``release`` clears the key entirely so the
    same logical key is re-acquirable — the cross-session re-arm the recovery
    compensation gate relies on. Idempotent and best-effort."""

    def test_release_makes_completed_key_reacquirable(self):
        """A released completed key is re-acquirable (CONTINUE, not SKIP)."""
        cache = _make_mock_cache(setnx_return=True)
        gate = IdempotencyGate(cache=cache)

        # Given — an acquired, then completed, record (would SKIP on re-check).
        gate.check_and_acquire("key-rearm")
        gate.mark_completed("key-rearm", result={"ok": True})
        assert gate.check_and_acquire("key-rearm").decision == IdempotencyDecision.SKIP

        # When — the record is released.
        gate.release("key-rearm")

        # Then — the key is re-acquirable.
        assert (
            gate.check_and_acquire("key-rearm").decision == IdempotencyDecision.CONTINUE
        )

    def test_release_deletes_the_record_from_cache(self):
        """release issues a single delete for the key."""
        cache = _make_mock_cache(setnx_return=True)
        gate = IdempotencyGate(cache=cache)
        gate.check_and_acquire("key-del")

        gate.release("key-del")

        assert "key-del" in cache._delete_calls
        assert "key-del" not in cache._store

    def test_release_missing_key_is_noop(self):
        """Releasing an absent key does not raise (idempotent)."""
        cache = _make_mock_cache(setnx_return=True)
        gate = IdempotencyGate(cache=cache)

        gate.release("never-seen")  # Should not raise

        assert (
            gate.check_and_acquire("never-seen").decision
            == IdempotencyDecision.CONTINUE
        )

    def test_release_no_cache_is_noop(self):
        """release is a no-op when cache=None (unconfigured gate)."""
        gate = IdempotencyGate(cache=None)
        gate.release("any-key")  # Should not raise

    def test_release_swallows_cache_error(self):
        """A cache delete failure is swallowed (best-effort)."""

        class _RaisingDeleteCache(_FakeAtomicCache):
            def delete(self, key):
                raise RuntimeError("cache down")

        gate = IdempotencyGate(cache=_RaisingDeleteCache())

        gate.release("key-boom")  # Should not raise


class TestIdempotencyGateSingletonBehavior:
    """Singleton lifecycle behavior."""

    def setup_method(self):
        reset_idempotency_gate()

    def teardown_method(self):
        reset_idempotency_gate()

    def test_get_returns_same_instance(self):
        """get_idempotency_gate() returns the same instance."""
        first = get_idempotency_gate()
        second = get_idempotency_gate()
        assert first is second

    def test_reset_clears_cached_instance(self):
        """A new instance is created after reset."""
        first = get_idempotency_gate()
        reset_idempotency_gate()
        second = get_idempotency_gate()
        assert first is not second

    def test_default_singleton_has_no_cache(self):
        """The default singleton has cache=None (no-op mode)."""
        gate = get_idempotency_gate()
        assert gate._cache is None


# ── 595 D3 — mark_* optional ttl (memory window) ────────────


class TestIdempotencyGateMarkTtlBehavior:
    """595 D3: ``mark_completed`` / ``mark_failed`` accept an optional memory
    ``ttl`` — explicit values forward verbatim to ``cas_dict_field``; ``None``
    resolves to the settings-driven memory default per use."""

    @pytest.fixture(autouse=True)
    def _reset_settings(self):
        from baldur.settings.idempotency import reset_idempotency_settings

        reset_idempotency_settings()
        yield
        reset_idempotency_settings()

    @staticmethod
    def _gate_with_executing_record(
        key: str,
    ) -> tuple[IdempotencyGate, _FakeAtomicCache]:
        cache = _make_mock_cache()
        cache._store[key] = {"status": "executing", "retry_count": 0}
        return IdempotencyGate(cache=cache), cache

    @pytest.mark.parametrize(
        "mark", ["mark_completed", "mark_failed"], ids=["completed", "failed"]
    )
    def test_explicit_ttl_forwarded_to_cas_dict_field(self, mark):
        """An explicit memory ttl reaches cas_dict_field unchanged."""
        gate, cache = self._gate_with_executing_record("key-ttl")
        explicit = timedelta(hours=2)

        getattr(gate, mark)("key-ttl", ttl=explicit)

        assert cache._cas_calls[0][4] is explicit

    @pytest.mark.parametrize(
        "mark", ["mark_completed", "mark_failed"], ids=["completed", "failed"]
    )
    def test_none_ttl_resolves_to_settings_memory_default(self, mark):
        """ttl=None → ``IdempotencySettings.gate_memory_ttl_seconds``."""
        from baldur.settings.idempotency import get_idempotency_settings

        gate, cache = self._gate_with_executing_record("key-default")

        getattr(gate, mark)("key-default")

        expected = timedelta(seconds=get_idempotency_settings().gate_memory_ttl_seconds)
        assert cache._cas_calls[0][4] == expected


# ── 595 D5 — settings-driven memory default (per-use resolution) ──


class TestIdempotencyGateMemoryDefaultBehavior:
    """595 D5: the ``None``-sentinel memory window resolves from
    ``BALDUR_IDEMPOTENCY_GATE_MEMORY_TTL_SECONDS`` per use (runtime-retunable
    via ``reset_idempotency_settings``); an explicit constructor override
    bypasses the settings read entirely."""

    @pytest.fixture(autouse=True)
    def _reset_settings(self):
        from baldur.settings.idempotency import reset_idempotency_settings

        reset_idempotency_settings()
        yield
        reset_idempotency_settings()

    def test_layered_setting_tunes_memory_default(self):
        """The layered idempotency setting reaches cas_dict_field on a default mark
        (686 D3/D5: the window is read through the cached layered seam)."""
        from baldur.settings.idempotency import IdempotencySettings

        cache = _make_mock_cache()
        cache._store["k"] = {"status": "executing", "retry_count": 0}
        gate = IdempotencyGate(cache=cache)

        with patch(
            "baldur.settings.layered_provider.get_layered_settings_cached",
            return_value=IdempotencySettings(gate_memory_ttl_seconds=120),
        ):
            gate.mark_completed("k")

        assert cache._cas_calls[0][4] == timedelta(seconds=120)

    def test_retune_observed_after_read_cache_reset(self):
        """686 D3/D5: the memory window is read via the 30s layered snapshot cache,
        so within the TTL it is stable across marks on the same gate instance; a
        retune is observed on the next mark only after the cache elapses (simulated
        here with reset_layered_settings_cached), not per-mark."""
        from baldur.settings.idempotency import IdempotencySettings
        from baldur.settings.layered_provider import reset_layered_settings_cached

        window = {"ttl": 120}

        def _fake_layered(*args, **kwargs):
            return IdempotencySettings(gate_memory_ttl_seconds=window["ttl"])

        reset_layered_settings_cached()
        cache = _make_mock_cache()
        for key in ("k1", "k2", "k3"):
            cache._store[key] = {"status": "executing", "retry_count": 0}
        gate = IdempotencyGate(cache=cache)

        with patch(
            "baldur.settings.layered_provider.get_layered_settings",
            side_effect=_fake_layered,
        ):
            gate.mark_completed("k1")  # reads 120, caches the snapshot
            window["ttl"] = 240  # operator retunes
            gate.mark_failed("k2")  # still 120 — cached within the TTL
            reset_layered_settings_cached()  # TTL elapses
            gate.mark_completed("k3")  # now observes 240

        assert cache._cas_calls[0][4] == timedelta(seconds=120)
        assert cache._cas_calls[1][4] == timedelta(seconds=120)
        assert cache._cas_calls[2][4] == timedelta(seconds=240)

    def test_ctor_memory_override_bypasses_settings(self, monkeypatch):
        """An explicit ``memory_ttl_seconds=`` wins over the env setting."""
        from baldur.settings.idempotency import reset_idempotency_settings

        monkeypatch.setenv("BALDUR_IDEMPOTENCY_GATE_MEMORY_TTL_SECONDS", "120")
        reset_idempotency_settings()

        cache = _make_mock_cache()
        cache._store["k"] = {"status": "executing", "retry_count": 0}
        gate = IdempotencyGate(cache=cache, memory_ttl_seconds=900)

        gate.mark_completed("k")

        assert cache._cas_calls[0][4] == timedelta(seconds=900)


# ── 673 D1/1a — clock-skew-conservative stale_before ────────


class TestIdempotencyGateStaleBeforeToleranceBehavior:
    """673 1a: the staleness threshold subtracts
    ``IdempotencySettings.clock_skew_tolerance_seconds`` so a peer whose wall
    clock runs ahead cannot judge a still-running claim stale early and
    double-execute. ``stale_before = now - execution_ttl - tolerance``, read per
    call (runtime-retunable), and ``tolerance=0.0`` disables the margin."""

    @pytest.fixture(autouse=True)
    def _reset_settings(self):
        from baldur.settings.idempotency import reset_idempotency_settings

        reset_idempotency_settings()
        yield
        reset_idempotency_settings()

    _TOL_ENV = "BALDUR_IDEMPOTENCY_CLOCK_SKEW_TOLERANCE_SECONDS"
    _TIME = "baldur.core.idempotency_gate.time.time"

    def test_stale_before_subtracts_ttl_and_tolerance(self):
        """``_stale_before`` == ``now - ttl.total_seconds() - tolerance`` (686 D3/D5:
        tolerance read through the cached layered seam)."""
        from baldur.settings.idempotency import IdempotencySettings

        gate = IdempotencyGate(cache=_make_mock_cache())

        with (
            patch(
                "baldur.settings.layered_provider.get_layered_settings_cached",
                return_value=IdempotencySettings(clock_skew_tolerance_seconds=7.5),
            ),
            patch(self._TIME, return_value=10_000.0),
        ):
            value = gate._stale_before(timedelta(seconds=1800))

        assert value == 10_000.0 - 1800 - 7.5

    def _executing_gate(self, started_at: float):
        cache = _make_mock_cache(setnx_return=True)
        cache._store["k"] = {
            "status": "executing",
            "started_at": started_at,
            "retry_count": 0,
        }
        return IdempotencyGate(cache=cache, execution_ttl_seconds=1800), cache

    def test_claim_within_tolerance_margin_is_not_taken_over(self, monkeypatch):
        """A claim older than ``ttl`` but younger than ``ttl + tolerance`` is NOT
        stale — the tolerance margin protects it from a clock-ahead taker.

        started_at=1000, ttl=1800, tolerance=5 → elapsed 1802 s (now=2802):
        stale_before = 2802 - 1800 - 5 = 997; 1000 < 997 is False → ABORT.
        Without the tolerance (old behavior) stale_before would be 1002 and the
        claim would be wrongly taken over.
        """
        from baldur.settings.idempotency import reset_idempotency_settings

        monkeypatch.setenv(self._TOL_ENV, "5.0")
        reset_idempotency_settings()
        gate, cache = self._executing_gate(started_at=1000.0)

        with patch(self._TIME, return_value=1000.0 + 1802):
            result = gate.check_and_acquire("k")

        assert result.decision == IdempotencyDecision.ABORT
        assert cache._cas_takeover_calls == []  # no takeover attempted

    def test_claim_beyond_tolerance_margin_is_taken_over(self, monkeypatch):
        """A claim older than ``ttl + tolerance`` IS stale → takeover CONTINUEs.

        started_at=1000, ttl=1800, tolerance=5 → elapsed 1806 s (now=2806):
        stale_before = 2806 - 1805 = 1001; 1000 < 1001 → CONTINUE.
        """
        from baldur.settings.idempotency import reset_idempotency_settings

        monkeypatch.setenv(self._TOL_ENV, "5.0")
        reset_idempotency_settings()
        gate, cache = self._executing_gate(started_at=1000.0)

        with patch(self._TIME, return_value=1000.0 + 1806):
            result = gate.check_and_acquire("k")

        assert result.decision == IdempotencyDecision.CONTINUE
        assert len(cache._cas_takeover_calls) == 1

    def test_tolerance_zero_disables_the_margin(self):
        """``tolerance=0.0`` restores the bare ``now - ttl`` threshold: a claim a
        hair past ``ttl`` is immediately takeable (686 D3/D5: tolerance read through
        the cached layered seam)."""
        from baldur.settings.idempotency import IdempotencySettings

        gate, cache = self._executing_gate(started_at=1000.0)

        # elapsed 1801 s > ttl, and with no margin → stale → CONTINUE.
        with (
            patch(
                "baldur.settings.layered_provider.get_layered_settings_cached",
                return_value=IdempotencySettings(clock_skew_tolerance_seconds=0.0),
            ),
            patch(self._TIME, return_value=1000.0 + 1801),
        ):
            result = gate.check_and_acquire("k")

        assert result.decision == IdempotencyDecision.CONTINUE
        assert len(cache._cas_takeover_calls) == 1

    def test_same_stale_before_handed_to_selector_and_atomic_op(self, monkeypatch):
        """The Python selector and the atomic ``cas_takeover`` receive the SAME
        threshold (computed once), so they never disagree on staleness."""
        from baldur.settings.idempotency import reset_idempotency_settings

        monkeypatch.setenv(self._TOL_ENV, "5.0")
        reset_idempotency_settings()
        gate, cache = self._executing_gate(started_at=1000.0)

        with patch(self._TIME, return_value=1000.0 + 1806):
            gate.check_and_acquire("k")

        # cas_takeover call tuple: (key, new_record, stale_before, ttl)
        passed_stale_before = cache._cas_takeover_calls[-1][2]
        assert passed_stale_before == (1000.0 + 1806) - 1800 - 5.0


# ── 673 D9/3a — takeover metric ─────────────────────────────


class TestIdempotencyGateTakeoverMetricBehavior:
    """673 D9/3a: a won failed / stale takeover increments
    ``baldur_idempotency_gate_takeover_total{reason}`` with the correct reason; a
    fresh-key acquire and a LOST takeover do NOT increment it (the counter meters
    only won takeovers, so ``continue``'s decision meaning stays stable)."""

    def test_failed_takeover_records_reason_failed(self):
        cache = _make_mock_cache(setnx_return=True)
        cache._store["k"] = {"status": "failed", "retry_count": 0}
        gate = IdempotencyGate(cache=cache)

        with patch("baldur.metrics.prometheus.get_metrics") as mock_get:
            result = gate.check_and_acquire("k")

        assert result.decision == IdempotencyDecision.CONTINUE
        mock_get.return_value.idempotency.record_takeover.assert_called_once_with(
            "failed"
        )

    def test_stale_takeover_records_reason_stale(self):
        import time

        cache = _make_mock_cache(setnx_return=True)
        cache._store["k"] = {
            "status": "executing",
            "started_at": time.time() - 7200,  # 2h ago, TTL 1800s
            "retry_count": 0,
        }
        gate = IdempotencyGate(cache=cache)

        with patch("baldur.metrics.prometheus.get_metrics") as mock_get:
            result = gate.check_and_acquire("k")

        assert result.decision == IdempotencyDecision.CONTINUE
        mock_get.return_value.idempotency.record_takeover.assert_called_once_with(
            "stale"
        )

    def test_fresh_acquire_does_not_record_takeover(self):
        """A first-time (fresh-key) acquire CONTINUEs but is NOT a takeover."""
        cache = _make_mock_cache(setnx_return=True)
        gate = IdempotencyGate(cache=cache)

        with patch("baldur.metrics.prometheus.get_metrics") as mock_get:
            result = gate.check_and_acquire("fresh")

        assert result.decision == IdempotencyDecision.CONTINUE
        mock_get.return_value.idempotency.record_takeover.assert_not_called()

    def test_lost_takeover_does_not_record(self):
        """A takeover lost to a peer (cas_takeover → False) ABORTs and records no
        takeover — only the winner meters the event."""
        cache = _FakeAtomicCache(cas_takeover_return=False)
        cache._store["k"] = {"status": "failed", "retry_count": 0}
        gate = IdempotencyGate(cache=cache)

        with patch("baldur.metrics.prometheus.get_metrics") as mock_get:
            result = gate.check_and_acquire("k")

        assert result.decision == IdempotencyDecision.ABORT
        mock_get.return_value.idempotency.record_takeover.assert_not_called()


# ── 673 D8 — Hypothesis: G2 outage surfacing + G1 at-most-once ──


class _AcquireFailCache(_FakeAtomicCache):
    """A ``_FakeAtomicCache`` whose acquire ops (``setnx`` / ``cas_takeover``)
    raise ``AdapterConnectionError`` when their name is in ``failing`` — models a
    cache outage isolated to the un-swallowed dedup-gate acquire ops."""

    def __init__(self, failing: set[str]) -> None:
        super().__init__()
        self._failing = failing

    def setnx(self, key, value, ttl=None):
        if "setnx" in self._failing:
            raise AdapterConnectionError("setnx down")
        return super().setnx(key, value, ttl)

    def cas_takeover(self, key, new_record, *, stale_before, ttl=None):
        if "cas_takeover" in self._failing:
            raise AdapterConnectionError("cas_takeover down")
        return super().cas_takeover(key, new_record, stale_before=stale_before, ttl=ttl)


class TestIdempotencyGateOutageSurfacingProperty:
    """673 G2 (generated): for ANY nonempty subset of the gate's acquire ops
    failing with I/O error, ``check_and_acquire`` surfaces the outage (raises)
    rather than silently returning ABORT — so the guard/decorator fail-open /
    ``IdempotencyUnavailableError`` path is always reachable. Generated coverage
    replaces a hand-picked "all ops fail" example."""

    @hyp_settings(max_examples=50, deadline=None)
    @given(failing=st.sets(st.sampled_from(["setnx", "cas_takeover"]), min_size=1))
    def test_any_failing_acquire_op_surfaces_never_silent_abort(self, failing):
        # A pre-seeded FAILED record routes the path through setnx (loses, key
        # held) → get → cas_takeover, so BOTH acquire ops are on the path; any
        # nonempty failing subset must therefore raise.
        cache = _AcquireFailCache(failing)
        cache._store["k"] = {"status": "failed", "retry_count": 0}
        gate = IdempotencyGate(cache=cache)

        with pytest.raises(AdapterConnectionError):
            gate.check_and_acquire("k")


class _TakeoverSingleWinnerMachine(RuleBasedStateMachine):
    """N retriers race ``check_and_acquire`` on ONE pre-seeded failed record
    (none marking) against a real lock-atomic ``InMemoryCacheAdapter``. The
    atomic ``cas_takeover`` must elect AT MOST ONE ``CONTINUE`` across every
    generated schedule — the retry-path at-most-once invariant (G1). The
    pre-673 ``delete()+setnx()`` two-step would let two interleaved retriers both
    CONTINUE; Hypothesis shrinks any such violation to the minimal schedule."""

    def __init__(self) -> None:
        super().__init__()
        from baldur.adapters.cache.memory_adapter import InMemoryCacheAdapter

        self.cache = InMemoryCacheAdapter(key_prefix="sm:")
        self.cache.set("k", {"status": "failed", "retry_count": 0})
        self.gate = IdempotencyGate(cache=self.cache, execution_ttl_seconds=1800)
        self.continue_count = 0

    @rule()
    def a_retrier_acquires(self):
        result = self.gate.check_and_acquire("k")
        if result.decision == IdempotencyDecision.CONTINUE:
            self.continue_count += 1

    @invariant()
    def at_most_one_winner(self):
        assert self.continue_count <= 1, (
            f"double-execute: {self.continue_count} retriers won the takeover"
        )


_TakeoverSingleWinnerMachine.TestCase.settings = hyp_settings(
    max_examples=50, deadline=None, stateful_step_count=10
)
TestIdempotencyGateTakeoverAtMostOnce = _TakeoverSingleWinnerMachine.TestCase
