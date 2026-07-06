"""
Unit tests for #412 Daily Report Data Completeness — Formatter layer.

Test targets:
  - format_report_for_slack() — adaptive DLQ/Chaos/LS/EB sections
  - format_report_for_pagerduty() — actionable items English summary
"""

from __future__ import annotations

from baldur.services.daily_report.formatters import (
    format_report_for_pagerduty,
    format_report_for_slack,
)
from baldur.services.daily_report.models import (
    ChaosReportSummary,
    DailyAutonomousReport,
    ErrorBudgetGateSummary,
    LoadSheddingSummary,
)

# =============================================================================
# format_report_for_slack() — Behavior Tests
# =============================================================================


class TestSlackFormatterAdaptiveSectionsBehavior:
    """Verify Slack formatter renders sections conditionally."""

    def test_dlq_section_shown_when_pending_count_nonzero(self):
        """DLQ section renders when dlq_pending_count > 0 (new 428 compact format)."""
        report = DailyAutonomousReport()
        report.dlq_pending_count = 5

        result = format_report_for_slack(report)

        assert "📬 DLQ:" in result
        assert "5 pending" in result

    def test_dlq_section_hidden_when_all_zero(self):
        """DLQ section omitted when all DLQ counters are 0."""
        report = DailyAutonomousReport()

        result = format_report_for_slack(report)

        assert "DLQ" not in result

    def test_dlq_sub_line_shown_when_resolution_details_exist(self):
        """DLQ sub-line (manual/TTL/retries) shown when any > 0."""
        report = DailyAutonomousReport()
        report.dlq_new_entries_count = 1
        report.dlq_manual_resolutions = 3
        report.dlq_ttl_expired = 2

        result = format_report_for_slack(report)

        assert "Manual 3" in result
        assert "TTL expired 2" in result

    def test_dlq_sub_line_hidden_when_no_resolution_details(self):
        """DLQ sub-line omitted when manual/TTL/retries all 0."""
        report = DailyAutonomousReport()
        report.dlq_new_entries_count = 1

        result = format_report_for_slack(report)

        assert "📬 DLQ:" in result
        assert "Manual" not in result

    def test_chaos_section_suppressed_deferred_tier(self):
        """Chaos is a Deferred-tier section -> suppressed from the Slack digest (D5)."""
        report = DailyAutonomousReport()
        report.chaos_summary = ChaosReportSummary(
            grade="B",
            grade_trend="improving",
            experiments_total=10,
            experiments_passed=8,
        )

        result = format_report_for_slack(report)

        assert "Chaos Resilience" not in result
        assert "Chaos" not in result

    def test_chaos_section_hidden_when_summary_none(self):
        """Chaos section omitted when chaos_summary is None."""
        report = DailyAutonomousReport()

        result = format_report_for_slack(report)

        assert "Chaos" not in result

    def test_chaos_sla_sub_line_suppressed_deferred_tier(self):
        """Chaos SLA sub-line is part of the suppressed Deferred chaos section (D5)."""
        report = DailyAutonomousReport()
        report.chaos_summary = ChaosReportSummary(
            grade="C",
            grade_trend="declining",
            experiments_total=5,
            sla_breaches=2,
            error_budget_consumed_pct=15.5,
        )

        result = format_report_for_slack(report)

        assert "SLA breaches" not in result
        assert "Chaos" not in result

    def test_load_shedding_section_suppressed_deferred_tier(self):
        """Load shedding is a Deferred-tier section -> suppressed from the digest (D5)."""
        report = DailyAutonomousReport()
        report.load_shedding_summary = LoadSheddingSummary(
            level="medium",
            dropped_total=100,
            processed_total=900,
        )

        result = format_report_for_slack(report)

        assert "Load Shedding" not in result

    def test_load_shedding_tier_breakdown_suppressed_deferred_tier(self):
        """LS tier breakdown is part of the suppressed Deferred load-shedding section (D5)."""
        report = DailyAutonomousReport()
        report.load_shedding_summary = LoadSheddingSummary(
            level="high",
            dropped_total=50,
            processed_total=500,
            dropped_by_tier={"non_essential": 30, "standard": 20},
        )

        result = format_report_for_slack(report)

        assert "By tier:" not in result
        assert "Load Shedding" not in result

    def test_error_budget_section_suppressed_deferred_tier(self):
        """Error budget is a Deferred-tier section -> suppressed from the digest (D5)."""
        report = DailyAutonomousReport()
        report.error_budget_summary = ErrorBudgetGateSummary(blocks=4, warnings=12)

        result = format_report_for_slack(report)

        assert "Error Budget Gate" not in result
        assert "Error Budget" not in result

    def test_error_budget_section_hidden_when_summary_none(self):
        """Error budget section omitted when error_budget_summary is None."""
        report = DailyAutonomousReport()

        result = format_report_for_slack(report)

        assert "Error Budget" not in result

    def test_shipped_sections_render_deferred_suppressed_simultaneously(self):
        """Shipped sections (DLQ) render while Deferred sections (chaos / LS / EB)
        stay suppressed even when all are present together (D5)."""
        report = DailyAutonomousReport()
        report.dlq_pending_count = 5
        report.dlq_new_entries_count = 3
        report.chaos_summary = ChaosReportSummary(grade="A", grade_trend="stable")
        report.load_shedding_summary = LoadSheddingSummary(level="none")
        report.error_budget_summary = ErrorBudgetGateSummary(blocks=1, warnings=2)

        result = format_report_for_slack(report)

        assert "DLQ" in result
        assert "Chaos" not in result
        assert "Load Shedding" not in result
        assert "Error Budget" not in result


# =============================================================================
# format_report_for_pagerduty() — Behavior Tests
# =============================================================================


class TestPagerDutyFormatterBehavior:
    """Verify PagerDuty formatter outputs actionable items in English."""

    def test_empty_report_returns_empty_string(self):
        """Empty report with no actionable items returns empty string."""
        report = DailyAutonomousReport()

        result = format_report_for_pagerduty(report)

        assert result == ""

    def test_error_budget_blocks_excluded_deferred_tier(self):
        """error_budget is Deferred -> excluded from the PD summary (D5)."""
        report = DailyAutonomousReport()
        report.error_budget_summary = ErrorBudgetGateSummary(blocks=3)

        result = format_report_for_pagerduty(report)

        assert "ErrorBudget" not in result

    def test_chaos_grade_d_or_f_excluded_deferred_tier(self):
        """chaos is Deferred -> grade D/F excluded from the PD summary (D5)."""
        report = DailyAutonomousReport()
        report.chaos_summary = ChaosReportSummary(
            grade="F",
            experiments_total=10,
            experiments_failed=7,
        )

        result = format_report_for_pagerduty(report)

        assert "Chaos" not in result

    def test_chaos_grade_a_excluded(self):
        """Chaos grade A is not actionable — excluded from PD summary."""
        report = DailyAutonomousReport()
        report.chaos_summary = ChaosReportSummary(grade="A")

        result = format_report_for_pagerduty(report)

        assert "Chaos" not in result

    def test_load_shedding_high_or_critical_excluded_deferred_tier(self):
        """load_shedding is Deferred -> high/critical excluded from the PD summary (D5)."""
        report = DailyAutonomousReport()
        report.load_shedding_summary = LoadSheddingSummary(
            level="critical",
            dropped_total=500,
        )

        result = format_report_for_pagerduty(report)

        assert "LoadShedding" not in result

    def test_load_shedding_low_excluded(self):
        """Load shedding low level is not actionable — excluded."""
        report = DailyAutonomousReport()
        report.load_shedding_summary = LoadSheddingSummary(level="low")

        result = format_report_for_pagerduty(report)

        assert "LoadShedding" not in result

    def test_critical_alerts_included(self):
        """Critical alerts count appears in PD summary."""
        report = DailyAutonomousReport()
        report.critical_alerts = 3

        result = format_report_for_pagerduty(report)

        assert "3 critical alerts" in result

    def test_task_failures_below_threshold_excluded(self):
        """Task failures < 5 are not actionable — excluded."""
        report = DailyAutonomousReport()
        report.task_failures = 4

        result = format_report_for_pagerduty(report)

        assert "task failures" not in result

    def test_task_failures_at_threshold_included(self):
        """Task failures >= 5 appear in PD summary."""
        report = DailyAutonomousReport()
        report.task_failures = 5

        result = format_report_for_pagerduty(report)

        assert "5 task failures" in result

    def test_multiple_items_joined_with_pipe(self):
        """Multiple shipped actionable items are joined with ' | ' (Deferred excluded)."""
        report = DailyAutonomousReport()
        report.critical_alerts = 2
        report.task_failures = 10

        result = format_report_for_pagerduty(report)

        assert " | " in result
        assert result.count(" | ") == 1  # 2 shipped items = 1 pipe

    def test_dlq_excluded_from_pagerduty(self):
        """DLQ stats intentionally excluded from PD summary (not a trigger condition)."""
        report = DailyAutonomousReport()
        report.dlq_pending_count = 100
        report.dlq_new_entries_count = 50

        result = format_report_for_pagerduty(report)

        assert "DLQ" not in result
        assert "dlq" not in result
