"""ThrottleStuckProbe unit tests (638 D7).

The throttle semantic-stuck probe feeds ``current_limit`` into the shared
``StuckDetector`` and reports UNHEALTHY when the limit is frozen (near-zero
variance) while constrained. ``constrained`` is demand-gated: a throttle resting
at its floor (``current_limit <= min_limit``) is flagged ONLY when rejections are
rising since the previous tick — so an idle / low-traffic throttle parked at the
floor is not a false positive. ``full_stop`` / ``emergency`` stay ungated.

OSS-only: the probe duck-types ``get_stats()`` (a plain dict), so these tests
fake the throttle without importing baldur_pro.

Shared-singleton isolation: the probe records into the module-singleton
``StuckDetector``; every test resets it (setup + teardown) to avoid cross-test
sample bleed. The probe instance persists ``_prev_rejected_requests`` across
ticks, so each test reuses ONE probe instance and calls ``probe()`` N times.

Covers:
- variance path (≥5 constrained frozen samples → UNHEALTHY)
- adapting limit (variance > 0 → HEALTHY even while constrained)
- demand-gate (at-floor + flat rejections → HEALTHY; rising rejections → UNHEALTHY)
- cold-start (< 5 samples → HEALTHY)
- prev-rejected delta tracking across ticks
- fail-safe (get_stats raises → UNKNOWN)
- is_applicable() state-based + fail-safe
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from baldur.factory.registry import ProviderRegistry
from baldur.meta.health_probe import HealthStatus
from baldur.meta.stuck_detector import reset_stuck_detector
from baldur.meta.throttle_stuck_probe import ThrottleStuckProbe


def _stats(
    *,
    current_limit: float,
    min_limit: float = 5,
    rejected_requests: int = 0,
    full_stop: bool = False,
    emergency_active: bool = False,
    dampening: bool = False,
) -> dict:
    """A duck-typed adaptive-throttle get_stats() result."""
    return {
        "current_limit": current_limit,
        "min_limit": min_limit,
        "rejected_requests": rejected_requests,
        "total_requests": 1000,
        "active_keys": 1,
        "emergency": {"full_stop_active": full_stop, "active": emergency_active},
        "recovery": {"dampening_active": dampening},
    }


def _throttle_with(stats_sequence: list[dict]) -> MagicMock:
    """A fake throttle returning the given stats dict on successive ticks."""
    throttle = MagicMock()
    throttle.get_stats.side_effect = stats_sequence
    return throttle


def _run_ticks(probe: ThrottleStuckProbe, stats_sequence: list[dict]):
    """Drive probe() once per stats dict; return the final ProbeResult."""
    throttle = _throttle_with(stats_sequence)
    result = None
    with patch.object(
        ProviderRegistry.adaptive_throttle, "safe_get", return_value=throttle
    ):
        for _ in stats_sequence:
            result = probe.probe()
    return result


@pytest.fixture(autouse=True)
def _isolate_stuck_detector():
    """Reset the shared StuckDetector around each test (sample-bleed guard)."""
    reset_stuck_detector()
    yield
    reset_stuck_detector()


class TestThrottleStuckProbeBehavior:
    """Behavior verification for ThrottleStuckProbe.probe()."""

    def test_component_name_is_adaptive_throttle(self):
        """Component name must be 'adaptive_throttle' (reads the throttle owner,
        not ThrottleAuditWorker)."""
        assert ThrottleStuckProbe().component_name == "adaptive_throttle"

    def test_unhealthy_when_full_stop_frozen(self):
        """full_stop held at a frozen limit across the window → UNHEALTHY.

        full_stop is ungated, so every sample is an error; a constant limit gives
        zero variance; ≥5 samples crosses the min-sample gate → stuck.
        """
        probe = ThrottleStuckProbe()
        seq = [_stats(current_limit=10, full_stop=True) for _ in range(5)]

        result = _run_ticks(probe, seq)

        assert result.status == HealthStatus.UNHEALTHY
        assert result.details["constrained"] is True
        assert result.details["variance"] < 0.001

    def test_unhealthy_when_emergency_active_frozen(self):
        """emergency.active held at a frozen limit → UNHEALTHY (ungated path)."""
        probe = ThrottleStuckProbe()
        seq = [_stats(current_limit=8, emergency_active=True) for _ in range(5)]

        result = _run_ticks(probe, seq)

        assert result.status == HealthStatus.UNHEALTHY

    def test_healthy_when_limit_adapting(self):
        """A moving limit (variance > 0) is actively managing → HEALTHY.

        Even with full_stop set on every tick, a varying current_limit means the
        throttle is NOT frozen — the zero-variance discriminator keeps it healthy.
        """
        probe = ThrottleStuckProbe()
        limits = [10, 20, 12, 25, 14, 30]
        seq = [_stats(current_limit=v, full_stop=True) for v in limits]

        result = _run_ticks(probe, seq)

        assert result.status == HealthStatus.HEALTHY
        assert result.details["variance"] > 0.001

    def test_healthy_at_floor_with_flat_rejections(self):
        """At-floor with NO rising rejections (idle / low-traffic) → HEALTHY.

        This is the demand-gate's reason to exist: current_limit == min_limit at
        zero variance would otherwise trip a false UNHEALTHY once ≥5 samples
        accumulate. With flat rejections, no sample is marked error → not stuck.
        """
        probe = ThrottleStuckProbe()
        seq = [
            _stats(current_limit=5, min_limit=5, rejected_requests=100)
            for _ in range(6)
        ]

        result = _run_ticks(probe, seq)

        assert result.status == HealthStatus.HEALTHY
        assert result.details["constrained"] is False
        assert result.details["error_rate"] == 0.0

    def test_unhealthy_at_floor_with_rising_rejections(self):
        """At-floor with rising rejections (real demand denied) → UNHEALTHY.

        The genuinely harmful wedge: the limit is pinned at the floor while real
        traffic is being rejected. The demand-gate fires the at-floor error term.
        """
        probe = ThrottleStuckProbe()
        seq = [
            _stats(current_limit=5, min_limit=5, rejected_requests=r)
            for r in (10, 20, 30, 40, 50, 60)
        ]

        result = _run_ticks(probe, seq)

        # First sample has no prev → not error; samples 2-6 are rising → error.
        # 5/6 error_rate > 0.5 and zero variance → stuck.
        assert result.status == HealthStatus.UNHEALTHY
        assert result.details["constrained"] is True

    def test_healthy_after_incident_recovery_idle_at_floor(self):
        """Recovery tail: after a genuine full_stop incident clears, an idle
        throttle resting at the floor with flat rejections returns HEALTHY at
        once — not a multi-tick false UNHEALTHY.

        Regression for the shared-StuckDetector window-flush tail: the 20-sample
        window still holds the incident's error samples, and the frozen limit
        keeps variance ~0, so ``detector.check(...).is_stuck`` stays True for
        several ticks after recovery. Gating UNHEALTHY on the CURRENT tick still
        being constrained (D7 — "only a frozen limit that is STILL constrained
        trips") keeps the recovered / idle-at-floor throttle HEALTHY. Rejections
        are held flat across the incident→idle boundary so the demand-gate sees
        no rising demand and the idle ticks are genuinely unconstrained.
        """
        probe = ThrottleStuckProbe()
        # 10 frozen full_stop ticks (a genuine incident: constrained, value 5) ...
        incident = [
            _stats(current_limit=5, full_stop=True, rejected_requests=200)
            for _ in range(10)
        ]
        # ... then full_stop clears; throttle idles at the floor, rejections flat.
        idle = [
            _stats(current_limit=5, min_limit=5, rejected_requests=200)
            for _ in range(6)
        ]

        result = _run_ticks(probe, incident + idle)

        # The detector still reports a frozen, historically-erroring window ...
        assert result.details["variance"] < 0.001
        assert result.details["error_rate"] > 0.5
        # ... but the current tick is no longer constrained → HEALTHY (old code
        # reported UNHEALTHY here off detector history alone).
        assert result.details["constrained"] is False
        assert result.status == HealthStatus.HEALTHY

    def test_healthy_before_min_samples(self):
        """Fewer than 5 samples never trips (cold-start window, D7 risk)."""
        probe = ThrottleStuckProbe()
        seq = [_stats(current_limit=10, full_stop=True) for _ in range(4)]

        result = _run_ticks(probe, seq)

        assert result.status == HealthStatus.HEALTHY
        assert result.details["sample_count"] == 4

    def test_first_tick_variance_reports_null_not_infinity(self):
        """One sample -> the detector's variance sentinel is float('inf'); the
        details must stay JSON-representable (the admin writer rejects
        non-finite floats and would 500 the whole /meta-watchdog/status
        payload), so the probe reports None instead."""
        probe = ThrottleStuckProbe()

        result = _run_ticks(probe, [_stats(current_limit=10)])

        assert result.details["variance"] is None
        json.dumps(result.details, allow_nan=False)  # must not raise

    def test_prev_rejected_tracked_across_ticks(self):
        """The probe records the previous tick's rejected_requests for the delta."""
        probe = ThrottleStuckProbe()
        assert probe._prev_rejected_requests is None

        _run_ticks(probe, [_stats(current_limit=5, rejected_requests=42)])

        assert probe._prev_rejected_requests == 42

    def test_unknown_when_get_stats_raises(self):
        """A throttle read failure → UNKNOWN, not a false verdict."""
        probe = ThrottleStuckProbe()
        throttle = MagicMock()
        throttle.get_stats.side_effect = RuntimeError("boom")

        with patch.object(
            ProviderRegistry.adaptive_throttle, "safe_get", return_value=throttle
        ):
            result = probe.probe()

        assert result.status == HealthStatus.UNKNOWN
        assert "boom" in result.error

    def test_unknown_when_throttle_unregistered_mid_probe(self):
        """safe_get() → None mid-probe → UNKNOWN (defensive guard)."""
        probe = ThrottleStuckProbe()
        with patch.object(
            ProviderRegistry.adaptive_throttle, "safe_get", return_value=None
        ):
            result = probe.probe()

        assert result.status == HealthStatus.UNKNOWN
        assert result.error is not None


class TestThrottleStuckProbeApplicabilityBehavior:
    """Behavior verification for ThrottleStuckProbe.is_applicable()."""

    def test_applicable_when_throttle_registered(self):
        with patch.object(
            ProviderRegistry.adaptive_throttle, "safe_get", return_value=object()
        ):
            assert ThrottleStuckProbe().is_applicable() is True

    def test_not_applicable_when_throttle_unregistered(self):
        with patch.object(
            ProviderRegistry.adaptive_throttle, "safe_get", return_value=None
        ):
            assert ThrottleStuckProbe().is_applicable() is False

    def test_not_applicable_on_resolution_error(self):
        with patch.object(
            ProviderRegistry.adaptive_throttle,
            "safe_get",
            side_effect=RuntimeError("registry down"),
        ):
            assert ThrottleStuckProbe().is_applicable() is False
