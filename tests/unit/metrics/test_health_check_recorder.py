"""
HealthCheckMetricRecorder Unit Tests (394 — R6).

Test targets:
    - baldur.metrics.recorders.health_check.HealthCheckMetricRecorder
    - Module-level convenience functions (DD-7)
    - Facade registration in BaldurMetrics

Test Categories:
    A. Contract: Status maps, __all__ exports (DD-5, DD-6)
    B. Behavior: Fail-open, convenience function delegation, facade access

Reference:
    394
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def health_check_recorder():
    from baldur.metrics.recorders.health_check import HealthCheckMetricRecorder

    return HealthCheckMetricRecorder()


# =============================================================================
# A. Contract Tests — Status Maps (DD-6)
# =============================================================================


class TestHealthCheckRecorderContract:
    """R6: HealthCheckMetricRecorder status map contract values."""

    def test_status_map_values(self):
        """STATUS_MAP: healthy=0, degraded=1, unhealthy=2, error=3."""
        from baldur.metrics.recorders.health_check import _STATUS_MAP

        assert _STATUS_MAP == {
            "healthy": 0,
            "degraded": 1,
            "unhealthy": 2,
            "error": 3,
        }

    def test_pool_status_map_values(self):
        """POOL_STATUS_MAP: healthy=0, degraded=1, error=2."""
        from baldur.metrics.recorders.health_check import _POOL_STATUS_MAP

        assert _POOL_STATUS_MAP == {"healthy": 0, "degraded": 1, "error": 2}

    def test_exports_four_convenience_functions(self):
        """__all__ includes class + 4 convenience functions."""
        from baldur.metrics.recorders.health_check import __all__

        assert "HealthCheckMetricRecorder" in __all__
        assert "record_health_check" in __all__
        assert "set_health_status" in __all__
        assert "set_database_connected" in __all__
        assert "set_pool_status" in __all__


# =============================================================================
# B. Behavior Tests — Recorder Methods
# =============================================================================


class TestHealthCheckRecorderBehavior:
    """R6: HealthCheckMetricRecorder method behavior."""

    def test_record_check_does_not_raise(self, health_check_recorder):
        """record_check with valid args does not raise (fail-open)."""
        health_check_recorder.record_check("database", "healthy", 0.05, "default")

    def test_set_status_maps_string_to_int(self, health_check_recorder):
        """set_status maps status string to int gauge value."""
        health_check_recorder.set_status("overall", "degraded")

    def test_set_database_connected_true(self, health_check_recorder):
        """set_database_connected(True) sets gauge to 1."""
        health_check_recorder.set_database_connected("default", True)

    def test_set_database_connected_false(self, health_check_recorder):
        """set_database_connected(False) sets gauge to 0."""
        health_check_recorder.set_database_connected("default", False)

    def test_set_pool_status_valid(self, health_check_recorder):
        """set_pool_status with valid status does not raise."""
        health_check_recorder.set_pool_status("replica", "healthy")

    def test_unknown_status_defaults_to_zero(self, health_check_recorder):
        """Unknown status string maps to 0 (fail-open via dict.get default)."""
        health_check_recorder.set_status("overall", "nonexistent_status")

    def test_record_check_exception_is_suppressed(self, health_check_recorder):
        """Exception in record_check is caught and logged (fail-open)."""
        with patch.object(
            health_check_recorder,
            "_duration",
            side_effect=Exception("boom"),
            autospec=True,
        ):
            health_check_recorder.record_check("database", "healthy", 0.1)


# =============================================================================
# C. Behavior Tests — Convenience Functions (DD-7)
# =============================================================================


class TestHealthCheckConvenienceFunctionsBehavior:
    """DD-7: Health check convenience functions delegate to lazy recorder."""

    def test_convenience_delegates_to_recorder(self):
        """record_health_check delegates to recorder.record_check."""
        from baldur.metrics.recorders.health_check import record_health_check

        mock_recorder = MagicMock()
        with patch(
            "baldur.metrics.recorders.health_check._lazy_recorder",
            return_value=mock_recorder,
            autospec=True,
        ):
            record_health_check("database", "healthy", 0.05, "default")
        mock_recorder.record_check.assert_called_once_with(
            "database", "healthy", 0.05, "default"
        )

    def test_convenience_noop_when_recorder_none(self):
        """Convenience functions are no-op when _lazy_recorder returns None."""
        from baldur.metrics.recorders.health_check import record_health_check

        with patch(
            "baldur.metrics.recorders.health_check._lazy_recorder",
            return_value=None,
            autospec=True,
        ):
            record_health_check("database", "healthy", 0.05)


# =============================================================================
# D. Contract Tests — Facade Registration
# =============================================================================


class TestHealthCheckFacadeRegistrationContract:
    """HealthCheckMetricRecorder registered in BaldurMetrics facade."""

    def test_facade_has_health_check_attribute(self):
        """BaldurMetrics exposes health_check recorder."""
        from baldur.metrics.prometheus import get_metrics
        from baldur.metrics.recorders.health_check import (
            HealthCheckMetricRecorder,
        )

        m = get_metrics()
        assert isinstance(m.health_check, HealthCheckMetricRecorder)
