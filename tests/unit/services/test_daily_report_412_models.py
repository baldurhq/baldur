"""
Unit tests for #412 Daily Report Data Completeness — Model layer.

Test targets:
  - ChaosReportSummary, LoadSheddingSummary, ErrorBudgetGateSummary (new dataclasses)
  - DailyAutonomousReport.to_dict() — DLQ 6 fields + typed summaries
  - DailyAutonomousReport.merge() — counter/gauge/summary group semantics
"""

from __future__ import annotations

from datetime import UTC, datetime

from baldur.services.daily_report.models import (
    ChaosReportSummary,
    DailyAutonomousReport,
    ErrorBudgetGateSummary,
    LoadSheddingSummary,
    TaskResultEntry,
)

# =============================================================================
# Typed Summary Dataclasses — Contract Tests
# =============================================================================


class TestChaosReportSummaryContract:
    """ChaosReportSummary default values and serialization contract."""

    def test_default_values_all_zero_or_empty(self):
        """All fields default to zero/empty string."""
        s = ChaosReportSummary()
        assert s.grade == ""
        assert s.grade_trend == ""
        assert s.experiments_total == 0
        assert s.experiments_passed == 0
        assert s.experiments_failed == 0
        assert s.sla_breaches == 0
        assert s.error_budget_consumed_pct == 0.0

    def test_to_dict_contains_all_seven_fields(self):
        """to_dict() includes all 7 fields."""
        s = ChaosReportSummary(grade="B", grade_trend="improving")
        d = s.to_dict()
        expected_keys = {
            "grade",
            "grade_trend",
            "experiments_total",
            "experiments_passed",
            "experiments_failed",
            "sla_breaches",
            "error_budget_consumed_pct",
        }
        assert set(d.keys()) == expected_keys


class TestLoadSheddingSummaryContract:
    """LoadSheddingSummary default values and serialization contract."""

    def test_default_values_all_zero_or_empty(self):
        """All fields default to zero/empty."""
        s = LoadSheddingSummary()
        assert s.dropped_total == 0
        assert s.dropped_by_tier == {}
        assert s.processed_total == 0
        assert s.processed_by_tier == {}
        assert s.level == ""

    def test_to_dict_contains_all_five_fields(self):
        """to_dict() includes all 5 fields."""
        s = LoadSheddingSummary(level="high", dropped_total=10)
        d = s.to_dict()
        expected_keys = {
            "dropped_total",
            "dropped_by_tier",
            "processed_total",
            "processed_by_tier",
            "level",
        }
        assert set(d.keys()) == expected_keys


class TestErrorBudgetGateSummaryContract:
    """ErrorBudgetGateSummary default values and serialization contract."""

    def test_default_values_both_zero(self):
        """blocks and warnings default to 0."""
        s = ErrorBudgetGateSummary()
        assert s.blocks == 0
        assert s.warnings == 0

    def test_to_dict_contains_two_fields(self):
        """to_dict() includes blocks and warnings."""
        s = ErrorBudgetGateSummary(blocks=3, warnings=7)
        d = s.to_dict()
        assert d == {"blocks": 3, "warnings": 7}


# =============================================================================
# DailyAutonomousReport.to_dict() — Contract Tests
# =============================================================================


class TestDailyReportToDictContract:
    """to_dict() output structure contract verification."""

    def test_to_dict_includes_all_six_dlq_fields(self):
        """D8: to_dict() must include all 6 DLQ fields."""
        report = DailyAutonomousReport()
        report.dlq_pending_count = 5
        report.dlq_new_entries_count = 10
        report.dlq_resolved_count = 3
        report.dlq_manual_resolutions = 1
        report.dlq_ttl_expired = 2
        report.dlq_max_retries_exhausted = 4

        d = report.to_dict()

        assert d["dlq_pending_count"] == 5
        assert d["dlq_new_entries_count"] == 10
        assert d["dlq_resolved_count"] == 3
        assert d["dlq_manual_resolutions"] == 1
        assert d["dlq_ttl_expired"] == 2
        assert d["dlq_max_retries_exhausted"] == 4

    def test_to_dict_excludes_summaries_when_none(self):
        """Typed summaries are excluded when None (module not installed/inactive)."""
        report = DailyAutonomousReport()
        d = report.to_dict()

        assert "chaos_summary" not in d
        assert "load_shedding_summary" not in d
        assert "error_budget_summary" not in d

    def test_to_dict_includes_summaries_when_present(self):
        """Typed summaries are included as sub-dicts when populated."""
        report = DailyAutonomousReport()
        report.chaos_summary = ChaosReportSummary(grade="A")
        report.load_shedding_summary = LoadSheddingSummary(level="none")
        report.error_budget_summary = ErrorBudgetGateSummary(blocks=1)

        d = report.to_dict()

        assert d["chaos_summary"]["grade"] == "A"
        assert d["load_shedding_summary"]["level"] == "none"
        assert d["error_budget_summary"]["blocks"] == 1


# =============================================================================
# DailyAutonomousReport.merge() — Behavior Tests
# =============================================================================


class TestDailyReportMerge412Behavior:
    """Behavior verification for merge() counter/gauge/summary groups."""

    def test_merge_dlq_pending_count_uses_max(self):
        """dlq_pending_count (gauge) uses max() not sum()."""
        # Given
        report_a = DailyAutonomousReport()
        report_a.dlq_pending_count = 30

        report_b = DailyAutonomousReport()
        report_b.dlq_pending_count = 20

        # When
        report_a.merge(report_b)

        # Then — max(30, 20) = 30, NOT 30 + 20 = 50
        assert report_a.dlq_pending_count == 30

    def test_merge_dlq_pending_count_takes_other_when_larger(self):
        """dlq_pending_count takes other value when it is larger."""
        # Given
        report_a = DailyAutonomousReport()
        report_a.dlq_pending_count = 10

        report_b = DailyAutonomousReport()
        report_b.dlq_pending_count = 50

        # When
        report_a.merge(report_b)

        # Then
        assert report_a.dlq_pending_count == 50

    def test_merge_typed_summaries_non_none_wins(self):
        """Typed summaries: non-None other wins when self is None."""
        # Given
        report_a = DailyAutonomousReport()
        report_b = DailyAutonomousReport()
        report_b.chaos_summary = ChaosReportSummary(grade="B")
        report_b.error_budget_summary = ErrorBudgetGateSummary(blocks=5)

        # When
        report_a.merge(report_b)

        # Then
        assert report_a.chaos_summary is not None
        assert report_a.chaos_summary.grade == "B"
        assert report_a.error_budget_summary is not None
        assert report_a.error_budget_summary.blocks == 5
        assert report_a.load_shedding_summary is None  # both None -> stays None

    def test_merge_typed_summaries_self_preserved_when_both_set(self):
        """Typed summaries: self's value preserved when both self and other are set."""
        # Given
        report_a = DailyAutonomousReport()
        report_a.chaos_summary = ChaosReportSummary(grade="A")

        report_b = DailyAutonomousReport()
        report_b.chaos_summary = ChaosReportSummary(grade="F")

        # When
        report_a.merge(report_b)

        # Then — self wins, not overwritten
        assert report_a.chaos_summary.grade == "A"

    def test_merge_counters_still_additive(self):
        """Event-driven counters remain additive after restructure."""
        # Given
        report_a = DailyAutonomousReport()
        report_a.task_failures = 3
        report_a.dlq_new_entries_count = 5

        report_b = DailyAutonomousReport()
        report_b.task_failures = 2
        report_b.dlq_new_entries_count = 7

        # When
        report_a.merge(report_b)

        # Then
        assert report_a.task_failures == 5
        assert report_a.dlq_new_entries_count == 12


# =============================================================================
# DailyAutonomousReport summary fields — Contract Tests
# =============================================================================


class TestDailyReportSummaryFieldsContract:
    """Contract: three Optional summary fields exist with default None."""

    def test_summary_fields_default_to_none(self):
        """chaos_summary, load_shedding_summary, error_budget_summary all default None."""
        report = DailyAutonomousReport()
        assert report.chaos_summary is None
        assert report.load_shedding_summary is None
        assert report.error_budget_summary is None


# =============================================================================
# field_mapping — Contract Tests
# =============================================================================


class TestFieldMappingContract:
    """Contract: dlq_pending_count excluded from field_mapping (gauge -> snapshot)."""

    def test_dlq_pending_count_not_in_field_mapping(self):
        """dlq_pending_count is not updated by _update_counts_from_entry."""
        report = DailyAutonomousReport()
        entry = TaskResultEntry(
            task_name="test",
            result={"dlq_pending_count": 99},
            timestamp=datetime.now(UTC),
        )
        report.add_entry(entry)
        assert report.dlq_pending_count == 0

    def test_error_budget_entries_do_not_increment_task_failures(self):
        """EB gate entries have no error/success=False keys, so task_failures stays 0."""
        report = DailyAutonomousReport()
        entry = TaskResultEntry(
            task_name="error_budget_gate_blocked",
            result={
                "budget_percent": 95.0,
                "threshold_percent": 90.0,
                "tier_id": "critical",
                "region": "",
            },
            timestamp=datetime.now(UTC),
            severity="warning",
        )
        report.add_entry(entry)
        assert report.task_failures == 0
        assert report.critical_alerts == 0
