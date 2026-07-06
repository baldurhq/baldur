"""
Unit tests for 428 Daily Report — Model & formatter additions.

Test targets:
  - DailyAutonomousReport.to_dict(include_entries=...) — new include_entries flag.
  - AutomatedActionsSummary — new dataclass for Phase 3 (D7).
  - DLQPendingBreakdown / DLQFailureTypeBreakdown — new dataclasses for D9.
  - DailyAutonomousReport.merge() — new field summary precedence (non-None wins).
  - format_report_for_slack() — quiet-day short-circuit, Automated Actions section,
    DLQ per-failure_type breakdown rendering (D9/D10).
"""

from __future__ import annotations

from datetime import UTC, datetime

from baldur.services.daily_report.formatters import format_report_for_slack
from baldur.services.daily_report.models import (
    AutomatedActionsSummary,
    DailyAutonomousReport,
    DLQFailureTypeBreakdown,
    DLQPendingBreakdown,
    TaskResultEntry,
)

# =============================================================================
# AutomatedActionsSummary — Contract Tests
# =============================================================================


class TestAutomatedActionsSummaryContract:
    """Design-spec default values and serialization keys."""

    def test_default_values_all_zero(self):
        """All 10 counter fields default to 0."""
        s = AutomatedActionsSummary()
        assert s.auto_replay_batches == 0
        assert s.auto_replay_recovered == 0
        assert s.auto_replay_failed == 0
        assert s.canary_completed == 0
        assert s.canary_rolled_back == 0
        assert s.auto_tuning_applied == 0
        assert s.emergency_level_changes == 0
        assert s.saga_completed == 0
        assert s.saga_compensated == 0
        assert s.governance_blocked == 0

    def test_to_dict_contains_all_ten_fields(self):
        """to_dict() exposes all 10 counters."""
        s = AutomatedActionsSummary(auto_replay_batches=2)
        d = s.to_dict()
        expected_keys = {
            "auto_replay_batches",
            "auto_replay_recovered",
            "auto_replay_failed",
            "canary_completed",
            "canary_rolled_back",
            "auto_tuning_applied",
            "emergency_level_changes",
            "saga_completed",
            "saga_compensated",
            "governance_blocked",
        }
        assert set(d.keys()) == expected_keys


# =============================================================================
# DLQ breakdown dataclasses — Contract Tests
# =============================================================================


class TestDLQFailureTypeBreakdownContract:
    """Default values and serialization keys for per-failure-type buckets."""

    def test_default_values(self):
        """count=0, domains=[], action=''."""
        bd = DLQFailureTypeBreakdown()
        assert bd.count == 0
        assert bd.domains == []
        assert bd.action == ""

    def test_to_dict_fields(self):
        """to_dict() exposes count, domains, action."""
        bd = DLQFailureTypeBreakdown(count=3, domains=["payment"], action="retry")
        d = bd.to_dict()
        assert set(d.keys()) == {"count", "domains", "action"}
        assert d["count"] == 3
        assert d["domains"] == ["payment"]
        assert d["action"] == "retry"


class TestDLQPendingBreakdownContract:
    """Default values, nested to_dict, and structural shape."""

    def test_default_values(self):
        """Defaults: total=0, by_domain={}, by_failure_type={}."""
        bd = DLQPendingBreakdown()
        assert bd.total == 0
        assert bd.by_domain == {}
        assert bd.by_failure_type == {}

    def test_to_dict_serializes_nested_failure_type_dict(self):
        """by_failure_type values are recursively serialized via to_dict()."""
        bd = DLQPendingBreakdown(
            total=5,
            by_domain={"payment": 5},
            by_failure_type={
                "TIMEOUT": DLQFailureTypeBreakdown(
                    count=5, domains=["payment"], action="retry"
                ),
            },
        )
        d = bd.to_dict()
        assert d["total"] == 5
        assert d["by_domain"] == {"payment": 5}
        # Nested DLQFailureTypeBreakdown is dict, not object
        assert d["by_failure_type"]["TIMEOUT"] == {
            "count": 5,
            "domains": ["payment"],
            "action": "retry",
        }


# =============================================================================
# DailyAutonomousReport.to_dict(include_entries=...) — Behavior Tests
# =============================================================================


class TestToDictIncludeEntriesBehavior:
    """Behavior tests for the new include_entries flag (D4)."""

    def test_include_entries_false_by_default_omits_entries_key(self):
        """Default include_entries=False -> no 'entries' key in output."""
        report = DailyAutonomousReport()
        report.entries.append(
            TaskResultEntry(
                task_name="test",
                result={"x": 1},
                timestamp=datetime(2026, 4, 10, tzinfo=UTC),
            )
        )

        d = report.to_dict()

        assert "entries" not in d
        # entry_count is still included as a summary field
        assert d["entry_count"] == 1

    def test_include_entries_true_emits_entry_list(self):
        """include_entries=True -> 'entries' list with per-entry detail."""
        report = DailyAutonomousReport()
        ts = datetime(2026, 4, 10, 12, 0, tzinfo=UTC)
        report.entries.append(
            TaskResultEntry(
                task_name="dlq_item_created",
                result={"domain": "payment", "failure_type": "TIMEOUT"},
                timestamp=ts,
                severity="warning",
            )
        )

        d = report.to_dict(include_entries=True)

        assert "entries" in d
        assert len(d["entries"]) == 1
        entry = d["entries"][0]
        assert entry["task_name"] == "dlq_item_created"
        assert entry["result"] == {"domain": "payment", "failure_type": "TIMEOUT"}
        assert entry["timestamp"] == ts.isoformat()
        assert entry["severity"] == "warning"

    def test_automated_actions_summary_included_conditionally(self):
        """automated_actions_summary key emitted only when non-None."""
        report = DailyAutonomousReport()
        assert "automated_actions_summary" not in report.to_dict()

        report.automated_actions_summary = AutomatedActionsSummary(saga_completed=1)
        d = report.to_dict()
        assert "automated_actions_summary" in d
        assert d["automated_actions_summary"]["saga_completed"] == 1

    def test_dlq_pending_breakdown_included_conditionally(self):
        """dlq_pending_breakdown key emitted only when non-None."""
        report = DailyAutonomousReport()
        assert "dlq_pending_breakdown" not in report.to_dict()

        report.dlq_pending_breakdown = DLQPendingBreakdown(
            total=1,
            by_domain={"payment": 1},
            by_failure_type={
                "TIMEOUT": DLQFailureTypeBreakdown(count=1, domains=["payment"]),
            },
        )
        d = report.to_dict()
        assert "dlq_pending_breakdown" in d
        assert d["dlq_pending_breakdown"]["total"] == 1


# =============================================================================
# DailyAutonomousReport.merge() — Behavior Tests for new summaries
# =============================================================================


class TestMergeNewSummariesBehavior:
    """merge() behavior for automated_actions_summary and dlq_pending_breakdown."""

    def test_merge_propagates_automated_actions_from_other_when_self_none(self):
        """other.automated_actions_summary set, self.None -> self adopts other's."""
        a = DailyAutonomousReport()
        b = DailyAutonomousReport()
        b.automated_actions_summary = AutomatedActionsSummary(saga_completed=2)

        a.merge(b)

        assert a.automated_actions_summary is not None
        assert a.automated_actions_summary.saga_completed == 2

    def test_merge_keeps_self_automated_actions_when_both_set(self):
        """Both sides have summary -> self wins (non-None wins, not additive)."""
        a = DailyAutonomousReport()
        a.automated_actions_summary = AutomatedActionsSummary(saga_completed=5)
        b = DailyAutonomousReport()
        b.automated_actions_summary = AutomatedActionsSummary(saga_completed=99)

        a.merge(b)

        assert a.automated_actions_summary.saga_completed == 5

    def test_merge_propagates_dlq_pending_breakdown(self):
        """other.dlq_pending_breakdown set, self.None -> self adopts other's."""
        a = DailyAutonomousReport()
        b = DailyAutonomousReport()
        b.dlq_pending_breakdown = DLQPendingBreakdown(
            total=10, by_domain={"payment": 10}
        )

        a.merge(b)

        assert a.dlq_pending_breakdown is not None
        assert a.dlq_pending_breakdown.total == 10


# =============================================================================
# format_report_for_slack() — Quiet-day and new sections
# =============================================================================


class TestSlackFormatterQuietDayBehavior:
    """Quiet-day short-circuit logic: all zero + all summaries None."""

    def test_empty_report_renders_quiet_day_message(self):
        """Report with nothing to report collapses to one-line quiet message."""
        report = DailyAutonomousReport()

        result = format_report_for_slack(report)

        assert "All quiet" in result
        # Quiet-day output has no additional sections
        assert "Auto-Processing Summary" not in result
        assert "Circuit Breaker" not in result
        assert "DLQ" not in result

    def test_quiet_day_broken_by_any_count(self):
        """Any non-zero count breaks quiet-day short-circuit."""
        report = DailyAutonomousReport()
        report.archived_count = 1

        result = format_report_for_slack(report)

        assert "All quiet" not in result

    def test_quiet_day_when_only_deferred_summary(self):
        """A summary with only Deferred-tier actions (saga) is suppressed (D5),
        so the day still collapses to quiet-day with no Automated Actions heading."""
        report = DailyAutonomousReport()
        report.automated_actions_summary = AutomatedActionsSummary(saga_completed=1)

        result = format_report_for_slack(report)

        assert "All quiet" in result
        assert "Automated Actions" not in result

    def test_quiet_day_broken_by_shipped_summary(self):
        """A summary with a shipped action (auto-replay) renders the heading and
        defeats quiet-day."""
        report = DailyAutonomousReport()
        report.automated_actions_summary = AutomatedActionsSummary(
            auto_replay_batches=1
        )

        result = format_report_for_slack(report)

        assert "All quiet" not in result
        assert "Automated Actions" in result

    def test_quiet_day_broken_by_custom_metric(self):
        """custom_counts with a non-zero value breaks quiet-day and renders
        under its own ``Custom Metrics`` heading (not attached to Errors)."""
        report = DailyAutonomousReport()
        report.custom_counts = {"mymetric": 1}

        result = format_report_for_slack(report)

        assert "All quiet" not in result
        assert "*📦 Custom Metrics*" in result
        assert "• mymetric: 1" in result

    def test_custom_metrics_heading_omitted_when_all_zero(self):
        """All-zero custom_counts emit no line, so the heading is not rendered
        (accumulate-then-emit guard mirrors the Automated Actions section)."""
        report = DailyAutonomousReport()
        report.archived_count = 1  # break quiet-day so other sections render
        report.custom_counts = {"mymetric": 0}

        result = format_report_for_slack(report)

        assert "Custom Metrics" not in result


class TestSlackFormatterAutomatedActionsSectionBehavior:
    """Render Automated Actions section conditionally."""

    def test_section_omitted_when_summary_none(self):
        """automated_actions_summary=None -> section not in output."""
        report = DailyAutonomousReport()
        report.archived_count = 1  # Break quiet-day short-circuit

        result = format_report_for_slack(report)

        assert "Automated Actions" not in result

    def test_section_rendered_when_any_action_present(self):
        """Non-zero action counter -> section with heading rendered."""
        report = DailyAutonomousReport()
        report.automated_actions_summary = AutomatedActionsSummary(
            auto_replay_batches=1,
            auto_replay_recovered=4,
            auto_replay_failed=1,
        )

        result = format_report_for_slack(report)

        assert "Automated Actions" in result
        assert "Auto-replay" in result
        assert "1 batches" in result
        assert "4 recovered" in result
        assert "1 failed" in result

    def test_section_canary_line_shown_for_completed_or_rolled_back(self):
        """Canary line shown if either canary_completed or canary_rolled_back > 0."""
        report = DailyAutonomousReport()
        report.automated_actions_summary = AutomatedActionsSummary(
            canary_completed=2, canary_rolled_back=1
        )

        result = format_report_for_slack(report)

        assert "Canary: 2 completed / 1 rolled back" in result

    def test_section_governance_line_shown_when_blocked(self):
        """governance_blocked > 0 -> dedicated line shown."""
        report = DailyAutonomousReport()
        report.automated_actions_summary = AutomatedActionsSummary(governance_blocked=3)

        result = format_report_for_slack(report)

        assert "Governance blocked: 3" in result


class TestSlackFormatterDLQBreakdownBehavior:
    """Render per-failure-type DLQ breakdown under DLQ section (D9)."""

    def test_breakdown_block_shown_when_by_failure_type_populated(self):
        """Per-failure-type lines included with domain + count + action."""
        report = DailyAutonomousReport()
        report.dlq_pending_count = 4
        report.dlq_pending_breakdown = DLQPendingBreakdown(
            total=4,
            by_domain={"payment": 4},
            by_failure_type={
                "TIMEOUT": DLQFailureTypeBreakdown(
                    count=4,
                    domains=["payment"],
                    action="Increase timeout or retry with backoff",
                ),
            },
        )

        result = format_report_for_slack(report)

        assert "Needs attention:" in result
        assert "payment: 4 TIMEOUT" in result
        assert "Increase timeout or retry with backoff" in result

    def test_breakdown_block_omitted_when_by_failure_type_empty(self):
        """Empty by_failure_type -> 'Needs attention' block skipped."""
        report = DailyAutonomousReport()
        report.dlq_pending_count = 2
        report.dlq_pending_breakdown = DLQPendingBreakdown(total=2, by_domain={"a": 2})

        result = format_report_for_slack(report)

        assert "Needs attention:" not in result

    def test_breakdown_renders_unknown_domain_placeholder(self):
        """Empty domains list renders 'unknown' in output."""
        report = DailyAutonomousReport()
        report.dlq_pending_count = 1
        report.dlq_pending_breakdown = DLQPendingBreakdown(
            total=1,
            by_domain={"": 1},
            by_failure_type={
                "UNKNOWN_ERROR": DLQFailureTypeBreakdown(
                    count=1, domains=[], action="Manual review recommended"
                ),
            },
        )

        result = format_report_for_slack(report)

        assert "unknown: 1 UNKNOWN_ERROR" in result
