"""CanaryStuckProbe unit tests (638 D5).

The canary semantic-stuck probe bridges
``RolloutWatchdog.detect_stalled_rollouts()`` — a single source of truth shared
with the Celery canary watchdog — and maps "≥1 stalled rollout" to UNHEALTHY,
"0 stalled" to HEALTHY, and a watchdog resolution failure to UNKNOWN. It is
applicable only while the PRO canary service is registered (registration is
enablement, D2).

OSS-only: the probe duck-types the bridge result, so these tests fake the
watchdog / registry slot without importing baldur_pro.

Covers:
- probe() state transition (stalled → UNHEALTHY / none → HEALTHY)
- probe() fail-safe (watchdog raises → UNKNOWN)
- probe() reason / details population (boundary: reason caps the first 5)
- is_applicable() state-based (service registered/unregistered) + fail-safe
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from baldur.factory.registry import ProviderRegistry
from baldur.meta.canary_stuck_probe import CanaryStuckProbe
from baldur.meta.health_probe import HealthStatus

_WATCHDOG_FACTORY = "baldur.tasks.canary_watchdog.get_rollout_watchdog"


def _stalled(rollout_id: str, reason: str) -> SimpleNamespace:
    """A duck-typed stalled-rollout result (only .rollout_id / .reason read)."""
    return SimpleNamespace(rollout_id=rollout_id, reason=reason)


def _watchdog_returning(stalled: list[SimpleNamespace]) -> MagicMock:
    watchdog = MagicMock()
    watchdog.detect_stalled_rollouts.return_value = stalled
    return watchdog


class TestCanaryStuckProbeBehavior:
    """Behavior verification for CanaryStuckProbe.probe()."""

    def test_component_name_is_canary_rollout(self):
        """Component name must be 'canary_rollout' for escalation wiring."""
        assert CanaryStuckProbe().component_name == "canary_rollout"

    def test_probe_unhealthy_when_rollouts_stalled(self):
        """≥1 stalled rollout → UNHEALTHY with stalled_count + rollout_ids."""
        # Given the bridge reports two stalled rollouts
        watchdog = _watchdog_returning(
            [_stalled("r1", "Stuck in CANARY"), _stalled("r2", "Stuck in PROMOTING")]
        )

        # When the probe runs
        with patch(_WATCHDOG_FACTORY, return_value=watchdog):
            result = CanaryStuckProbe().probe()

        # Then it reports UNHEALTHY with the concrete debugging context
        assert result.status == HealthStatus.UNHEALTHY
        assert result.details["stalled_count"] == 2
        assert result.details["rollout_ids"] == ["r1", "r2"]

    def test_probe_healthy_when_no_stalled_rollouts(self):
        """Empty stalled set → HEALTHY (boundary: 0 rollouts)."""
        watchdog = _watchdog_returning([])

        with patch(_WATCHDOG_FACTORY, return_value=watchdog):
            result = CanaryStuckProbe().probe()

        assert result.status == HealthStatus.HEALTHY

    def test_probe_reason_lists_stalled_reasons(self):
        """The reason string surfaces the per-rollout stall reasons."""
        watchdog = _watchdog_returning([_stalled("r1", "Stuck in CANARY for 99 min")])

        with patch(_WATCHDOG_FACTORY, return_value=watchdog):
            result = CanaryStuckProbe().probe()

        assert "1 canary rollout(s) stalled" in result.reason
        assert "Stuck in CANARY for 99 min" in result.reason

    def test_probe_reason_caps_reasons_at_five(self):
        """The reason joins at most the first 5 stall reasons (boundary)."""
        watchdog = _watchdog_returning(
            [_stalled(f"r{i}", f"reason-{i}") for i in range(7)]
        )

        with patch(_WATCHDOG_FACTORY, return_value=watchdog):
            result = CanaryStuckProbe().probe()

        # All 7 are counted, but only the first 5 reasons are joined into reason.
        assert result.details["stalled_count"] == 7
        assert "reason-4" in result.reason
        assert "reason-5" not in result.reason

    def test_probe_unknown_when_watchdog_raises(self):
        """A bridge/resolution failure → UNKNOWN, not a false HEALTHY/UNHEALTHY."""
        watchdog = MagicMock()
        watchdog.detect_stalled_rollouts.side_effect = RuntimeError("boom")

        with patch(_WATCHDOG_FACTORY, return_value=watchdog):
            result = CanaryStuckProbe().probe()

        assert result.status == HealthStatus.UNKNOWN
        assert "boom" in result.error


class TestCanaryStuckProbeApplicabilityBehavior:
    """Behavior verification for CanaryStuckProbe.is_applicable()."""

    def test_applicable_when_service_registered(self):
        """A registered PRO canary service → applicable."""
        with patch.object(
            ProviderRegistry.canary_rollout_service, "safe_get", return_value=object()
        ):
            assert CanaryStuckProbe().is_applicable() is True

    def test_not_applicable_when_service_unregistered(self):
        """safe_get() → None (OSS-only / unregistered) → skipped."""
        with patch.object(
            ProviderRegistry.canary_rollout_service, "safe_get", return_value=None
        ):
            assert CanaryStuckProbe().is_applicable() is False

    def test_not_applicable_on_resolution_error(self):
        """A registry resolution error is swallowed → not applicable (fail-safe)."""
        with patch.object(
            ProviderRegistry.canary_rollout_service,
            "safe_get",
            side_effect=RuntimeError("registry down"),
        ):
            assert CanaryStuckProbe().is_applicable() is False
