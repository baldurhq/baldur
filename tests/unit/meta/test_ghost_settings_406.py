"""
#406 Ghost Settings — Meta-Watchdog wiring tests.

Tests for:
- escalation_delay_seconds timestamp dict pattern (Decision 2)
- Pending escalation cleared on healthy detection
- StuckDetector time-based stuck detection
- HealthProbeManager.probe_all() timeout enforcement
"""

from __future__ import annotations

import pytest

pytest.importorskip("baldur_pro")

pytestmark = pytest.mark.requires_pro


import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from baldur.core.exceptions import StepTimeoutError
from baldur.meta.health_probe import (
    HealthProbeManager,
    HealthStatus,
    ProbeResult,
)
from baldur.meta.stuck_detector import StuckDetector
from baldur.utils.time import utc_now
from baldur_pro.services.meta_watchdog import SelfHealerWatchdog


def _make_probe(component: str, status=HealthStatus.UNHEALTHY) -> ProbeResult:
    return ProbeResult(
        component=component,
        status=status,
        latency_ms=1.0,
        timestamp=utc_now(),
    )


def _make_watchdog_settings():
    """Create a mock MetaWatchdogSettings."""
    settings = MagicMock()
    settings.enabled = True
    settings.probe_interval_seconds = 5.0
    settings.probe_timeout_seconds = 10.0
    settings.self_cb_enabled = False
    settings.self_cb_failure_threshold = 5
    settings.self_cb_recovery_timeout_seconds = 60.0
    settings.escalation_enabled = True
    settings.escalation_delay_seconds = 180.0
    settings.escalation_cooldown_seconds = 3600.0
    settings.recovery_cooldown_seconds = 300.0
    settings.dry_run_mode = False
    settings.maintenance_components = []
    settings.recovery_total_timeout_seconds = 60.0
    settings.max_items_per_recovery = 5
    settings.k8s_api_timeout_seconds = 30.0
    settings.stuck_threshold_seconds = 300.0
    settings.escalation_api_timeout_seconds = 10.0
    return settings


# =============================================================================
# Behavior: escalation delay (timestamp dict pattern)
# =============================================================================


class TestEscalationDelayBehavior:
    """escalation_delay_seconds defers escalation until time threshold met."""

    @pytest.fixture
    def watchdog(self):
        with patch(
            "baldur_pro.services.meta_watchdog.get_meta_watchdog_settings",
            return_value=_make_watchdog_settings(),
        ):
            return SelfHealerWatchdog()

    def test_first_failure_does_not_escalate(self, watchdog):
        """First recovery failure records pending but does not escalate."""
        probe = _make_probe("redis")

        with (
            patch.object(watchdog, "_attempt_recovery", return_value=False),
            patch.object(watchdog, "_escalate") as mock_escalate,
            patch.object(watchdog, "_is_in_cooldown", return_value=False),
        ):
            result = watchdog._attempt_guarded_recovery("redis", probe, 60.0)

        assert result is False
        mock_escalate.assert_not_called()
        assert "redis" in watchdog._pending_escalations

    def test_escalation_fires_after_delay_exceeded(self, watchdog):
        """Escalation triggers when pending duration exceeds delay."""
        probe = _make_probe("redis")

        # Pre-populate past the delay threshold
        watchdog._pending_escalations["redis"] = (
            time.time() - watchdog._settings.escalation_delay_seconds - 1
        )

        with (
            patch.object(watchdog, "_attempt_recovery", return_value=False),
            patch.object(watchdog, "_escalate") as mock_escalate,
            patch.object(watchdog, "_is_in_cooldown", return_value=False),
        ):
            result = watchdog._attempt_guarded_recovery("redis", probe, 60.0)

        assert result is True
        mock_escalate.assert_called_once_with("redis", probe, recovery_attempted=True)
        assert "redis" not in watchdog._pending_escalations

    def test_successful_recovery_clears_pending_escalation(self, watchdog):
        """Successful recovery removes pending escalation entry."""
        probe = _make_probe("redis")
        watchdog._pending_escalations["redis"] = time.time()

        with (
            patch.object(watchdog, "_attempt_recovery", return_value=True),
            patch.object(watchdog, "_is_in_cooldown", return_value=False),
        ):
            watchdog._attempt_guarded_recovery("redis", probe, 60.0)

        assert "redis" not in watchdog._pending_escalations


# =============================================================================
# Behavior: pending escalation cleared on healthy detection
# =============================================================================


class TestPendingClearedOnHealthyBehavior:
    """check_health() clears pending escalation when component becomes healthy."""

    @pytest.fixture
    def watchdog(self):
        with patch(
            "baldur_pro.services.meta_watchdog.get_meta_watchdog_settings",
            return_value=_make_watchdog_settings(),
        ):
            return SelfHealerWatchdog()

    def test_healthy_probe_clears_pending_escalation(self, watchdog):
        """Healthy probe result removes component from _pending_escalations."""
        # Given
        watchdog._pending_escalations["redis"] = time.time()
        healthy_result = _make_probe("redis", HealthStatus.HEALTHY)

        # When
        with (
            patch.object(
                watchdog._probe_manager,
                "probe_all",
                return_value={"redis": healthy_result},
            ),
            patch.object(
                watchdog._probe_manager,
                "get_overall_status",
                return_value=HealthStatus.HEALTHY,
            ),
        ):
            watchdog.check_health()

        # Then
        assert "redis" not in watchdog._pending_escalations


# =============================================================================
# Behavior: StuckDetector time-based detection
# =============================================================================


class TestStuckDetectorTimeBasedBehavior:
    """stuck_threshold_seconds adds time-based stuck detection."""

    def test_time_based_stuck_when_duration_exceeds_threshold(self):
        """Component stuck when duration >= threshold and error rate high."""
        detector = StuckDetector(
            window_size=20,
            variance_threshold=0.001,
            error_rate_threshold=0.5,
        )

        # Record samples with high error rate but NON-zero variance
        for i in range(10):
            detector.record("comp", value=float(100 + (i % 3)), error=True)

        with patch(
            "baldur.meta.config.get_meta_watchdog_settings",
            return_value=MagicMock(stuck_threshold_seconds=10.0),
        ):
            # Simulate: first sample was 15s ago
            detector._first_sample_time["comp"] = time.time() - 15.0
            result = detector.check("comp")

        assert result.is_stuck is True

    def test_time_based_not_stuck_when_below_threshold(self):
        """Component not stuck when duration < threshold."""
        detector = StuckDetector(
            window_size=20,
            variance_threshold=0.001,
            error_rate_threshold=0.5,
        )

        for i in range(10):
            detector.record("comp", value=float(100 + (i % 3)), error=True)

        with patch(
            "baldur.meta.config.get_meta_watchdog_settings",
            return_value=MagicMock(stuck_threshold_seconds=300.0),
        ):
            # first sample was only 2s ago
            detector._first_sample_time["comp"] = time.time() - 2.0
            result = detector.check("comp")

        # variance is non-zero and duration is below threshold
        assert result.is_stuck is False


# =============================================================================
# Behavior: HealthProbeManager.probe_all() timeout
# =============================================================================


class TestProbeAllTimeoutBehavior:
    """probe_all() wraps each probe with TimeoutExecutor."""

    def test_probe_timeout_produces_unknown_status(self):
        """Probe that times out results in UNKNOWN status."""
        mock_probe = MagicMock()
        mock_probe.component_name = "slow_probe"

        settings = MagicMock()
        settings.probe_timeout_seconds = 1.0

        manager = HealthProbeManager.__new__(HealthProbeManager)
        manager._probes = [mock_probe]
        manager._settings = settings
        manager._lock = threading.RLock()
        manager._last_results = {}

        with patch("baldur.core.timeout_executor.TimeoutExecutor") as mock_executor_cls:
            mock_executor = MagicMock()
            mock_executor.execute.side_effect = StepTimeoutError("timeout")
            mock_executor_cls.return_value = mock_executor

            results = manager.probe_all()

        assert "slow_probe" in results
        assert results["slow_probe"].status == HealthStatus.UNKNOWN
        assert "timeout" in results["slow_probe"].error
