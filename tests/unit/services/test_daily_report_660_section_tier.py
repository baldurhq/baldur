"""
Unit tests for #660 — daily-report section tier-honesty.

Test targets (``src/baldur/services/daily_report/formatters.py``):
  - ``_SECTION_TIER`` / ``_SHIPPED_TIERS`` / ``_is_shipped`` — the single-source
    tier map driving the Slack/PagerDuty suppression (D5).
  - ``format_report_for_slack`` — Deferred-tier sections (whole sections + the
    saga / auto-tuning sub-lines) are suppressed; the *Automated Actions*
    heading emits only when >= 1 shipped action line exists.

The map-level whole-section suppression of chaos / load-shedding / error-budget
is also exercised here as a systematic parametrized sweep; the per-section
render assertions for those live next door in ``test_daily_report_412_formatters.py``.
"""

from __future__ import annotations

import pytest

from baldur.services.daily_report.formatters import (
    _SECTION_TIER,
    _SHIPPED_TIERS,
    _is_shipped,
    format_report_for_slack,
)
from baldur.services.daily_report.models import (
    AutomatedActionsSummary,
    ChaosReportSummary,
    DailyAutonomousReport,
    ErrorBudgetGateSummary,
    LoadSheddingSummary,
)

# Design-doc D5 map (impl 660). Hardcoded here so the Contract test pins the
# exact tier classification the formatter/guide/parity triad agrees on.
_OSS_KEYS = frozenset(
    {"auto_processing", "alerts", "circuit_breaker", "errors", "custom", "shadow_pro"}
)
_V1_KEYS = frozenset(
    {"dlq", "automated_actions", "auto_replay", "canary", "emergency", "governance"}
)
_DEFERRED_KEYS = frozenset(
    {"chaos", "load_shedding", "error_budget", "auto_tuning", "saga"}
)


# =============================================================================
# _SECTION_TIER / _SHIPPED_TIERS / _is_shipped — Contract Tests
# =============================================================================


class TestSectionTierMapContract:
    """Pin the single-source tier map's keys, values, and membership."""

    def test_section_tier_keys_match_documented_set(self):
        """The map keys are exactly the documented v1.0 section set (D5)."""
        assert set(_SECTION_TIER) == _OSS_KEYS | _V1_KEYS | _DEFERRED_KEYS

    def test_shipped_tiers_exact_membership(self):
        """Shipped tiers are exactly {oss, v1.0} — Deferred ships nothing."""
        assert _SHIPPED_TIERS == frozenset({"oss", "v1.0"})

    @pytest.mark.parametrize("section", sorted(_OSS_KEYS))
    def test_oss_sections_tagged_oss(self, section):
        assert _SECTION_TIER[section] == "oss"

    @pytest.mark.parametrize("section", sorted(_V1_KEYS))
    def test_v1_sections_tagged_v1(self, section):
        assert _SECTION_TIER[section] == "v1.0"

    @pytest.mark.parametrize("section", sorted(_DEFERRED_KEYS))
    def test_deferred_sections_tagged_deferred(self, section):
        assert _SECTION_TIER[section] == "deferred"

    @pytest.mark.parametrize("section", sorted(_OSS_KEYS | _V1_KEYS))
    def test_is_shipped_true_for_shipped_keys(self, section):
        assert _is_shipped(section) is True

    @pytest.mark.parametrize("section", sorted(_DEFERRED_KEYS))
    def test_is_shipped_false_for_deferred_keys(self, section):
        assert _is_shipped(section) is False

    def test_is_shipped_unknown_key_defaults_shipped(self):
        """Unknown keys default to shipped (the OSS render-on-presence posture)."""
        assert _is_shipped("a_section_not_in_the_map") is True


# =============================================================================
# format_report_for_slack — Deferred suppression Behavior Tests (map-driven)
# =============================================================================


def _report() -> DailyAutonomousReport:
    return DailyAutonomousReport()


class TestDailyReport660SectionTier:
    """Deferred sections/sub-lines never reach the Slack digest (D5)."""

    @pytest.mark.parametrize(
        ("section", "configure", "absent_marker"),
        [
            (
                "chaos",
                lambda r: setattr(
                    r, "chaos_summary", ChaosReportSummary(grade="A", grade_trend="up")
                ),
                "Chaos",
            ),
            (
                "load_shedding",
                lambda r: setattr(
                    r,
                    "load_shedding_summary",
                    LoadSheddingSummary(level="high", dropped_total=10),
                ),
                "Load Shedding",
            ),
            (
                "error_budget",
                lambda r: setattr(
                    r, "error_budget_summary", ErrorBudgetGateSummary(blocks=4)
                ),
                "Error Budget",
            ),
        ],
    )
    def test_deferred_whole_section_suppressed(self, section, configure, absent_marker):
        """Each Deferred whole-section stays out of the Slack digest."""
        # Given: a report carrying only a Deferred-tier section's data
        report = _report()
        configure(report)
        assert _SECTION_TIER[section] == "deferred"  # guard: still Deferred

        # When
        result = format_report_for_slack(report)

        # Then: the section heading never renders
        assert absent_marker not in result

    def test_saga_subline_suppressed_when_only_deferred_action(self):
        """A saga-only day yields no Saga line and no Automated Actions heading."""
        report = _report()
        report.automated_actions_summary = AutomatedActionsSummary(
            saga_completed=3, saga_compensated=1
        )

        result = format_report_for_slack(report)

        assert "Saga" not in result
        assert "Automated Actions" not in result

    def test_auto_tuning_subline_suppressed_when_only_deferred_action(self):
        """An auto-tuning-only day yields no tuning line and no heading."""
        report = _report()
        report.automated_actions_summary = AutomatedActionsSummary(
            auto_tuning_applied=7
        )

        result = format_report_for_slack(report)

        assert "Auto-tuning" not in result
        assert "Automated Actions" not in result

    @pytest.mark.parametrize(
        ("configure", "present_marker"),
        [
            (
                lambda s: setattr(s, "auto_replay_batches", 2),
                "Auto-replay",
            ),
            (
                lambda s: setattr(s, "canary_completed", 1),
                "Canary",
            ),
            (
                lambda s: setattr(s, "emergency_level_changes", 1),
                "Emergency level changes",
            ),
            (
                lambda s: setattr(s, "governance_blocked", 4),
                "Governance blocked",
            ),
        ],
    )
    def test_shipped_action_line_renders_under_heading(self, configure, present_marker):
        """Each shipped automated-action renders its line and the heading."""
        report = _report()
        summary = AutomatedActionsSummary()
        configure(summary)
        report.automated_actions_summary = summary

        result = format_report_for_slack(report)

        assert "Automated Actions" in result
        assert present_marker in result

    def test_heading_emits_with_shipped_line_even_when_deferred_present(self):
        """Mixed day: heading + shipped line render; Deferred sub-line stays out."""
        report = _report()
        report.automated_actions_summary = AutomatedActionsSummary(
            auto_replay_batches=2,
            auto_replay_recovered=18,
            saga_completed=9,  # Deferred — suppressed
        )

        result = format_report_for_slack(report)

        assert "Automated Actions" in result
        assert "Auto-replay" in result
        assert "Saga" not in result

    def test_deferred_only_actions_collapse_to_quiet_day(self):
        """A day of only Deferred actions (saga + tuning) collapses to All quiet."""
        report = _report()
        report.automated_actions_summary = AutomatedActionsSummary(
            saga_completed=3, auto_tuning_applied=5
        )

        result = format_report_for_slack(report)

        assert "Automated Actions" not in result
        assert "All quiet" in result

    def test_shipped_section_still_renders_alongside_suppressed_deferred(self):
        """Sanity: a shipped section (DLQ) renders while Deferred chaos is hidden."""
        report = _report()
        report.dlq_pending_count = 5
        report.chaos_summary = ChaosReportSummary(grade="F", grade_trend="down")

        result = format_report_for_slack(report)

        assert "DLQ" in result
        assert "Chaos" not in result
