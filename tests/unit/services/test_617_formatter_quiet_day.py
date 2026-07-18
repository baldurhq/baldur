"""Unit tests for 617 D5 — Slack formatter quiet-day short-circuit.

D5 rebuilt ``format_report_for_slack`` to accumulate detail-section lines into
a local list and key the quiet-day short-circuit off whether that list is
empty — deleting the 15-condition OR chain that previously mirrored the section
conditions and could silently drop an unlisted future section.

These boundary tests pin the contract the restructure must preserve: an
all-zero report shows the quiet-day line, and any single *shipped* section
minimally present defeats the short-circuit (its content renders, the quiet
line does not). Because the new short-circuit is "did any section append a
line", a future section is counted by construction — so this suite also guards
against a regression to a hardcoded condition chain.

660 D5 added a tier guard: a Deferred-tier summary (chaos / load-shedding /
error-budget) is suppressed from the digest, and an all-zero automated-actions
summary emits no action lines, so a report whose only content is one of those
contributes no shipped line and the quiet-day short-circuit still fires.
"""

from __future__ import annotations

import pytest

from baldur.services.daily_report.formatters import format_report_for_slack
from baldur.services.daily_report.models import (
    AutomatedActionsSummary,
    ChaosReportSummary,
    DailyAutonomousReport,
    ErrorBudgetGateSummary,
    LoadSheddingSummary,
    ShadowProSummary,
)

# The quiet-day short-circuit line emitted when no section contributed detail.
_QUIET_MARKER = "All quiet"


def _mut(name, value):
    """Build a (id, setattr-mutation) pair for a scalar report field."""
    return name, (lambda r: setattr(r, name, value))


# Each entry minimally activates exactly one *shipped* detail section, which
# contributes a line and so defeats the quiet-day short-circuit.
_SHIPPED_SECTION_TRIGGERS = [
    _mut("archived_count", 1),
    _mut("expired_count", 1),
    _mut("purged_count", 1),
    _mut("recovered_count", 1),
    _mut("drift_warnings_count", 1),
    _mut("circuit_transitions", 1),
    _mut("dlq_new_entries_count", 1),
    _mut("dlq_resolved_count", 1),
    _mut("dlq_pending_count", 1),
    _mut("task_failures", 1),
    _mut("critical_alerts", 1),
    ("custom_counts", lambda r: r.custom_counts.__setitem__("widgets", 1)),
    (
        "shadow_pro_summary",
        lambda r: setattr(r, "shadow_pro_summary", ShadowProSummary()),
    ),
]

# Sections whose minimal presence contributes no shipped line: Deferred-tier
# summaries are suppressed (660 D5) and an all-zero automated-actions summary
# emits no action lines — so the quiet-day short-circuit still fires.
_QUIET_PRESERVING_TRIGGERS = [
    ("chaos_summary", lambda r: setattr(r, "chaos_summary", ChaosReportSummary())),
    (
        "load_shedding_summary",
        lambda r: setattr(r, "load_shedding_summary", LoadSheddingSummary()),
    ),
    (
        "error_budget_summary",
        lambda r: setattr(r, "error_budget_summary", ErrorBudgetGateSummary()),
    ),
    (
        "automated_actions_summary",
        lambda r: setattr(r, "automated_actions_summary", AutomatedActionsSummary()),
    ),
]


class TestFormatterQuietDayBoundary:
    """Quiet-day short-circuit boundary: empty vs single-section-present."""

    def test_empty_report_renders_quiet_day_line(self):
        """An all-zero report (no section contributes) shows the quiet-day line."""
        result = format_report_for_slack(DailyAutonomousReport())

        assert _QUIET_MARKER in result

    @pytest.mark.parametrize(
        "trigger",
        [pair[1] for pair in _SHIPPED_SECTION_TRIGGERS],
        ids=[pair[0] for pair in _SHIPPED_SECTION_TRIGGERS],
    )
    def test_single_shipped_section_defeats_quiet_day_short_circuit(self, trigger):
        """Any single shipped section minimally present suppresses the quiet-day line."""
        # Given — an otherwise-empty report with exactly one section activated
        report = DailyAutonomousReport()
        trigger(report)

        # When
        result = format_report_for_slack(report)

        # Then — the quiet-day short-circuit did not fire
        assert _QUIET_MARKER not in result

    @pytest.mark.parametrize(
        "trigger",
        [pair[1] for pair in _QUIET_PRESERVING_TRIGGERS],
        ids=[pair[0] for pair in _QUIET_PRESERVING_TRIGGERS],
    )
    def test_suppressed_or_empty_section_stays_quiet(self, trigger):
        """A Deferred-tier summary (suppressed) or an all-zero automated-actions
        summary contributes no shipped line, so quiet-day still fires (660 D5)."""
        # Given — an otherwise-empty report with one suppressed/empty section
        report = DailyAutonomousReport()
        trigger(report)

        # When
        result = format_report_for_slack(report)

        # Then — no shipped line was appended, so the short-circuit fired
        assert _QUIET_MARKER in result

    def test_custom_count_of_zero_stays_quiet(self):
        """A custom metric of 0 contributes no line — quiet-day still fires.

        Boundary just below the section emitter's ``value > 0`` condition.
        """
        report = DailyAutonomousReport()
        report.custom_counts["widgets"] = 0

        result = format_report_for_slack(report)

        assert _QUIET_MARKER in result
