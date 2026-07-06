"""
Unit tests for 428 — SLADriftDetector daily report batch push.

Test target:
  - baldur.tasks.drift_detection.SLADriftDetector._send_drift_notifications() —
    batch push aggregate counter (drift_warnings_count) to daily report,
    once per detection run.

Design intent:
  - Individual warning detail is already captured via unified_notification entries.
  - Only the aggregate count is forwarded to the daily report to avoid
    per-warning entry spam.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from baldur.tasks.drift_detection import SLADriftDetector


def _make_detector() -> SLADriftDetector:
    """Construct a detector with minimal no-op dependencies."""
    return SLADriftDetector(
        get_sla_thresholds=lambda: MagicMock(get_all_thresholds=lambda: {}),
        get_failed_operations=lambda **kw: MagicMock(
            count=lambda: 0, __iter__=lambda s: iter([])
        ),
    )


class TestSendDriftNotificationsDailyReportPushBehavior:
    """428: daily report batch push in _send_drift_notifications."""

    @pytest.fixture(autouse=True)
    def _require_pro(self):
        pytest.importorskip("baldur_pro")

    def test_push_skipped_when_warnings_list_is_empty(self):
        """No warnings -> collector never called (no zero-count push)."""
        detector = _make_detector()
        mock_collector = MagicMock()

        with (
            patch(
                "baldur_pro.services.unified_notification.notify_sla",
            ),
            patch(
                "baldur.services.daily_report.get_daily_report_collector",
                return_value=mock_collector,
            ),
        ):
            detector._send_drift_notifications([])

        mock_collector.add_result.assert_not_called()

    def test_single_push_for_n_warnings(self):
        """N warnings -> exactly one add_result call with drift_warnings_count=N."""
        detector = _make_detector()
        mock_collector = MagicMock()

        warnings = [
            {
                "type": "SLA_BREACH_RATE_HIGH",
                "domain": "payment",
                "severity": "warning",
                "message": "m1",
            },
            {
                "type": "SLA_APPROACHING_LIMIT",
                "domain": "inventory",
                "severity": "warning",
                "message": "m2",
            },
            {
                "type": "PENDING_ITEMS_AT_RISK",
                "domain": "payment",
                "severity": "warning",
                "message": "m3",
            },
        ]

        with (
            patch(
                "baldur_pro.services.unified_notification.notify_sla",
            ),
            patch(
                "baldur.services.daily_report.get_daily_report_collector",
                return_value=mock_collector,
            ),
        ):
            detector._send_drift_notifications(warnings)

        mock_collector.add_result.assert_called_once_with(
            task_name="sla.drift_warning",
            result={"drift_warnings_count": 3},
        )

    def test_push_fails_open_when_collector_unavailable(self):
        """Collector import/construction failure does not propagate."""
        detector = _make_detector()
        warnings = [
            {"type": "X", "domain": "a", "severity": "warning", "message": "m"},
        ]

        with (
            patch(
                "baldur_pro.services.unified_notification.notify_sla",
            ),
            patch(
                "baldur.services.daily_report.get_daily_report_collector",
                side_effect=RuntimeError("collector boom"),
            ),
        ):
            # Should not raise
            detector._send_drift_notifications(warnings)
