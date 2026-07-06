"""
SystemControlMetricRecorder Unit Tests (394 — R8).

Test targets:
    - baldur.metrics.recorders.system_control.SystemControlMetricRecorder
    - Module-level convenience functions (DD-7)
    - Facade registration in BaldurMetrics

Test Categories:
    A. Contract: __all__ exports (DD-5, DD-6)
    B. Behavior: Fail-open, convenience function delegation, facade access

Reference:
    394
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def system_control_recorder():
    from baldur.metrics.recorders.system_control import (
        SystemControlMetricRecorder,
    )

    return SystemControlMetricRecorder()


# =============================================================================
# A. Contract Tests
# =============================================================================


class TestSystemControlRecorderContract:
    """R8: SystemControlMetricRecorder contract values."""

    def test_exports_five_convenience_functions(self):
        """__all__ includes class + 5 convenience functions."""
        from baldur.metrics.recorders.system_control import __all__

        assert "SystemControlMetricRecorder" in __all__
        assert "set_sc_enabled" in __all__
        assert "set_sc_dry_run" in __all__
        assert "record_sc_state_change" in __all__
        assert "record_sc_disabled_duration" in __all__
        assert "record_sc_disabled" in __all__


# =============================================================================
# B. Behavior Tests — Recorder Methods
# =============================================================================


class TestSystemControlRecorderBehavior:
    """R8: SystemControlMetricRecorder method behavior."""

    def test_set_enabled_true(self, system_control_recorder):
        """set_enabled(True) does not raise."""
        system_control_recorder.set_enabled(True)

    def test_set_enabled_false(self, system_control_recorder):
        """set_enabled(False) does not raise."""
        system_control_recorder.set_enabled(False)

    def test_set_dry_run(self, system_control_recorder):
        """set_dry_run does not raise."""
        system_control_recorder.set_dry_run(True)

    def test_record_state_change_valid_actions(self, system_control_recorder):
        """record_state_change with each valid action does not raise."""
        for action in (
            "enable",
            "disable",
            "enable_dry_run",
            "disable_dry_run",
            "reset",
        ):
            system_control_recorder.record_state_change(action)

    def test_record_disabled_duration(self, system_control_recorder):
        """record_disabled_duration with positive value does not raise."""
        system_control_recorder.record_disabled_duration(3600.0)

    def test_record_disabled_increments(self, system_control_recorder):
        """record_disabled does not raise."""
        system_control_recorder.record_disabled()


# =============================================================================
# C. Behavior Tests — Convenience Functions (DD-7)
# =============================================================================


class TestSystemControlConvenienceFunctionsBehavior:
    """DD-7: System control convenience functions delegate to lazy recorder."""

    def test_convenience_delegates_to_recorder(self):
        """set_sc_enabled delegates to recorder.set_enabled."""
        from baldur.metrics.recorders.system_control import set_sc_enabled

        mock_recorder = MagicMock()
        with patch(
            "baldur.metrics.recorders.system_control._lazy_recorder",
            return_value=mock_recorder,
            autospec=True,
        ):
            set_sc_enabled(True)
        mock_recorder.set_enabled.assert_called_once_with(True)


# =============================================================================
# D. Contract Tests — Facade Registration
# =============================================================================


class TestSystemControlFacadeRegistrationContract:
    """SystemControlMetricRecorder registered in BaldurMetrics facade."""

    def test_facade_has_system_control_attribute(self):
        """BaldurMetrics exposes system_control recorder."""
        from baldur.metrics.prometheus import get_metrics
        from baldur.metrics.recorders.system_control import (
            SystemControlMetricRecorder,
        )

        m = get_metrics()
        assert isinstance(m.system_control, SystemControlMetricRecorder)
