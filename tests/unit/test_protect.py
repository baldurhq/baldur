"""Unit tests for ``baldur.protect_facade`` — the single-call resilience facade (429 Part 1).

Scope:
- ``protect()`` / ``aprotect()``: return value contract, fallback, raise-on-failure.
- ``protect_with_meta()`` / ``aprotect_with_meta()``: ProtectResult DTO fields.
- ``@protected`` / ``@aprotected``: decorator wrapping + coroutine auto-detect.
- Flag resolution: kwargs override ProtectSettings defaults.
- Metrics emission: ProtectMetricRecorder called with outcome-matching labels.
- ``ProtectSettings.enabled=False``: bypass path calls ``fn`` directly.

Verification techniques: Contract (defaults), Behavior (fallback path, decorator
coroutine detection, metric interaction), Exception handling (raise propagation),
Idempotency (flag resolution).
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from baldur.interfaces.resilience_policy import PolicyOutcome
from baldur.protect_facade import (
    ProtectResult,
    _build_async_composer,
    _outcome_label,
    _resolve_flags,
    _resolve_retry_stage,
    aprotect,
    aprotect_with_meta,
    aprotected,
    protect,
    protect_with_meta,
    protected,
)
from baldur.services.retry_handler.models import RetryPolicyConfig


@pytest.fixture(autouse=True)
def _reset_protect_settings():
    """Ensure each test starts with a fresh ProtectSettings singleton."""
    from baldur.settings.protect import reset_protect_settings

    reset_protect_settings()
    yield
    reset_protect_settings()


# =============================================================================
# Contract — public surface
# =============================================================================


class TestProtectPublicApiContract:
    """baldur.protect_facade module must expose the 8 public symbols: the 7 facade
    entry points from 429 Part 1,
    plus ``reset_protect_caches`` (480
    DEC-6) — the test hook that flushes the per-name ``CircuitBreakerPolicy``
    cache."""

    def test_public_all_lists_exactly_eight_symbols(self):
        """Contract: __all__ declares the seven facade entry points plus
        ``reset_protect_caches``."""
        import baldur.protect_facade as module

        expected = {
            "ProtectResult",
            "protect",
            "aprotect",
            "protect_with_meta",
            "aprotect_with_meta",
            "protected",
            "aprotected",
            "reset_protect_caches",
        }
        assert set(module.__all__) == expected

    def test_protect_result_default_outcome_is_success(self):
        """Contract: ProtectResult() default outcome is PolicyOutcome.SUCCESS."""
        result: ProtectResult = ProtectResult()

        assert result.outcome == PolicyOutcome.SUCCESS
        assert result.success is True
        assert result.fallback_used is False
        assert result.attempts == 1
        assert result.duration_seconds == 0.0
        assert result.error is None


# =============================================================================
# Behavior — protect() happy path and fallback
# =============================================================================


class TestProtectBehavior:
    """Behavior verification for the sync ``protect()`` entry point."""

    def test_protect_returns_fn_value_on_success(self):
        """Success path returns whatever fn() returned."""
        result = protect(name="svc.success", fn=lambda: "ok")

        assert result == "ok"

    def test_protect_returns_fallback_value_when_fn_raises(self):
        """Given fn raises, protect returns the fallback()'s value."""

        # Given
        def bad():
            raise RuntimeError("boom")

        # When
        result = protect(
            name="svc.fallback",
            fn=bad,
            fallback=lambda: "fb",
        )

        # Then
        assert result == "fb"

    def test_protect_propagates_exception_when_no_fallback(self):
        """Without a fallback, the underlying exception is re-raised."""

        def bad():
            raise ValueError("nope")

        with pytest.raises(ValueError, match="nope"):
            protect(name="svc.raise", fn=bad)

    def test_protect_bypasses_pipeline_when_settings_disabled(self):
        """Contract: ProtectSettings.enabled=False → fn runs directly, no CB/Retry/Metric."""
        from baldur.settings.protect import ProtectSettings, reset_protect_settings

        # Given — replace the cached singleton with a disabled settings instance.
        reset_protect_settings()
        disabled = ProtectSettings(enabled=False)
        with patch(
            "baldur.settings.protect.get_protect_settings",
            return_value=disabled,
        ):
            result = protect(name="svc.bypass", fn=lambda: 42)

        assert result == 42


# =============================================================================
# Behavior — protect_with_meta() DTO fields
# =============================================================================


class TestProtectWithMetaBehavior:
    """Behavior tests for the opt-in ``protect_with_meta()`` DTO variant."""

    def test_with_meta_success_path_fields(self):
        """Success → ProtectResult(success=True, outcome=SUCCESS, attempts>=1)."""
        meta = protect_with_meta(name="meta.ok", fn=lambda: 1)

        assert meta.success is True
        assert meta.outcome == PolicyOutcome.SUCCESS
        assert meta.value == 1
        assert meta.fallback_used is False
        assert meta.attempts >= 1

    def test_with_meta_fallback_path_fields(self):
        """Fallback branch → success=True, fallback_used=True, outcome=SUCCESS_WITH_FALLBACK."""

        def bad():
            raise RuntimeError("x")

        meta = protect_with_meta(
            name="meta.fb",
            fn=bad,
            fallback=lambda: "fb",
        )

        assert meta.success is True
        assert meta.fallback_used is True
        assert meta.outcome == PolicyOutcome.SUCCESS_WITH_FALLBACK
        assert meta.value == "fb"

    def test_with_meta_failure_path_captures_error(self):
        """All-failed → success=False, error is the caught exception (no raise)."""
        err = ValueError("captured")

        def bad():
            raise err

        meta = protect_with_meta(name="meta.fail", fn=bad)

        assert meta.success is False
        assert meta.error is err


# =============================================================================
# Flag resolution — kwargs override ProtectSettings defaults
# =============================================================================


class TestFlagResolutionBehavior:
    """Behavior verification for ``_resolve_flags`` and ``_resolve_retry_stage``
    — the private helpers that merge per-call kwargs with ProtectSettings."""

    def test_resolve_flags_explicit_kwargs_override_settings(self):
        """Given explicit True/False kwargs, they win over ProtectSettings defaults."""
        dlq_flag, cb_flag = _resolve_flags(dlq=True, circuit_breaker=False)

        assert dlq_flag is True
        assert cb_flag is False

    def test_resolve_flags_none_falls_back_to_settings(self):
        """Given dlq=None and circuit_breaker=None, the settings defaults are used."""
        from baldur.settings.protect import get_protect_settings

        settings = get_protect_settings()
        dlq_flag, cb_flag = _resolve_flags(dlq=None, circuit_breaker=None)

        assert dlq_flag == settings.default_dlq
        assert cb_flag == settings.default_circuit_breaker

    def test_resolve_retry_stage_false_returns_pair_of_nones(self):
        """retry=False → (None, None, False) — pipeline omits Retry."""
        cfg, policy, settings_derived = _resolve_retry_stage(
            retry=False, dlq_requested=False, domain="x"
        )

        assert cfg is None
        assert policy is None
        assert settings_derived is False

    def test_resolve_retry_stage_true_resolves_from_settings(self):
        """retry=True → (RetryPolicyConfig.from_settings(domain), None, True).

        The True flag marks the cfg as settings-derived, gating the
        ``dlq_protect`` fast-path cache (#499 D5).
        """
        cfg, policy, settings_derived = _resolve_retry_stage(
            retry=True, dlq_requested=False, domain="svc.retry"
        )

        assert isinstance(cfg, RetryPolicyConfig)
        assert cfg.domain == "svc.retry"
        assert policy is None
        assert settings_derived is True

    def test_resolve_retry_stage_forces_enable_dlq_when_caller_requests_dlq(self):
        """When caller passes dlq=True, returned cfg has enable_dlq=True even if
        the user-provided config started with enable_dlq=False."""
        user_cfg = RetryPolicyConfig(enable_dlq=False, domain="svc.dlq")

        cfg, policy, settings_derived = _resolve_retry_stage(
            retry=user_cfg, dlq_requested=True, domain="svc.dlq"
        )

        assert cfg is not None
        assert cfg.enable_dlq is True
        # Original config is not mutated (immutability contract)
        assert user_cfg.enable_dlq is False
        assert policy is None
        # Explicit cfg caller is not settings_derived
        assert settings_derived is False


# =============================================================================
# Metric emission — ProtectMetricRecorder is called with expected labels
# =============================================================================


class TestMetricsInteractionBehavior:
    """Dependency interaction check — ``protect()`` must call
    ``ProtectMetricRecorder.record()`` with outcome-matching labels."""

    def test_success_path_records_success_outcome(self):
        """Success → recorder.record(outcome="success", fallback_used=False)."""
        mock_recorder = MagicMock()

        with patch(
            "baldur.metrics.recorders.protect.get_protect_recorder",
            return_value=mock_recorder,
        ):
            protect(name="metric.ok", fn=lambda: 1)

        mock_recorder.record.assert_called_once()
        kwargs = mock_recorder.record.call_args.kwargs
        assert kwargs["name"] == "metric.ok"
        assert kwargs["outcome"] == "success"
        assert kwargs["fallback_used"] is False
        assert kwargs["attempts"] >= 1

    def test_fallback_path_records_fallback_outcome(self):
        """Fallback branch → outcome="fallback", fallback_used=True."""
        mock_recorder = MagicMock()

        with patch(
            "baldur.metrics.recorders.protect.get_protect_recorder",
            return_value=mock_recorder,
        ):
            protect(
                name="metric.fb",
                fn=lambda: (_ for _ in ()).throw(RuntimeError("x")),
                fallback=lambda: "fb",
            )

        kwargs = mock_recorder.record.call_args.kwargs
        assert kwargs["outcome"] == "fallback"
        assert kwargs["fallback_used"] is True


# =============================================================================
# Outcome label contract — PolicyOutcome → Prometheus label string
# =============================================================================


class TestOutcomeLabelContract:
    """Contract — _outcome_label maps PolicyOutcome to exactly these label strings."""

    def test_outcome_label_exact_mapping(self):
        """All five PolicyOutcome values map to the documented label strings."""
        assert _outcome_label(PolicyOutcome.SUCCESS) == "success"
        assert _outcome_label(PolicyOutcome.SUCCESS_WITH_FALLBACK) == "fallback"
        assert _outcome_label(PolicyOutcome.REJECTED) == "rejected"
        assert _outcome_label(PolicyOutcome.TIMEOUT) == "timeout"
        assert _outcome_label(PolicyOutcome.FAILURE) == "failure"


# =============================================================================
# Decorators — @protected and @aprotected
# =============================================================================


class TestProtectedDecoratorBehavior:
    """Behavior for ``@protected`` and ``@aprotected`` decorator forms."""

    def test_protected_wraps_sync_function_transparently(self):
        """A sync-decorated function returns the original return value."""

        @protected(name="dec.sync")
        def add(a: int, b: int) -> int:
            return a + b

        assert add(2, 3) == 5

    def test_protected_auto_detects_and_awaits_coroutine(self):
        """@protected over an async def produces an awaitable that runs aprotect()."""

        @protected(name="dec.async")
        async def work():
            return "async-ok"

        result = asyncio.run(work())
        assert result == "async-ok"

    def test_protected_applies_fallback_on_failure(self):
        """Decorator forwards fallback kwarg to protect()."""

        @protected(name="dec.fb", fallback=lambda: "fb")
        def bad():
            raise RuntimeError("x")

        assert bad() == "fb"

    def test_aprotected_rejects_sync_function_with_type_error(self):
        """Contract: @aprotected applied to a sync function raises TypeError
        at decoration time."""
        with pytest.raises(TypeError, match="coroutine function"):

            @aprotected(name="dec.bad")
            def sync_fn():  # type: ignore[unused-variable]
                return 1

    def test_aprotected_wraps_async_function(self):
        """Happy path: @aprotected over a coroutine function runs under aprotect."""

        @aprotected(name="dec.aok")
        async def work():
            return 42

        assert asyncio.run(work()) == 42


# =============================================================================
# Async API — aprotect / aprotect_with_meta
# =============================================================================


class TestAprotectBehavior:
    """Behavior for the async entry points."""

    def test_aprotect_returns_awaited_fn_value(self):
        """aprotect(fn) awaits fn and returns its value."""

        async def work():
            return "async-value"

        result = asyncio.run(aprotect(name="async.ok", fn=work))
        assert result == "async-value"

    def test_aprotect_uses_fallback_when_fn_raises(self):
        """Async fallback fires when primary coroutine raises."""

        async def bad():
            raise RuntimeError("async-boom")

        async def fb():
            return "async-fb"

        result = asyncio.run(aprotect(name="async.fb", fn=bad, fallback=fb))
        assert result == "async-fb"

    def test_aprotect_with_meta_returns_protect_result(self):
        """aprotect_with_meta returns a ProtectResult with async-path metadata."""

        async def work():
            return 99

        meta = asyncio.run(aprotect_with_meta(name="async.meta", fn=work))

        assert isinstance(meta, ProtectResult)
        assert meta.success is True
        assert meta.value == 99


# =============================================================================
# Async unsupported kwargs — fail loud on yet-unimplemented policies
# =============================================================================


class TestAprotectFirstPartyPoliciesApplyContract:
    """Contract — ``aprotect`` now WIRES first-party async CB + retry (670);
    only a pre-built tenacity-bridged ``ResiliencePolicy`` passed as ``retry=``
    still raises ``NotImplementedError`` (a documented non-goal).

    This inverts the former seam that pinned CB / first-party retry as
    ``NotImplementedError``.
    """

    def test_aprotect_circuit_breaker_true_applies(self):
        """circuit_breaker=True now applies AsyncCircuitBreakerPolicy — no raise."""

        async def work():
            return 1

        # Would have raised NotImplementedError before 670; now succeeds.
        result = asyncio.run(aprotect(name="async.cb", fn=work, circuit_breaker=True))
        assert result == 1

    def test_aprotect_retry_true_applies(self):
        """retry=True now applies AsyncRetryPolicy — no raise."""

        async def work():
            return 1

        result = asyncio.run(aprotect(name="async.retry", fn=work, retry=True))
        assert result == 1

    def test_aprotect_retry_config_applies(self):
        """A RetryPolicyConfig instance drives the async retry stage — no raise."""

        async def work():
            return 1

        cfg = RetryPolicyConfig(max_attempts=2, domain="async.cfg")
        result = asyncio.run(aprotect(name="async.cfg", fn=work, retry=cfg))
        assert result == 1

    def test_aprotect_with_meta_cb_true_applies(self):
        """CB=True applies through the _with_meta variant too — returns a DTO."""

        async def work():
            return 1

        meta = asyncio.run(
            aprotect_with_meta(name="async.meta_cb", fn=work, circuit_breaker=True)
        )
        assert meta.success is True
        assert meta.value == 1

    def test_aprotect_tenacity_bridge_retry_raises_not_implemented(self):
        """The surviving guard branch — a pre-built ResiliencePolicy passed as
        retry= (async tenacity-bridge) is a documented non-goal."""

        async def work():
            return 1

        class _DummyBridgePolicy:
            @property
            def name(self) -> str:
                return "dummy_bridge"

            def execute(self, func, *args, context=None, **kwargs):
                raise AssertionError("bridge policy must not run")

        with pytest.raises(NotImplementedError, match="AsyncTenacityBridgePolicy"):
            asyncio.run(
                aprotect(name="async.bridge", fn=work, retry=_DummyBridgePolicy())
            )


class TestAprotectAsyncDefaultsBehavior:
    """Behavior — ``None`` defaults on the async path now resolve identically to
    sync (670 D4): ``circuit_breaker=None`` → ``default_circuit_breaker`` (True),
    ``retry=None`` → ``default_retry`` (False). ``@aprotected("svc")`` therefore
    gets the circuit breaker by default, matching ``@protected("svc")``."""

    def test_aprotect_cb_false_explicit_omits_breaker(self):
        """Behavior: explicit circuit_breaker=False still runs fn with no CB."""

        async def work():
            return "ok"

        result = asyncio.run(
            aprotect(name="async.cb_off", fn=work, circuit_breaker=False)
        )
        assert result == "ok"

    def test_aprotect_cb_default_parity_on(self):
        """Behavior: with CB left at default None, the async composer resolves it
        to ``default_circuit_breaker`` (True) and includes the circuit breaker —
        parity with the sync path (no silent drop)."""
        dlq_flag, cb_flag = _resolve_flags(None, None)
        assert cb_flag is True  # sync-parity default

        composer = _build_async_composer(
            name="async.cb_default",
            fallback=None,
            dlq=False,
            retry_cfg=None,
            circuit_breaker=cb_flag,
            timeout_seconds=None,
        )
        policy_names = [p.name for p in composer._policies]
        assert "circuit_breaker" in policy_names

    def test_aprotect_none_defaults_stay_usable(self):
        """Behavior: the zero-kwarg async call still succeeds (CB closed lets it
        through) even with CB now on by default."""

        async def work():
            return "ok"

        result = asyncio.run(aprotect(name="async.defaults", fn=work))
        assert result == "ok"


# =============================================================================
# Behavior — structlog auto-configure (D9)
# =============================================================================


class TestProtectStructlogAutoConfigBehavior:
    """D9: configure_structlog() auto-call from public entry points."""

    def test_protect_triggers_structlog_configure(self):
        """protect() calls configure_structlog() before processing."""
        with patch(
            "baldur.observability.structlog_config.configure_structlog"
        ) as mock_configure:
            protect(
                "test",
                lambda: 42,
                circuit_breaker=False,
                retry=False,
                dlq=False,
            )
        mock_configure.assert_called()

    def test_protect_with_meta_triggers_structlog_configure(self):
        """protect_with_meta() calls configure_structlog() before processing."""
        with patch(
            "baldur.observability.structlog_config.configure_structlog"
        ) as mock_configure:
            protect_with_meta(
                "test",
                lambda: 42,
                circuit_breaker=False,
                retry=False,
                dlq=False,
            )
        mock_configure.assert_called()

    def test_aprotect_triggers_structlog_configure(self):
        """aprotect() calls configure_structlog() before processing."""

        async def work():
            return 42

        with patch(
            "baldur.observability.structlog_config.configure_structlog"
        ) as mock_configure:
            asyncio.run(aprotect("test", work, dlq=False))
        mock_configure.assert_called()

    def test_aprotect_with_meta_triggers_structlog_configure(self):
        """aprotect_with_meta() calls configure_structlog() before processing."""

        async def work():
            return 42

        with patch(
            "baldur.observability.structlog_config.configure_structlog"
        ) as mock_configure:
            asyncio.run(aprotect_with_meta("test", work, dlq=False))
        mock_configure.assert_called()


# =============================================================================
# Behavior — _resolve_timeout sentinel (449)
# =============================================================================


class TestResolveTimeoutBehavior:
    """Three-state sentinel resolution: _TIMEOUT_UNSET → settings, explicit → value, None → disabled."""

    def test_unset_sentinel_resolves_to_settings_default(self):
        """482 D1: _TIMEOUT_UNSET → None (ProtectSettings.default_timeout_seconds
        was flipped from 30.0 to None to recover the canonical p50 < 100 μs
        bar; I/O-layer timeouts are the enforced safety net for default
        callers)."""
        from baldur.protect_facade import _TIMEOUT_UNSET, _resolve_timeout

        assert _resolve_timeout(_TIMEOUT_UNSET) is None

    def test_explicit_float_returns_as_is(self):
        """Explicit float value passes through unchanged."""
        from baldur.protect_facade import _resolve_timeout

        assert _resolve_timeout(5.0) == 5.0
        assert _resolve_timeout(0.1) == 0.1

    def test_none_disables_timeout(self):
        """Explicit None → timeout disabled (no wrapping)."""
        from baldur.protect_facade import _resolve_timeout

        assert _resolve_timeout(None) is None

    def test_unset_with_custom_settings_resolves_to_custom_value(self, monkeypatch):
        """_TIMEOUT_UNSET resolves to the env-overridden settings value."""
        from baldur.protect_facade import _TIMEOUT_UNSET, _resolve_timeout
        from baldur.settings.protect import reset_protect_settings

        monkeypatch.setenv("BALDUR_PROTECT_DEFAULT_TIMEOUT_SECONDS", "15.0")
        reset_protect_settings()

        result = _resolve_timeout(_TIMEOUT_UNSET)
        assert result == 15.0

    def test_explicit_none_wins_over_env_override(self, monkeypatch):
        """482 D3: explicit None always wins regardless of the resolved
        setting value — locks the sentinel-vs-explicit-None priority
        contract so a future caller passing ``timeout=None`` cannot be
        silently overridden by an env-supplied default."""
        from baldur.protect_facade import _resolve_timeout
        from baldur.settings.protect import reset_protect_settings

        monkeypatch.setenv("BALDUR_PROTECT_DEFAULT_TIMEOUT_SECONDS", "30")
        reset_protect_settings()

        assert _resolve_timeout(None) is None
