"""
Unit tests for #412 Daily Report Data Completeness — Service layer.

Test targets:
  - DailyReportService._collect_snapshots() — XP3, XP5, D8 supplement, UU-E5
  - DailyReportService._determine_severity()
  - DailyReportService._has_actionable_items()
  - DailyReportService._has_critical_items()
  - DailyReportService._send_to_channel() — two-track routing
  - DailyReportService._send_report() — channels=[channel] routing bug fix
  - DailyReportService.generate_and_send_report() — skip condition with snapshots
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest

from baldur.services.daily_report.models import (
    ChaosReportSummary,
    DailyAutonomousReport,
    ErrorBudgetGateSummary,
    LoadSheddingSummary,
    TaskResultEntry,
)
from baldur.services.daily_report.service import DailyReportService

# =============================================================================
# _determine_severity() — Behavior Tests
# =============================================================================


class TestDetermineSeverityBehavior:
    """Verify 3-way severity determination logic."""

    def test_critical_when_critical_alerts_nonzero(self):
        """critical_alerts > 0 -> severity 'critical'."""
        svc = DailyReportService()
        report = DailyAutonomousReport()
        report.critical_alerts = 1

        assert svc._determine_severity(report) == "critical"

    def test_warning_when_task_failures_nonzero(self):
        """task_failures > 0 (no critical) -> severity 'warning'."""
        svc = DailyReportService()
        report = DailyAutonomousReport()
        report.task_failures = 3

        assert svc._determine_severity(report) == "warning"

    def test_info_when_no_alerts_no_failures(self):
        """No critical alerts or task failures -> severity 'info'."""
        svc = DailyReportService()
        report = DailyAutonomousReport()

        assert svc._determine_severity(report) == "info"

    def test_critical_takes_precedence_over_warning(self):
        """critical_alerts > 0 wins even if task_failures > 0."""
        svc = DailyReportService()
        report = DailyAutonomousReport()
        report.critical_alerts = 1
        report.task_failures = 5

        assert svc._determine_severity(report) == "critical"


# =============================================================================
# _has_actionable_items() — Behavior Tests
# =============================================================================


class TestHasActionableItemsBehavior:
    """Verify 5 trigger conditions for PagerDuty alert."""

    def test_true_when_critical_alerts(self):
        """critical_alerts > 0 -> actionable."""
        svc = DailyReportService()
        report = DailyAutonomousReport()
        report.critical_alerts = 1
        assert svc._has_actionable_items(report) is True

    def test_true_when_task_failures_gte_5(self):
        """task_failures >= 5 -> actionable."""
        svc = DailyReportService()
        report = DailyAutonomousReport()
        report.task_failures = 5
        assert svc._has_actionable_items(report) is True

    def test_false_when_task_failures_lt_5(self):
        """task_failures < 5 -> not actionable (boundary)."""
        svc = DailyReportService()
        report = DailyAutonomousReport()
        report.task_failures = 4
        assert svc._has_actionable_items(report) is False

    def test_false_when_only_error_budget_blocks_deferred(self):
        """error_budget is a Deferred-tier signal -> gated out of actionable (D5)."""
        svc = DailyReportService()
        report = DailyAutonomousReport()
        report.error_budget_summary = ErrorBudgetGateSummary(blocks=1)
        assert svc._has_actionable_items(report) is False

    def test_false_when_only_chaos_grade_d_deferred(self):
        """chaos is a Deferred-tier signal -> grade D gated out of actionable (D5)."""
        svc = DailyReportService()
        report = DailyAutonomousReport()
        report.chaos_summary = ChaosReportSummary(grade="D")
        assert svc._has_actionable_items(report) is False

    def test_false_when_only_chaos_grade_f_deferred(self):
        """chaos is a Deferred-tier signal -> grade F gated out of actionable (D5)."""
        svc = DailyReportService()
        report = DailyAutonomousReport()
        report.chaos_summary = ChaosReportSummary(grade="F")
        assert svc._has_actionable_items(report) is False

    def test_false_when_chaos_grade_c(self):
        """Chaos grade C -> not actionable."""
        svc = DailyReportService()
        report = DailyAutonomousReport()
        report.chaos_summary = ChaosReportSummary(grade="C")
        assert svc._has_actionable_items(report) is False

    def test_false_when_only_load_shedding_high_deferred(self):
        """load_shedding is a Deferred-tier signal -> 'high' gated out of actionable (D5)."""
        svc = DailyReportService()
        report = DailyAutonomousReport()
        report.load_shedding_summary = LoadSheddingSummary(level="high")
        assert svc._has_actionable_items(report) is False

    def test_false_when_only_load_shedding_critical_deferred(self):
        """load_shedding is a Deferred-tier signal -> 'critical' gated out of actionable (D5)."""
        svc = DailyReportService()
        report = DailyAutonomousReport()
        report.load_shedding_summary = LoadSheddingSummary(level="critical")
        assert svc._has_actionable_items(report) is False

    def test_false_when_load_shedding_medium(self):
        """Load shedding level 'medium' -> not actionable."""
        svc = DailyReportService()
        report = DailyAutonomousReport()
        report.load_shedding_summary = LoadSheddingSummary(level="medium")
        assert svc._has_actionable_items(report) is False

    def test_false_when_empty_report(self):
        """Empty report -> not actionable."""
        svc = DailyReportService()
        report = DailyAutonomousReport()
        assert svc._has_actionable_items(report) is False


# =============================================================================
# _has_critical_items() — Behavior Tests
# =============================================================================


class TestHasCriticalItemsBehavior:
    """Verify 2 conditions for PagerDuty critical severity."""

    def test_true_when_critical_alerts(self):
        """critical_alerts > 0 -> critical."""
        svc = DailyReportService()
        report = DailyAutonomousReport()
        report.critical_alerts = 1
        assert svc._has_critical_items(report) is True

    def test_false_when_only_error_budget_blocks_deferred(self):
        """error_budget is a Deferred-tier signal -> gated out of critical (D5)."""
        svc = DailyReportService()
        report = DailyAutonomousReport()
        report.error_budget_summary = ErrorBudgetGateSummary(blocks=1)
        assert svc._has_critical_items(report) is False

    def test_false_when_only_task_failures(self):
        """task_failures alone -> NOT critical (only actionable)."""
        svc = DailyReportService()
        report = DailyAutonomousReport()
        report.task_failures = 10
        assert svc._has_critical_items(report) is False

    def test_false_when_chaos_grade_f(self):
        """Chaos grade F is a Deferred-tier signal -> NOT critical (and gated out)."""
        svc = DailyReportService()
        report = DailyAutonomousReport()
        report.chaos_summary = ChaosReportSummary(grade="F")
        assert svc._has_critical_items(report) is False


# =============================================================================
# _send_to_channel() — Behavior Tests
# =============================================================================


class TestSendToChannelBehavior:
    """Verify two-track channel routing logic."""

    def test_slack_channel_uses_slack_formatter(self):
        """Slack channel routes through format_report_for_slack."""
        svc = DailyReportService()
        report = DailyAutonomousReport()
        report.entries.append(
            TaskResultEntry(
                task_name="test",
                result={},
                timestamp=datetime.now(UTC),
            )
        )

        with patch.object(svc, "_send_report") as mock_send:
            svc._send_to_channel(report, "slack")

        mock_send.assert_called_once()
        call_kwargs = mock_send.call_args
        assert call_kwargs[1]["channel"] == "slack"

    def test_pagerduty_skipped_when_not_actionable(self):
        """PagerDuty channel skipped when no actionable items."""
        svc = DailyReportService()
        report = DailyAutonomousReport()

        with patch.object(svc, "_send_report") as mock_send:
            svc._send_to_channel(report, "pagerduty")

        mock_send.assert_not_called()

    def test_pagerduty_sent_when_actionable(self):
        """PagerDuty channel sends when actionable items exist."""
        svc = DailyReportService()
        report = DailyAutonomousReport()
        report.critical_alerts = 1

        with patch.object(svc, "_send_report") as mock_send:
            svc._send_to_channel(report, "pagerduty")

        mock_send.assert_called_once()
        call_kwargs = mock_send.call_args
        assert call_kwargs[1]["channel"] == "pagerduty"

    def test_pagerduty_severity_critical_when_critical_items(self):
        """PagerDuty uses 'critical' severity when critical items exist."""
        svc = DailyReportService()
        report = DailyAutonomousReport()
        report.critical_alerts = 2

        with patch.object(svc, "_send_report") as mock_send:
            svc._send_to_channel(report, "pagerduty")

        args = mock_send.call_args
        assert args[0][2] == "critical"  # severity positional arg

    def test_pagerduty_severity_error_when_no_critical_items(self):
        """PagerDuty uses 'error' severity for actionable but non-critical items."""
        svc = DailyReportService()
        report = DailyAutonomousReport()
        report.task_failures = 10  # actionable but not critical

        with patch.object(svc, "_send_report") as mock_send:
            svc._send_to_channel(report, "pagerduty")

        args = mock_send.call_args
        assert args[0][2] == "error"  # severity positional arg

    def test_unknown_channel_skipped_with_warning(self):
        """Unknown channel is skipped without error."""
        svc = DailyReportService()
        report = DailyAutonomousReport()

        with patch.object(svc, "_send_report") as mock_send:
            svc._send_to_channel(report, "sms")

        mock_send.assert_not_called()


# =============================================================================
# _send_report() — Behavior Tests
# =============================================================================


class TestSendReportRoutingBugFixBehavior:
    """Verify routing bug fix: channels=[channel] parameter passed to notify()."""

    @pytest.fixture(autouse=True)
    def _require_pro(self):
        pytest.importorskip("baldur_pro")

    def test_send_report_passes_channels_list(self):
        """_send_report passes channels=[channel] to notify()."""
        svc = DailyReportService()
        report = DailyAutonomousReport()

        with patch(
            "baldur_pro.services.unified_notification.notify",
            return_value=MagicMock(),
        ) as mock_notify:
            svc._send_report(report, "test message", "info", channel="slack")

        mock_notify.assert_called_once()
        call_kwargs = mock_notify.call_args[1]
        assert call_kwargs["channels"] == ["slack"]

    def test_send_report_passes_pagerduty_channel(self):
        """_send_report passes channels=['pagerduty'] for PD track."""
        svc = DailyReportService()
        report = DailyAutonomousReport()

        with patch(
            "baldur_pro.services.unified_notification.notify",
            return_value=MagicMock(),
        ) as mock_notify:
            svc._send_report(report, "alert", "error", channel="pagerduty")

        call_kwargs = mock_notify.call_args[1]
        assert call_kwargs["channels"] == ["pagerduty"]


# =============================================================================
# _collect_snapshots() — Behavior Tests
# =============================================================================


class TestCollectSnapshotsBehavior:
    """Verify snapshot collection with fail-open semantics."""

    @pytest.fixture(autouse=True)
    def _require_pro(self):
        pytest.importorskip("baldur_pro")

    def test_chaos_snapshot_populates_summary(self):
        """XP3: chaos report populates chaos_summary when available."""
        svc = DailyReportService()
        report = DailyAutonomousReport()
        date = datetime(2026, 4, 4, tzinfo=UTC)

        mock_chaos_report = MagicMock()
        mock_chaos_report.grade = "B"
        mock_chaos_report.grade_trend = "improving"
        mock_chaos_report.total_experiments = 10
        mock_chaos_report.passed_experiments = 8
        mock_chaos_report.failed_experiments = 2
        mock_chaos_report.total_sla_breaches = 1
        mock_chaos_report.error_budget_consumed_percent = 5.0

        mock_generator = MagicMock()
        mock_generator.get_report_by_date.return_value = mock_chaos_report

        with patch(
            "baldur_pro.services.chaos.reports.get_report_generator",
            return_value=mock_generator,
        ):
            svc._collect_snapshots(report, date)

        assert report.chaos_summary is not None
        assert report.chaos_summary.grade == "B"
        assert report.chaos_summary.experiments_total == 10

    def test_chaos_snapshot_fail_open_on_exception(self):
        """XP3: chaos module exception does not block snapshot collection."""
        svc = DailyReportService()
        report = DailyAutonomousReport()
        date = datetime(2026, 4, 4, tzinfo=UTC)

        with patch(
            "baldur_pro.services.chaos.reports.get_report_generator",
            side_effect=ImportError("chaos not installed"),
        ):
            svc._collect_snapshots(report, date)

        assert report.chaos_summary is None

    def test_load_shedding_in_memory_fallback_with_false_zero_guard(self):
        """XP5: False Zero guard skips section when processed+dropped==0."""
        svc = DailyReportService()
        report = DailyAutonomousReport()
        date = datetime(2026, 4, 4, tzinfo=UTC)

        mock_state = MagicMock()
        mock_state.processed_count = 0
        mock_state.dropped_count = 0

        mock_controller = MagicMock()
        mock_controller.get_state.return_value = mock_state

        with (
            patch(
                "baldur_pro.services.chaos.reports.get_report_generator",
                side_effect=Exception,
            ),
            patch(
                "baldur.factory.ProviderRegistry.get_cache",
                side_effect=Exception("no redis"),
            ),
            patch(
                "baldur.scaling.rate_controller.get_rate_controller",
                return_value=mock_controller,
            ),
        ):
            svc._collect_snapshots(report, date)

        assert report.load_shedding_summary is None

    def test_load_shedding_in_memory_populated_when_nonzero(self):
        """XP5: in-memory fallback populates summary when counters > 0."""
        svc = DailyReportService()
        report = DailyAutonomousReport()
        date = datetime(2026, 4, 4, tzinfo=UTC)

        mock_state = MagicMock()
        mock_state.processed_count = 1000
        mock_state.dropped_count = 50
        mock_state.dropped_by_tier = {"non_essential": 50}
        mock_state.processed_by_tier = {"critical": 500, "standard": 500}
        mock_state.level = MagicMock(value="medium")

        mock_controller = MagicMock()
        mock_controller.get_state.return_value = mock_state

        with (
            patch(
                "baldur_pro.services.chaos.reports.get_report_generator",
                side_effect=Exception,
            ),
            patch(
                "baldur.factory.ProviderRegistry.get_cache",
                side_effect=Exception("no redis"),
            ),
            patch(
                "baldur.scaling.rate_controller.get_rate_controller",
                return_value=mock_controller,
            ),
        ):
            svc._collect_snapshots(report, date)

        assert report.load_shedding_summary is not None
        assert report.load_shedding_summary.dropped_total == 50
        assert report.load_shedding_summary.processed_total == 1000
        assert report.load_shedding_summary.level == "medium"

    def test_dlq_pending_snapshot_populates_gauge(self):
        """D8: dlq_pending_count populated from update_dlq_pending_gauges()."""
        svc = DailyReportService()
        report = DailyAutonomousReport()
        date = datetime(2026, 4, 4, tzinfo=UTC)

        with (
            patch(
                "baldur_pro.services.chaos.reports.get_report_generator",
                side_effect=Exception,
            ),
            patch(
                "baldur.factory.ProviderRegistry.get_cache",
                side_effect=Exception,
            ),
            patch(
                "baldur.scaling.rate_controller.get_rate_controller",
                side_effect=Exception,
            ),
            patch(
                "baldur.services.metrics.updaters.update_dlq_pending_gauges",
                return_value={"payment": 10, "inventory": 5},
            ),
        ):
            svc._collect_snapshots(report, date)

        assert report.dlq_pending_count == 15

    def test_error_budget_entries_extracted_and_purged(self):
        """UU-E5: EB entries counted into summary, then purged from entries."""
        svc = DailyReportService()
        report = DailyAutonomousReport()
        date = datetime(2026, 4, 4, tzinfo=UTC)

        # Simulate accumulated EB entries
        report.entries = [
            TaskResultEntry(
                task_name="error_budget_gate_blocked",
                result={"budget_percent": 95.0},
                timestamp=datetime.now(UTC),
                severity="warning",
            ),
            TaskResultEntry(
                task_name="error_budget_gate_blocked",
                result={"budget_percent": 96.0},
                timestamp=datetime.now(UTC),
                severity="warning",
            ),
            TaskResultEntry(
                task_name="error_budget_gate_warning",
                result={"budget_percent": 82.0},
                timestamp=datetime.now(UTC),
                severity="info",
            ),
            TaskResultEntry(
                task_name="normal_task",
                result={"archived_count": 1},
                timestamp=datetime.now(UTC),
            ),
        ]

        with (
            patch(
                "baldur_pro.services.chaos.reports.get_report_generator",
                side_effect=Exception,
            ),
            patch(
                "baldur.factory.ProviderRegistry.get_cache",
                side_effect=Exception,
            ),
            patch(
                "baldur.scaling.rate_controller.get_rate_controller",
                side_effect=Exception,
            ),
            patch(
                "baldur.services.metrics.updaters.update_dlq_pending_gauges",
                side_effect=Exception,
            ),
        ):
            svc._collect_snapshots(report, date)

        # Summary extracted
        assert report.error_budget_summary is not None
        assert report.error_budget_summary.blocks == 2
        assert report.error_budget_summary.warnings == 1

        # EB entries purged, normal entry preserved
        assert len(report.entries) == 1
        assert report.entries[0].task_name == "normal_task"

    def test_no_error_budget_summary_when_no_eb_entries(self):
        """UU-E5: no EB entries -> error_budget_summary stays None."""
        svc = DailyReportService()
        report = DailyAutonomousReport()
        date = datetime(2026, 4, 4, tzinfo=UTC)

        with (
            patch(
                "baldur_pro.services.chaos.reports.get_report_generator",
                side_effect=Exception,
            ),
            patch(
                "baldur.factory.ProviderRegistry.get_cache",
                side_effect=Exception,
            ),
            patch(
                "baldur.scaling.rate_controller.get_rate_controller",
                side_effect=Exception,
            ),
            patch(
                "baldur.services.metrics.updaters.update_dlq_pending_gauges",
                side_effect=Exception,
            ),
        ):
            svc._collect_snapshots(report, date)

        assert report.error_budget_summary is None


# =============================================================================
# generate_and_send_report() — Behavior Tests
# =============================================================================


class TestGenerateAndSendReportSkipConditionBehavior:
    """Verify skip condition includes snapshot data."""

    def test_no_skip_when_entries_zero_but_snapshot_present(self):
        """Report NOT skipped when entries=0 but chaos_summary exists."""
        svc = DailyReportService()

        report = DailyAutonomousReport()
        report.chaos_summary = ChaosReportSummary(grade="F")

        with (
            patch(
                "baldur.services.daily_report.service.aggregate_daily_results",
                return_value=report,
            ),
            patch.object(svc, "_collect_snapshots"),
            patch.object(svc, "_send_to_channel"),
            patch(
                "baldur.services.daily_report.service._get_daily_report_recorder",
                return_value=None,
            ),
        ):
            result = svc.generate_and_send_report(
                date=datetime(2026, 4, 4, tzinfo=UTC),
                channels=["slack"],
            )

        assert result.success is True
        assert result.skipped is False

    def test_skip_when_no_entries_and_no_snapshots(self):
        """Report skipped when both entries and snapshots are empty."""
        svc = DailyReportService()

        report = DailyAutonomousReport()

        with (
            patch(
                "baldur.services.daily_report.service.aggregate_daily_results",
                return_value=report,
            ),
            patch.object(svc, "_collect_snapshots"),
            patch(
                "baldur.services.daily_report.service._get_daily_report_recorder",
                return_value=None,
            ),
        ):
            result = svc.generate_and_send_report(
                date=datetime(2026, 4, 4, tzinfo=UTC),
                channels=["slack"],
            )

        assert result.skipped is True
        assert result.skip_reason == "no_data"
