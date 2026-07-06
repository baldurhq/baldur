"""Mock-based integration tests for the tenacity bridge (impl 451).

Scope (from impl 451 ``Test Assessment`` integration scenarios):
- ``protect(retry=TenacityBridgePolicy(...))`` end-to-end success / exhausted.
- Bridge inside ``standard_pipeline(retry_policy=...)`` — composer chain.
- ``instrument_tenacity()`` Level-1 patch emits ``RETRY_EXHAUSTED`` events.
- D5↔D7 interplay: bootstrap installs Level-1 + user instantiates Level-3 →
  exactly one RETRY_EXHAUSTED emitted per exhausted call.

No Docker / Redis needed — EventBus runs locally; AdaptiveRetryBudget and
RateLimitCoordinator are constructor-injected as plain instances.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
import tenacity

from baldur.bridges.tenacity import instrument_tenacity, is_instrumented
from baldur.bridges.tenacity.instrument import _reset_instrument_for_testing
from baldur.bridges.tenacity.policy import TenacityBridgePolicy
from baldur.protect_facade import protect, protect_with_meta
from baldur.resilience.policies.presets import standard_pipeline
from baldur.settings.bridge import reset_bridge_settings
from baldur.settings.protect import reset_protect_settings


@pytest.fixture(autouse=True)
def _isolated_state():
    reset_protect_settings()
    reset_bridge_settings()
    _reset_instrument_for_testing()
    yield
    _reset_instrument_for_testing()
    reset_bridge_settings()
    reset_protect_settings()


@pytest.fixture
def captured_events(monkeypatch):
    """Replace the EventBus with a list-capturing stub."""
    captured: list[dict] = []

    class _StubBus:
        def emit(self, *, event_type, data, source):
            captured.append({"event_type": event_type, "data": data, "source": source})

    monkeypatch.setattr("baldur.services.event_bus.get_event_bus", lambda: _StubBus())
    return captured


# =============================================================================
# protect() + TenacityBridgePolicy end-to-end
# =============================================================================


class TestProtectWithBridgeIntegration:
    """``protect(retry=<TenacityBridgePolicy>)`` exercises the full pipeline."""

    def test_success_after_one_retry_returns_value(self):
        counter = {"calls": 0}

        def _fn():
            counter["calls"] += 1
            if counter["calls"] < 2:
                raise OSError("transient")
            return "ok"

        bridge: TenacityBridgePolicy[str] = TenacityBridgePolicy(
            stop=tenacity.stop_after_attempt(3),
            wait=tenacity.wait_fixed(0),
        )

        result = protect("svc-int", _fn, retry=bridge, circuit_breaker=False)

        assert result == "ok"
        assert counter["calls"] == 2

    def test_exhausted_returns_failure_in_protect_with_meta(self):
        counter = {"calls": 0}

        def _always_fail():
            counter["calls"] += 1
            raise RuntimeError("never recovers")

        bridge: TenacityBridgePolicy[None] = TenacityBridgePolicy(
            stop=tenacity.stop_after_attempt(3),
            wait=tenacity.wait_fixed(0),
        )

        meta = protect_with_meta(
            "svc-int-fail",
            _always_fail,
            retry=bridge,
            circuit_breaker=False,
        )

        # The composer's outer PolicyResult does not propagate the inner
        # bridge's ``total_attempts`` (each policy reports independently),
        # so we verify exhaustion by observing the wrapped fn call count
        # alongside the failure outcome.
        assert meta.success is False
        assert isinstance(meta.error, RuntimeError)
        assert counter["calls"] == 3


# =============================================================================
# standard_pipeline composition
# =============================================================================


class TestStandardPipelineWithBridgeIntegration:
    """Bridge slots into ``standard_pipeline(retry_policy=...)`` correctly."""

    def test_composer_runs_bridge_then_cb(self):
        """Pipeline executes the bridge in retry slot; CB still wraps it."""
        counter = {"calls": 0}

        def _fn():
            counter["calls"] += 1
            if counter["calls"] < 3:
                raise ValueError("transient")
            return "recovered"

        bridge: TenacityBridgePolicy[str] = TenacityBridgePolicy(
            stop=tenacity.stop_after_attempt(5),
            wait=tenacity.wait_fixed(0),
        )

        pipeline = standard_pipeline("svc-pipeline", retry_policy=bridge)
        result = pipeline.execute(_fn)

        assert result.success is True
        assert result.value == "recovered"
        assert counter["calls"] == 3


# =============================================================================
# Level-1 instrument event emission
# =============================================================================


class TestInstrumentTenacityIntegration:
    """``instrument_tenacity()`` makes vanilla ``tenacity.Retrying`` emit events."""

    def test_user_retrying_after_instrument_emits_retry_exhausted(
        self, captured_events
    ):
        """A user-built Retrying (no Baldur policy) emits RETRY_EXHAUSTED on exhaustion."""
        instrument_tenacity()
        assert is_instrumented() is True

        def _always_fail():
            raise RuntimeError("nope")

        retrying = tenacity.Retrying(
            stop=tenacity.stop_after_attempt(2),
            wait=tenacity.wait_fixed(0),
        )

        with pytest.raises(RuntimeError):
            retrying(_always_fail)

        # Level-1 path emits one RETRY_EXHAUSTED with domain='tenacity_instrument'.
        assert len(captured_events) == 1
        assert captured_events[0]["source"] == "tenacity_bridge"
        assert captured_events[0]["data"]["domain"] == "tenacity_instrument"
        assert captured_events[0]["data"]["attempts"] == 2


# =============================================================================
# D5 ↔ D7 interplay — exactly one event when both levels are active
# =============================================================================


class TestLevelInterplayIntegration:
    """When Level-1 and Level-3 coexist, only the policy emits the event."""

    def test_explicit_policy_under_instrument_emits_once(self, captured_events):
        """Bridge-built Retrying carries the explicit marker → instrument skips chaining."""
        instrument_tenacity()

        def _always_fail():
            raise RuntimeError("nope")

        bridge: TenacityBridgePolicy[None] = TenacityBridgePolicy(
            stop=tenacity.stop_after_attempt(2),
            wait=tenacity.wait_fixed(0),
            domain="combo",
        )
        bridge.execute(_always_fail)

        # Exactly one event from the explicit policy path.
        assert len(captured_events) == 1
        assert captured_events[0]["data"]["domain"] == "combo"


# =============================================================================
# Self-DDoS guards inside the bridge
# =============================================================================


class TestBridgeSelfDDoSGuardIntegration:
    """RateLimitCoordinator + AdaptiveRetryBudget collaborators wire into the loop."""

    def test_rate_limit_coordinator_invoked_per_attempt(self):
        coord = MagicMock()
        coord.wait_if_needed.return_value = MagicMock(waited=False, wait_time=0.0)

        counter = {"calls": 0}

        def _fn():
            counter["calls"] += 1
            if counter["calls"] < 2:
                raise OSError("transient")
            return "ok"

        bridge: TenacityBridgePolicy[str] = TenacityBridgePolicy(
            stop=tenacity.stop_after_attempt(3),
            wait=tenacity.wait_fixed(0),
            rate_limit_coordinator=coord,
            rate_limit_key="payment",
        )
        protect("svc-rl", _fn, retry=bridge, circuit_breaker=False)

        # ``before`` fires once per attempt → wait_if_needed call count.
        assert coord.wait_if_needed.call_count == counter["calls"]
