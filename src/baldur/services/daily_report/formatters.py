"""
Report Formatting for Various Channels.

Provides formatting functions for Slack and PagerDuty output.
"""

from __future__ import annotations

from .models import DailyAutonomousReport

# Section -> release tier. Single source for the Slack/PagerDuty tier-guard
# (the formatter suppression below), the concept-guide section->tier table
# (docs/concepts/foundations/daily-report.md), and the parity test. A section
# graduates simply by editing its row here in the feature's own promotion plan.
# Tiers confirmed against V1_LAUNCH_MANIFEST.yaml + the PRO launch strategy:
# chaos, error-budget-gate, auto-tuning, saga, and load-shedding are held out
# of the v1.0 launch set, so they are suppressed from the digest until they
# graduate.
_SECTION_TIER: dict[str, str] = {
    "auto_processing": "oss",
    "alerts": "oss",
    "circuit_breaker": "oss",
    "errors": "oss",
    "custom": "oss",
    "shadow_pro": "oss",
    "dlq": "oss",
    "automated_actions": "oss",
    "auto_replay": "oss",
    "canary": "v1.0",
    "emergency": "v1.0",
    "governance": "v1.0",
    "chaos": "deferred",
    "load_shedding": "deferred",
    "error_budget": "deferred",
    "auto_tuning": "deferred",
    "saga": "deferred",
}

# Release tiers that ship in the v1.0 digest. A section mapped to any other
# tier (e.g. "deferred") is suppressed from Slack delivery and the PagerDuty
# actionable/critical gate so no unshipped-feature section reaches an operator.
_SHIPPED_TIERS = frozenset({"oss", "v1.0"})


def _is_shipped(section: str) -> bool:
    """Whether a report section's producing-feature tier ships in the v1.0 digest.

    Unknown keys default to shipped (the OSS render-on-data-presence posture);
    the ``_SECTION_TIER`` <-> guide parity test keeps the map exhaustive, so the
    default is never silently relied on for a real section.

    Args:
        section: A ``_SECTION_TIER`` key naming a report section.

    Returns:
        True if the section's tier is in ``_SHIPPED_TIERS``.
    """
    return _SECTION_TIER.get(section, "oss") in _SHIPPED_TIERS


def format_report_for_slack(report: DailyAutonomousReport) -> str:  # noqa: C901, PLR0912, PLR0915
    """
    Format report as Slack message with adaptive sections (D10).

    Core summary line is always shown (confirms system is running).
    Detail sections expand only when their counts > 0 (reduce noise).

    Sections whose producing feature is not in the v1.0 shipped set
    (``_SECTION_TIER`` -> ``_SHIPPED_TIERS``) are suppressed, so an operator who
    opts into a Deferred feature still sees its data on the CLI / API / console
    but not in the Slack digest.

    Args:
        report: DailyAutonomousReport instance

    Returns:
        Formatted Slack message string
    """
    total_operations = (
        report.archived_count
        + report.expired_count
        + report.purged_count
        + report.recovered_count
    )

    status_emoji = "✅" if report.task_failures == 0 else "⚠️"

    # Core summary line (always shown)
    lines = [
        f"{status_emoji} *Autonomous Daily Report* ({report.date:%Y-%m-%d})",
    ]

    # Build the detail sections first (D10). Each section appends its lines to
    # `detail`; the quiet-day short-circuit then keys off whether `detail` is
    # empty — so a section added later is counted by construction, with no
    # separate condition chain to keep in sync.
    detail: list[str] = []

    # Auto-Processing Summary (conditional — any count > 0, D10)
    if _is_shipped("auto_processing") and (
        report.archived_count
        or report.expired_count
        or report.purged_count
        or report.recovered_count
    ):
        detail.extend(
            [
                "",
                "*📊 Auto-Processing Summary*",
                f"• Archived: {report.archived_count} / Expired: {report.expired_count} / Purged: {report.purged_count} / Recovered: {report.recovered_count}",
            ]
        )

    # Alerts (conditional — drift warnings > 0, D10)
    if _is_shipped("alerts") and report.drift_warnings_count > 0:
        detail.extend(
            [
                "",
                "*🔔 Alerts*",
                f"• Drift warnings: {report.drift_warnings_count}",
            ]
        )

    # Circuit breaker stats if any
    if _is_shipped("circuit_breaker") and report.circuit_transitions > 0:
        detail.extend(
            [
                "",
                "*⚡ Circuit Breaker*",
                f"• Transitions: {report.circuit_transitions} (Open: {report.circuits_opened} / Close: {report.circuits_closed})",
            ]
        )

    # DLQ section (D9 — with failure_type breakdown and recommended action)
    if _is_shipped("dlq") and (
        report.dlq_new_entries_count > 0
        or report.dlq_resolved_count > 0
        or report.dlq_pending_count > 0
    ):
        detail.extend(
            [
                "",
                f"📬 DLQ: {report.dlq_new_entries_count} new / {report.dlq_resolved_count} auto-resolved / {report.dlq_pending_count} pending",
            ]
        )
        if (
            report.dlq_manual_resolutions
            or report.dlq_ttl_expired
            or report.dlq_max_retries_exhausted
        ):
            detail.append(
                f"  └ Manual {report.dlq_manual_resolutions} / TTL expired {report.dlq_ttl_expired} / Max retries {report.dlq_max_retries_exhausted}"
            )
        # Per-failure-type breakdown with action guidance (D9)
        if (
            report.dlq_pending_breakdown
            and report.dlq_pending_breakdown.by_failure_type
        ):
            detail.append("  Needs attention:")
            for ft, bd in report.dlq_pending_breakdown.by_failure_type.items():
                domains_str = ", ".join(bd.domains) if bd.domains else "unknown"
                detail.append(f"  • {domains_str}: {bd.count} {ft} — {bd.action}")

    # Chaos section
    if _is_shipped("chaos") and report.chaos_summary:
        chaos = report.chaos_summary
        detail.extend(
            [
                "",
                "*🧪 Chaos Resilience*",
                f"• Grade: {chaos.grade} ({chaos.grade_trend}) | Experiments: {chaos.experiments_passed}/{chaos.experiments_total} passed",
            ]
        )
        if chaos.sla_breaches > 0 or chaos.error_budget_consumed_pct > 0:
            detail.append(
                f"• SLA breaches: {chaos.sla_breaches} | Error budget consumed: {chaos.error_budget_consumed_pct:.1f}%"
            )

    # Load shedding section
    if _is_shipped("load_shedding") and report.load_shedding_summary:
        shed = report.load_shedding_summary
        detail.extend(
            [
                "",
                "*🛡️ Load Shedding*",
                f"• Level: {shed.level} | Dropped: {shed.dropped_total} / Processed: {shed.processed_total}",
            ]
        )
        if shed.dropped_by_tier:
            tier_parts = [f"{k}: {v}" for k, v in shed.dropped_by_tier.items() if v > 0]
            if tier_parts:
                detail.append(f"  └ By tier: {' / '.join(tier_parts)}")

    # Error budget section
    if _is_shipped("error_budget") and report.error_budget_summary:
        eb = report.error_budget_summary
        detail.extend(
            [
                "",
                f"*🚦 Error Budget Gate* — {eb.blocks} blocks, {eb.warnings} warnings",
            ]
        )

    # Automated Actions section (Phase 3.2, D7) — conditional; omitted for OSS.
    # Shipped action lines are accumulated first; the heading is emitted only if
    # at least one shipped line exists, so a day with only Deferred-tier actions
    # (saga / auto-tuning) yields no empty heading (D5).
    if report.automated_actions_summary:
        auto = report.automated_actions_summary
        action_lines: list[str] = []
        if _is_shipped("auto_replay") and auto.auto_replay_batches > 0:
            action_lines.append(
                f"• Auto-replay: {auto.auto_replay_batches} batches, "
                f"{auto.auto_replay_recovered} recovered / {auto.auto_replay_failed} failed"
            )
        if _is_shipped("canary") and (
            auto.canary_completed > 0 or auto.canary_rolled_back > 0
        ):
            action_lines.append(
                f"• Canary: {auto.canary_completed} completed / {auto.canary_rolled_back} rolled back"
            )
        if _is_shipped("auto_tuning") and auto.auto_tuning_applied > 0:
            action_lines.append(f"• Auto-tuning applied: {auto.auto_tuning_applied}")
        if _is_shipped("emergency") and auto.emergency_level_changes > 0:
            action_lines.append(
                f"• Emergency level changes: {auto.emergency_level_changes}"
            )
        if _is_shipped("saga") and (
            auto.saga_completed > 0 or auto.saga_compensated > 0
        ):
            action_lines.append(
                f"• Saga: {auto.saga_completed} completed / {auto.saga_compensated} compensated"
            )
        if _is_shipped("governance") and auto.governance_blocked > 0:
            action_lines.append(f"• Governance blocked: {auto.governance_blocked}")

        if action_lines:
            detail.append("")
            detail.append("*🤖 Automated Actions*")
            detail.extend(action_lines)

    # Error summary if any
    if _is_shipped("errors") and (
        report.task_failures > 0 or report.critical_alerts > 0
    ):
        detail.extend(
            [
                "",
                "*❌ Errors*",
                f"• Task failures: {report.task_failures}",
                f"• Critical alerts: {report.critical_alerts}",
            ]
        )

    # Custom metrics (operator-defined counters). Accumulate the non-zero
    # lines first and emit the heading only when at least one exists — without
    # its own heading the bullets would visually attach to the preceding
    # section (mirrors the Automated Actions accumulate-then-emit pattern).
    if _is_shipped("custom"):
        custom_lines = [
            f"• {key}: {value}"
            for key, value in report.custom_counts.items()
            if value > 0
        ]
        if custom_lines:
            detail.append("")
            detail.append("*📦 Custom Metrics*")
            detail.extend(custom_lines)

    # Shadow PRO insights (427 §4.6, visibility policy: impl 452)
    if _is_shipped("shadow_pro") and report.shadow_pro_summary:
        shadow = report.shadow_pro_summary
        detail.extend(["", "*💡 PRO Insights*"])
        if shadow.cb_trips_without_auto_degradation > 0:
            detail.append(
                f"• {shadow.cb_trips_without_auto_degradation} CB trips without auto-degradation"
            )
        if shadow.dlq_captured_without_adaptive_replay > 0:
            detail.append(
                f"• {shadow.dlq_captured_without_adaptive_replay} operations captured in the DLQ, "
                "replayed at a fixed batch size (no adaptive sizing or throttled replay queue)"
            )
        if shadow.drift_warnings_manual_only > 0:
            detail.append(
                f"• {shadow.drift_warnings_manual_only} drift warnings, manual resolution only"
            )
        detail.append(
            "_To adjust frequency: "
            "BALDUR_DAILY_REPORT_SHADOW_PRO_MODE=auto|daily|weekly|off_"
        )

    # Quiet-day short-circuit: no processing, no alerts, no incidents — i.e.
    # no section contributed any line.
    if not detail:
        lines.append("📊 All quiet — 0 processed, 0 alerts")
        return "\n".join(lines)

    lines.extend(detail)

    # Summary
    lines.extend(
        [
            "",
            f"📈 *Total processed: {total_operations}* | Task executions: {len(report.entries)}",
        ]
    )

    return "\n".join(lines)


def format_report_for_pagerduty(report: DailyAutonomousReport) -> str:
    """Format daily report for PagerDuty — actionable items only, English.

    DLQ stats intentionally excluded from summary — not a trigger condition.
    DLQ data is available in PD custom_details via report.to_dict() -> metadata.

    Deferred-tier actionable lines (chaos / error-budget / load-shedding) are
    suppressed via ``_is_shipped`` so the page only carries shipped signals,
    matching the ``_has_actionable_items`` gate (D5 PagerDuty parity).
    """
    items = []
    if (
        _is_shipped("error_budget")
        and report.error_budget_summary
        and report.error_budget_summary.blocks > 0
    ):
        items.append(f"ErrorBudget: {report.error_budget_summary.blocks} blocks")
    if (
        _is_shipped("chaos")
        and report.chaos_summary
        and report.chaos_summary.grade in ("D", "F")
    ):
        chaos = report.chaos_summary
        items.append(
            f"Chaos grade {chaos.grade} ({chaos.experiments_failed}/{chaos.experiments_total} failed)"
        )
    if (
        _is_shipped("load_shedding")
        and report.load_shedding_summary
        and report.load_shedding_summary.level in ("high", "critical")
    ):
        shed = report.load_shedding_summary
        items.append(f"LoadShedding {shed.level}: {shed.dropped_total} dropped")
    if report.critical_alerts > 0:
        items.append(f"{report.critical_alerts} critical alerts")
    if report.task_failures >= 5:
        items.append(f"{report.task_failures} task failures")
    return " | ".join(items)


__all__ = [
    "format_report_for_slack",
    "format_report_for_pagerduty",
]
