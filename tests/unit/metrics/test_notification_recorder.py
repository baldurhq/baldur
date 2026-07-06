"""
NotificationMetricRecorder Unit Tests (408 — C5).

Test targets:
    - baldur.metrics.recorders.notification.NotificationMetricRecorder
    - Module-level convenience functions (DD-7)
    - Facade registration in BaldurMetrics

Test Categories:
    A. Contract: __all__ exports
    B. Behavior: Recorder methods, convenience delegation

Reference:
    408
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def notification_recorder():
    from baldur.metrics.recorders.notification import NotificationMetricRecorder

    return NotificationMetricRecorder()


# =============================================================================
# A. Contract Tests
# =============================================================================


class TestNotificationRecorderContract:
    """C5: NotificationMetricRecorder export contract."""

    def test_exports_class_and_three_convenience_functions(self):
        """__all__ includes class + 3 convenience functions."""
        from baldur.metrics.recorders.notification import __all__

        assert "NotificationMetricRecorder" in __all__
        assert "record_notification_sent" in __all__
        assert "record_notification_suppressed" in __all__
        assert "observe_notification_duration" in __all__


# =============================================================================
# B. Behavior Tests — Recorder Methods
# =============================================================================


class TestNotificationRecorderBehavior:
    """C5: NotificationMetricRecorder methods do not raise."""

    def test_record_sent_success(self, notification_recorder):
        """record_sent with success result does not raise."""
        notification_recorder.record_sent("slack", "high", "success")

    def test_record_sent_failure(self, notification_recorder):
        """record_sent with failure result does not raise."""
        notification_recorder.record_sent("email", "critical", "failure")

    def test_record_suppressed(self, notification_recorder):
        """record_suppressed with cooldown reason does not raise."""
        notification_recorder.record_suppressed("cooldown")

    def test_observe_duration(self, notification_recorder):
        """observe_duration with positive value does not raise."""
        notification_recorder.observe_duration("pagerduty", 1.5)


# =============================================================================
# C. Behavior Tests — Convenience Functions (DD-7)
# =============================================================================


class TestNotificationConvenienceFunctionsBehavior:
    """DD-7: Notification convenience functions delegate to lazy recorder."""

    def test_record_notification_sent_delegates(self):
        """record_notification_sent delegates to recorder.record_sent."""
        from baldur.metrics.recorders.notification import record_notification_sent

        mock_recorder = MagicMock()
        with patch(
            "baldur.metrics.recorders.notification._lazy_recorder",
            return_value=mock_recorder,
            autospec=True,
        ):
            record_notification_sent("slack", "high", "success")
        mock_recorder.record_sent.assert_called_once_with("slack", "high", "success")

    def test_record_notification_suppressed_delegates(self):
        """record_notification_suppressed delegates to recorder.record_suppressed."""
        from baldur.metrics.recorders.notification import (
            record_notification_suppressed,
        )

        mock_recorder = MagicMock()
        with patch(
            "baldur.metrics.recorders.notification._lazy_recorder",
            return_value=mock_recorder,
            autospec=True,
        ):
            record_notification_suppressed("rate_limit")
        mock_recorder.record_suppressed.assert_called_once_with("rate_limit")

    def test_convenience_noop_when_recorder_unavailable(self):
        """Convenience functions silently no-op when recorder is None."""
        from baldur.metrics.recorders.notification import record_notification_sent

        with patch(
            "baldur.metrics.recorders.notification._lazy_recorder",
            return_value=None,
            autospec=True,
        ):
            record_notification_sent("slack", "low", "success")  # Should not raise


# =============================================================================
# D. Contract Tests — Facade Registration
# =============================================================================


class TestNotificationFacadeRegistrationContract:
    """NotificationMetricRecorder registered in BaldurMetrics facade."""

    def test_facade_has_notification_attribute(self):
        """BaldurMetrics exposes notification recorder."""
        from baldur.metrics.prometheus import get_metrics
        from baldur.metrics.recorders.notification import (
            NotificationMetricRecorder,
        )

        m = get_metrics()
        assert isinstance(m.notification, NotificationMetricRecorder)
