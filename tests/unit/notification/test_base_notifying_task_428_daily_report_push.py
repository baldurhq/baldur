"""
Unit tests for 428 — BaseNotifyingTask._add_to_daily_report() integration with
DailyReportCollector.

Test target:
  - baldur.tasks.base.BaseNotifyingTask._add_to_daily_report() — was a TODO
    stub; now pushes cleanup task results via get_daily_report_collector().
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from baldur.tasks.base import BaseNotifyingTask
from baldur.tasks.notification_policy import (
    NotificationPolicy,
    NotificationTiming,
)


class _CleanupTask(BaseNotifyingTask):
    """Minimal task sub-class for exercising _add_to_daily_report()."""

    name = "cleanup_archived_entries"
    notification_policy = NotificationPolicy(
        timing=NotificationTiming.AGGREGATED,
        aggregate=True,
    )

    def run(self, *args, **kwargs):  # pragma: no cover — unused in this test
        return {}


class TestAddToDailyReportBehavior:
    """Verify task pushes result into DailyReportCollector."""

    def test_push_calls_collector_add_result_with_task_name_and_result(self):
        """_add_to_daily_report() forwards task_name + result to collector."""
        task = _CleanupTask()
        mock_collector = MagicMock()

        with patch(
            "baldur.services.daily_report.get_daily_report_collector",
            return_value=mock_collector,
        ):
            task._add_to_daily_report({"archived_count": 12})

        mock_collector.add_result.assert_called_once()
        call_kwargs = mock_collector.add_result.call_args.kwargs
        assert call_kwargs["task_name"] == "cleanup_archived_entries"
        assert call_kwargs["result"] == {"archived_count": 12}

    def test_push_passes_severity_from_get_severity(self):
        """severity is derived via self._get_severity(result)."""
        task = _CleanupTask()
        mock_collector = MagicMock()

        with patch(
            "baldur.services.daily_report.get_daily_report_collector",
            return_value=mock_collector,
        ):
            task._add_to_daily_report({"error": "boom"})

        severity = mock_collector.add_result.call_args.kwargs["severity"]
        # _get_severity() returns "critical" on error/success==False
        assert severity == "critical"

    def test_push_fails_open_when_collector_unavailable(self):
        """Import/construction failure does not propagate — task succeeds."""
        task = _CleanupTask()

        with patch(
            "baldur.services.daily_report.get_daily_report_collector",
            side_effect=RuntimeError("collector boom"),
        ):
            # Should not raise
            task._add_to_daily_report({"archived_count": 1})

    def test_push_fails_open_when_add_result_raises(self):
        """collector.add_result exception does not propagate."""
        task = _CleanupTask()
        mock_collector = MagicMock()
        mock_collector.add_result.side_effect = RuntimeError("cache down")

        with patch(
            "baldur.services.daily_report.get_daily_report_collector",
            return_value=mock_collector,
        ):
            # Should not raise
            task._add_to_daily_report({"archived_count": 1})
