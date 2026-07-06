"""Unit tests for ``baldur.protect_facade`` changes from impl 451 — tenacity bridge.

Scope:
- ``_resolve_retry_stage`` tuple-return contract for each ``retry=`` shape.
- ``protect(retry=<TenacityBridgePolicy>)`` end-to-end: bridge runs as the retry stage.
- ``_guard_async_unsupported`` allows a ``TenacityBridgePolicy`` on the async
  path (auto-converted to ``AsyncTenacityBridgePolicy``, 672 D9) and raises
  ``NotImplementedError`` only for an arbitrary non-tenacity ``ResiliencePolicy``.
"""

from __future__ import annotations

import pytest
import tenacity

from baldur.bridges.tenacity.policy import TenacityBridgePolicy
from baldur.protect_facade import (
    _guard_async_unsupported,
    _resolve_retry_stage,
    aprotect,
    protect,
)
from baldur.services.retry_handler.models import RetryPolicyConfig


@pytest.fixture(autouse=True)
def _reset_protect_settings():
    from baldur.settings.protect import reset_protect_settings

    reset_protect_settings()
    yield
    reset_protect_settings()


# =============================================================================
# Contract — _resolve_retry_stage tuple-return for each retry= shape
# =============================================================================


class TestResolveRetryStageContract:
    """``_resolve_retry_stage`` returns
    ``(retry_cfg, retry_policy, settings_derived)`` where exactly one of
    the first two slots is non-None when retry is requested. The third
    slot (added by #499 D5) is True only when ``retry_cfg`` came from the
    ``RetryPolicyConfig.from_settings(domain=name)`` path and gates the
    ``dlq_protect`` cache fast-path.
    """

    @pytest.mark.parametrize(
        "retry_arg",
        [False, None],
        ids=["false_no_retry", "none_with_default_off"],
    )
    def test_non_truthy_returns_pair_of_nones(self, retry_arg):
        """retry=False (or None with default off) → (None, None, False)."""
        # ProtectSettings.default_retry defaults to False.
        cfg, policy, settings_derived = _resolve_retry_stage(
            retry_arg, dlq_requested=False, domain="t"
        )

        assert cfg is None
        assert policy is None
        assert settings_derived is False

    def test_resilience_policy_instance_returned_in_policy_slot(self):
        """ResiliencePolicy is returned verbatim in the second tuple slot."""
        bridge: TenacityBridgePolicy[None] = TenacityBridgePolicy(
            stop=tenacity.stop_after_attempt(1)
        )

        cfg, policy, settings_derived = _resolve_retry_stage(
            bridge, dlq_requested=False, domain="t"
        )

        assert cfg is None
        assert policy is bridge
        assert settings_derived is False

    def test_retry_policy_config_returned_in_cfg_slot(self):
        """RetryPolicyConfig is returned in the first tuple slot, policy slot is None.

        Explicit-cfg callers must not be settings_derived — caching across
        name would collide with ``@dlq_protect("X")`` callers.
        """
        user_cfg = RetryPolicyConfig(max_attempts=2, domain="t")

        cfg, policy, settings_derived = _resolve_retry_stage(
            user_cfg, dlq_requested=False, domain="t"
        )

        assert cfg is user_cfg
        assert policy is None
        assert settings_derived is False

    def test_retry_true_resolves_config_from_settings(self):
        """retry=True → (RetryPolicyConfig.from_settings(domain), None, True).

        The True flag is the cache-eligibility signal for the dlq_protect
        fast-path.
        """
        cfg, policy, settings_derived = _resolve_retry_stage(
            True, dlq_requested=False, domain="svc.retry"
        )

        assert isinstance(cfg, RetryPolicyConfig)
        assert cfg.domain == "svc.retry"
        assert policy is None
        assert settings_derived is True

    def test_dlq_requested_forces_enable_dlq_on_returned_cfg(self):
        """When ``dlq_requested=True`` and the user-supplied cfg has
        ``enable_dlq=False``, the returned cfg has it flipped to True without
        mutating the original (immutability contract preserved across the
        rename). User-supplied cfg → settings_derived False.
        """
        user_cfg = RetryPolicyConfig(enable_dlq=False, domain="svc.dlq")

        cfg, policy, settings_derived = _resolve_retry_stage(
            user_cfg, dlq_requested=True, domain="svc.dlq"
        )

        assert cfg is not None
        assert cfg.enable_dlq is True
        assert user_cfg.enable_dlq is False
        assert policy is None
        assert settings_derived is False


# =============================================================================
# Behavior — protect(retry=<TenacityBridgePolicy>) end-to-end
# =============================================================================


class TestProtectRetryResiliencePolicyBehavior:
    """``protect(retry=<TenacityBridgePolicy>)`` runs the bridge as retry stage."""

    def test_retry_resilience_policy_invokes_fn_under_bridge_loop(self):
        """fn invoked N times per the bridge's tenacity stop strategy."""
        counter = {"calls": 0}

        def _fn():
            counter["calls"] += 1
            if counter["calls"] < 3:
                raise ValueError("transient")
            return "ok"

        bridge: TenacityBridgePolicy[str] = TenacityBridgePolicy(
            stop=tenacity.stop_after_attempt(5),
            wait=tenacity.wait_fixed(0),
        )

        result = protect("svc-bridge", _fn, retry=bridge, circuit_breaker=False)

        assert result == "ok"
        assert counter["calls"] == 3

    def test_retry_resilience_policy_all_failures_raises(self):
        """Exhausted bridge → underlying error re-raised by protect()."""

        def _always_fail():
            raise RuntimeError("nope")

        bridge: TenacityBridgePolicy[None] = TenacityBridgePolicy(
            stop=tenacity.stop_after_attempt(2),
            wait=tenacity.wait_fixed(0),
        )

        with pytest.raises(RuntimeError, match="nope"):
            protect(
                "svc-bridge-fail", _always_fail, retry=bridge, circuit_breaker=False
            )


# =============================================================================
# Contract — _guard_async_unsupported (672 D9: tenacity now auto-converts;
# only a non-tenacity ResiliencePolicy still fails loud)
# =============================================================================


class TestGuardAsyncUnsupportedResiliencePolicyContract:
    """After 672, a ``TenacityBridgePolicy`` retry= is supported on the async
    path (auto-converted); only an arbitrary non-tenacity ``ResiliencePolicy``
    fails loud — silent degradation forbidden."""

    def test_tenacity_bridge_does_not_raise(self):
        """A pre-built tenacity bridge is supported — auto-converted downstream."""
        bridge: TenacityBridgePolicy[None] = TenacityBridgePolicy(
            stop=tenacity.stop_after_attempt(1)
        )
        # Should not raise — the async facade auto-converts it.
        _guard_async_unsupported(bridge)

    def test_non_tenacity_resilience_policy_raises_with_sync_pointer(self):
        """An arbitrary sync ResiliencePolicy still raises, pointing to protect()."""
        from baldur.interfaces.resilience_policy import PolicyOutcome, PolicyResult

        class _DummyPolicy:
            name = "dummy"

            def execute(self, func, *a, context=None, **k):
                return PolicyResult(value=func(), outcome=PolicyOutcome.SUCCESS)

        with pytest.raises(NotImplementedError, match=r"sync protect\(\)"):
            _guard_async_unsupported(_DummyPolicy())

    def test_none_retry_does_not_raise(self):
        """retry=None / False / first-party cfg does not match the guard branch."""
        # Should not raise; pure no-op.
        _guard_async_unsupported(None)
        _guard_async_unsupported(False)
        _guard_async_unsupported(True)
        _guard_async_unsupported(RetryPolicyConfig(max_attempts=2, domain="t"))


class TestAprotectTenacityBridgeBehavior:
    """End-to-end: ``aprotect`` auto-converts a tenacity bridge and runs it."""

    @pytest.mark.asyncio
    async def test_aprotect_with_bridge_auto_converts_and_runs(self):
        """aprotect(retry=<bridge>) now runs under AsyncRetrying (672 D9)."""
        attempts = {"n": 0}

        async def _flaky():
            attempts["n"] += 1
            if attempts["n"] < 2:
                raise ConnectionError("boom")
            return "x"

        bridge: TenacityBridgePolicy[str] = TenacityBridgePolicy(
            stop=tenacity.stop_after_attempt(3),
            wait=tenacity.wait_fixed(0),
            retry=tenacity.retry_if_exception_type(ConnectionError),
        )

        result = await aprotect("svc", _flaky, retry=bridge, circuit_breaker=False)
        assert result == "x"
        assert attempts["n"] == 2

    @pytest.mark.asyncio
    async def test_aprotect_with_non_tenacity_policy_raises(self):
        """A non-tenacity ResiliencePolicy still raises NotImplementedError."""
        from baldur.interfaces.resilience_policy import PolicyOutcome, PolicyResult

        class _DummyPolicy:
            name = "dummy"

            def execute(self, func, *a, context=None, **k):
                return PolicyResult(value=func(), outcome=PolicyOutcome.SUCCESS)

        async def _fn():
            return "x"

        with pytest.raises(NotImplementedError, match=r"sync protect\(\)"):
            await aprotect("svc", _fn, retry=_DummyPolicy(), circuit_breaker=False)
