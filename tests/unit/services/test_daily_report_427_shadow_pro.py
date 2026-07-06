"""
Unit tests for Shadow PRO section in daily_report (427 §4.6).

Verification techniques:
- Contract: ShadowProSummary field defaults
- Behavior: _collect_shadow_pro_section, formatter rendering, merge
"""

from __future__ import annotations

from baldur.services.daily_report.formatters import format_report_for_slack
from baldur.services.daily_report.models import (
    DailyAutonomousReport,
    ShadowProSummary,
)
from baldur.services.daily_report.service import DailyReportService


class TestShadowProSummaryContract:
    """ShadowProSummary field defaults (427 §4.6)."""

    def test_cb_trips_default_zero(self):
        """cb_trips_without_auto_degradation defaults to 0."""
        s = ShadowProSummary()
        assert s.cb_trips_without_auto_degradation == 0

    def test_failed_ops_default_zero(self):
        """failed_ops_without_dlq defaults to 0."""
        s = ShadowProSummary()
        assert s.failed_ops_without_dlq == 0

    def test_drift_warnings_default_zero(self):
        """drift_warnings_manual_only defaults to 0."""
        s = ShadowProSummary()
        assert s.drift_warnings_manual_only == 0


class TestCollectShadowProSectionBehavior:
    """_collect_shadow_pro_section behavior."""

    def _make_service(self):
        return DailyReportService.__new__(DailyReportService)

    def test_all_zeros_produces_no_summary(self):
        """Report with no CB trips/failures/drift → shadow_pro_summary stays None."""
        service = self._make_service()
        report = DailyAutonomousReport()
        report.circuit_transitions = 0
        report.task_failures = 0
        report.drift_warnings_count = 0

        service._collect_shadow_pro_section(report)

        assert report.shadow_pro_summary is None

    def test_cb_trips_only_produces_summary(self):
        """CB trips > 0 → summary created with correct count."""
        service = self._make_service()
        report = DailyAutonomousReport()
        report.circuit_transitions = 3
        report.task_failures = 0
        report.drift_warnings_count = 0

        service._collect_shadow_pro_section(report)

        assert report.shadow_pro_summary is not None
        assert report.shadow_pro_summary.cb_trips_without_auto_degradation == 3
        assert report.shadow_pro_summary.failed_ops_without_dlq == 0

    def test_all_indicators_populated(self):
        """All three indicators present → all fields set."""
        service = self._make_service()
        report = DailyAutonomousReport()
        report.circuit_transitions = 5
        report.task_failures = 2
        report.drift_warnings_count = 7

        service._collect_shadow_pro_section(report)

        s = report.shadow_pro_summary
        assert s is not None
        assert s.cb_trips_without_auto_degradation == 5
        assert s.failed_ops_without_dlq == 2
        assert s.drift_warnings_manual_only == 7


class TestShadowProFormatterBehavior:
    """Slack formatter renders shadow PRO section correctly."""

    def test_shadow_pro_rendered_when_present(self):
        """Report with shadow_pro_summary includes 'PRO Insights' header."""
        report = DailyAutonomousReport()
        report.shadow_pro_summary = ShadowProSummary(
            cb_trips_without_auto_degradation=3,
            failed_ops_without_dlq=1,
            drift_warnings_manual_only=2,
        )

        output = format_report_for_slack(report)

        assert "PRO Insights" in output
        assert "3 CB trips without auto-degradation" in output
        assert "1 operations failed permanently" in output
        assert "2 drift warnings, manual resolution only" in output

    def test_shadow_pro_omitted_when_none(self):
        """Report without shadow_pro_summary has no 'PRO Insights' section."""
        report = DailyAutonomousReport()
        report.shadow_pro_summary = None

        output = format_report_for_slack(report)

        assert "PRO Insights" not in output

    def test_zero_indicator_lines_omitted(self):
        """Only non-zero indicators get rendered."""
        report = DailyAutonomousReport()
        report.shadow_pro_summary = ShadowProSummary(
            cb_trips_without_auto_degradation=0,
            failed_ops_without_dlq=4,
            drift_warnings_manual_only=0,
        )

        output = format_report_for_slack(report)

        assert "PRO Insights" in output
        assert "CB trips" not in output
        assert "4 operations failed permanently" in output
        assert "drift warnings" not in output


class TestShadowProToDictBehavior:
    """to_dict() includes shadow_pro_summary when present."""

    def test_to_dict_excludes_shadow_pro_when_none(self):
        """shadow_pro_summary omitted from dict when None."""
        report = DailyAutonomousReport()
        d = report.to_dict()

        assert "shadow_pro_summary" not in d

    def test_to_dict_includes_shadow_pro_when_present(self):
        """shadow_pro_summary included as sub-dict when populated."""
        report = DailyAutonomousReport()
        report.shadow_pro_summary = ShadowProSummary(
            cb_trips_without_auto_degradation=3,
            failed_ops_without_dlq=1,
            drift_warnings_manual_only=2,
        )

        d = report.to_dict()

        assert "shadow_pro_summary" in d
        assert d["shadow_pro_summary"]["cb_trips_without_auto_degradation"] == 3
        assert d["shadow_pro_summary"]["failed_ops_without_dlq"] == 1
        assert d["shadow_pro_summary"]["drift_warnings_manual_only"] == 2


class TestShadowProMergeBehavior:
    """DailyAutonomousReport.merge includes shadow_pro_summary."""

    def test_merge_non_none_wins(self):
        """Non-None shadow_pro_summary in other is merged into self when self is None."""
        report_a = DailyAutonomousReport()
        report_b = DailyAutonomousReport()
        report_b.shadow_pro_summary = ShadowProSummary(
            cb_trips_without_auto_degradation=5,
        )

        report_a.merge(report_b)

        assert report_a.shadow_pro_summary is not None
        assert report_a.shadow_pro_summary.cb_trips_without_auto_degradation == 5

    def test_merge_self_preserved_when_both_set(self):
        """Self's shadow_pro_summary preserved when both have values."""
        report_a = DailyAutonomousReport()
        report_a.shadow_pro_summary = ShadowProSummary(
            cb_trips_without_auto_degradation=2,
        )
        report_b = DailyAutonomousReport()
        report_b.shadow_pro_summary = ShadowProSummary(
            cb_trips_without_auto_degradation=9,
        )

        report_a.merge(report_b)

        assert report_a.shadow_pro_summary.cb_trips_without_auto_degradation == 2
