"""
Daily Report Data Models.

Contains data classes for task results and daily report aggregation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from baldur.core.serializable import SerializableMixin
from baldur.utils.time import utc_now


@dataclass
class TaskResultEntry:
    """Individual task result entry for aggregation."""

    task_name: str
    result: dict[str, Any]
    timestamp: datetime
    severity: str = "info"


# =========================================================================
# Typed section summaries (snapshot-collected, populated at report time)
# =========================================================================


@dataclass
class ChaosReportSummary(SerializableMixin):
    """Summary of daily chaos resilience report (XP3)."""

    grade: str = ""
    grade_trend: str = ""
    experiments_total: int = 0
    experiments_passed: int = 0
    experiments_failed: int = 0
    sla_breaches: int = 0
    error_budget_consumed_pct: float = 0.0


@dataclass
class LoadSheddingSummary(SerializableMixin):
    """Summary of load shedding statistics (XP5)."""

    dropped_total: int = 0
    dropped_by_tier: dict[str, int] = field(default_factory=dict)
    processed_total: int = 0
    processed_by_tier: dict[str, int] = field(default_factory=dict)
    level: str = ""


@dataclass
class ErrorBudgetGateSummary(SerializableMixin):
    """Summary of error budget gate blocking events (UU-E5)."""

    blocks: int = 0
    warnings: int = 0


@dataclass
class ShadowProSummary(SerializableMixin):
    """Shadow PRO insights derived from OSS observables (427 §4.6).

    Conservative, directional estimation to create PRO awareness.
    No PRO code imported, no PRO logic executed.
    """

    cb_trips_without_auto_degradation: int = 0
    dlq_captured_without_adaptive_replay: int = 0
    drift_warnings_manual_only: int = 0


# =========================================================================
# PRO Automated Actions (Phase 3, D7)
# =========================================================================


@dataclass
class AutomatedActionsSummary(SerializableMixin):
    """Summary of PRO automated actions executed during the day.

    Aggregated from entries with the corresponding task_name prefixes
    at report generation time. Entries are kept for the detail API view.
    """

    auto_replay_batches: int = 0
    auto_replay_recovered: int = 0
    auto_replay_failed: int = 0
    canary_completed: int = 0
    canary_rolled_back: int = 0
    auto_tuning_applied: int = 0
    emergency_level_changes: int = 0
    saga_completed: int = 0
    saga_compensated: int = 0
    governance_blocked: int = 0


# =========================================================================
# DLQ Pending Breakdown (Phase 5.3, D9)
# =========================================================================

# Copy of adapters/celery/integrations/dlq_recorder.py:_RECOMMENDED_ACTIONS
# for display use. Duplication accepted: consumers serve different purposes
# (recording vs display), dict is small and static, relocation would break
# DLQRecorder.get_recommended_action() + 4 tests (D9 trade-off).
_RECOMMENDED_ACTIONS: dict[str, str] = {
    "NETWORK_ERROR": "Wait for network recovery, then auto-replay",
    "TIMEOUT": "Increase timeout or retry with backoff",
    "CONNECTION_ERROR": "Check external service availability",
    "RATE_LIMITED": "Wait for rate limit window, then retry",
    "AUTH_ERROR": "Check credentials and permissions",
    "VALIDATION_ERROR": "Manual review required - data may be invalid",
    "EXTERNAL_SERVICE_ERROR": "Wait for external service recovery",
    "GATEWAY_ERROR": "Check gateway status, manual review may be needed",
    "UNKNOWN_ERROR": "Manual review recommended",
}


@dataclass
class DLQFailureTypeBreakdown(SerializableMixin):
    """Per-failure-type breakdown for a single pending DLQ bucket."""

    count: int = 0
    domains: list[str] = field(default_factory=list)
    action: str = ""


@dataclass
class DLQPendingBreakdown(SerializableMixin):
    """DLQ pending snapshot with domain and failure_type breakdown (D9).

    Populated from FailedOperationRepository.get_statistics() extension:
    - by_domain: {domain: pending_count}
    - by_failure_type: {failure_type: DLQFailureTypeBreakdown}

    Fail-open: repository unavailable → breakdown omitted, pending count
    still available via DailyAutonomousReport.dlq_pending_count.
    """

    total: int = 0
    by_domain: dict[str, int] = field(default_factory=dict)
    by_failure_type: dict[str, DLQFailureTypeBreakdown] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "by_domain": dict(self.by_domain),
            "by_failure_type": {
                ft: breakdown.to_dict()
                for ft, breakdown in self.by_failure_type.items()
            },
        }


@dataclass
class DailyAutonomousReport:
    """
    Daily autonomous operations summary report.

    Aggregates counts and statistics from all baldur tasks
    executed during the day. This is the main class for daily report aggregation.

    Contains counts and statistics from all baldur tasks
    executed during the day.
    """

    date: datetime = field(default_factory=lambda: utc_now())

    # Core counts
    archived_count: int = 0
    expired_count: int = 0
    purged_count: int = 0
    recovered_count: int = 0
    drift_warnings_count: int = 0

    # Circuit breaker stats
    circuit_transitions: int = 0
    circuits_opened: int = 0
    circuits_closed: int = 0

    # DLQ stats
    dlq_pending_count: int = 0
    dlq_new_entries_count: int = 0
    dlq_resolved_count: int = 0
    dlq_manual_resolutions: int = 0
    dlq_ttl_expired: int = 0
    dlq_max_retries_exhausted: int = 0

    # Error counts
    task_failures: int = 0
    critical_alerts: int = 0

    # Snapshot-collected summaries (populated at report generation time)
    chaos_summary: ChaosReportSummary | None = None
    load_shedding_summary: LoadSheddingSummary | None = None
    error_budget_summary: ErrorBudgetGateSummary | None = None
    shadow_pro_summary: ShadowProSummary | None = None
    automated_actions_summary: AutomatedActionsSummary | None = None
    dlq_pending_breakdown: DLQPendingBreakdown | None = None

    # Custom metrics (extensible)
    custom_counts: dict[str, int] = field(default_factory=dict)

    # Raw entries for detailed analysis
    entries: list[TaskResultEntry] = field(default_factory=list)

    def add_entry(self, entry: TaskResultEntry) -> None:
        """Add a task result entry and update counts."""
        self.entries.append(entry)
        self._update_counts_from_entry(entry)

    def _update_counts_from_entry(self, entry: TaskResultEntry) -> None:
        """Update aggregate counts based on entry result."""
        result = entry.result

        # Map result fields to our counts
        # Note: dlq_pending_count excluded — gauge collected via snapshot, not event-driven
        field_mapping = {
            "archived_count": "archived_count",
            "expired_count": "expired_count",
            "purged_count": "purged_count",
            "recovered_count": "recovered_count",
            "drift_warnings_count": "drift_warnings_count",
            "circuit_transitions": "circuit_transitions",
            "circuits_opened": "circuits_opened",
            "circuits_closed": "circuits_closed",
            "dlq_new_entries_count": "dlq_new_entries_count",
            "dlq_resolved_count": "dlq_resolved_count",
            "dlq_manual_resolutions": "dlq_manual_resolutions",
            "dlq_ttl_expired": "dlq_ttl_expired",
            "dlq_max_retries_exhausted": "dlq_max_retries_exhausted",
        }

        for result_field, attr_name in field_mapping.items():
            if result_field in result:
                current = getattr(self, attr_name)
                setattr(self, attr_name, current + int(result[result_field]))

        # Track failures
        if result.get("error") or result.get("success") is False:
            self.task_failures += 1

        # Track critical alerts
        if entry.severity == "critical":
            self.critical_alerts += 1

    def merge(self, other: DailyAutonomousReport) -> None:
        """Merge another report's data into this one."""
        # --- Event-driven counters (additive) ---
        self.archived_count += other.archived_count
        self.expired_count += other.expired_count
        self.purged_count += other.purged_count
        self.recovered_count += other.recovered_count
        self.drift_warnings_count += other.drift_warnings_count
        self.circuit_transitions += other.circuit_transitions
        self.circuits_opened += other.circuits_opened
        self.circuits_closed += other.circuits_closed
        self.dlq_new_entries_count += other.dlq_new_entries_count
        self.dlq_resolved_count += other.dlq_resolved_count
        self.dlq_manual_resolutions += other.dlq_manual_resolutions
        self.dlq_ttl_expired += other.dlq_ttl_expired
        self.dlq_max_retries_exhausted += other.dlq_max_retries_exhausted
        self.task_failures += other.task_failures
        self.critical_alerts += other.critical_alerts

        # --- Snapshot gauges (latest wins, overwritten by _collect_snapshots) ---
        self.dlq_pending_count = max(self.dlq_pending_count, other.dlq_pending_count)

        # --- Typed summaries (non-None wins, single-source snapshot) ---
        for attr in (
            "chaos_summary",
            "load_shedding_summary",
            "error_budget_summary",
            "shadow_pro_summary",
            "automated_actions_summary",
            "dlq_pending_breakdown",
        ):
            if getattr(other, attr) and not getattr(self, attr):
                setattr(self, attr, getattr(other, attr))

        # --- Collections ---
        for key, value in other.custom_counts.items():
            self.custom_counts[key] = self.custom_counts.get(key, 0) + value
        self.entries.extend(other.entries)

    def to_dict(self, include_entries: bool = False) -> dict[str, Any]:
        """Convert to dictionary for serialization.

        Args:
            include_entries: When True, serialize individual entries.
                Default False keeps notification payloads small; persistence
                (D4) passes True so detail queries can access per-entry
                service/domain/failure_type context after cache TTL expires.
        """
        result: dict[str, Any] = {
            "date": self.date.isoformat(),
            "archived_count": self.archived_count,
            "expired_count": self.expired_count,
            "purged_count": self.purged_count,
            "recovered_count": self.recovered_count,
            "drift_warnings_count": self.drift_warnings_count,
            "circuit_transitions": self.circuit_transitions,
            "circuits_opened": self.circuits_opened,
            "circuits_closed": self.circuits_closed,
            # DLQ stats (D8)
            "dlq_pending_count": self.dlq_pending_count,
            "dlq_new_entries_count": self.dlq_new_entries_count,
            "dlq_resolved_count": self.dlq_resolved_count,
            "dlq_manual_resolutions": self.dlq_manual_resolutions,
            "dlq_ttl_expired": self.dlq_ttl_expired,
            "dlq_max_retries_exhausted": self.dlq_max_retries_exhausted,
            "task_failures": self.task_failures,
            "critical_alerts": self.critical_alerts,
            "custom_counts": self.custom_counts,
            "entry_count": len(self.entries),
        }
        # Typed summaries (conditional)
        if self.chaos_summary:
            result["chaos_summary"] = self.chaos_summary.to_dict()
        if self.load_shedding_summary:
            result["load_shedding_summary"] = self.load_shedding_summary.to_dict()
        if self.error_budget_summary:
            result["error_budget_summary"] = self.error_budget_summary.to_dict()
        if self.shadow_pro_summary:
            result["shadow_pro_summary"] = self.shadow_pro_summary.to_dict()
        if self.automated_actions_summary:
            result["automated_actions_summary"] = (
                self.automated_actions_summary.to_dict()
            )
        if self.dlq_pending_breakdown:
            result["dlq_pending_breakdown"] = self.dlq_pending_breakdown.to_dict()
        if include_entries:
            result["entries"] = [
                {
                    "task_name": e.task_name,
                    "result": e.result,
                    "timestamp": e.timestamp.isoformat(),
                    "severity": e.severity,
                }
                for e in self.entries
            ]
        return result


# Backward compatibility alias
DailyReportData = DailyAutonomousReport


__all__ = [
    "TaskResultEntry",
    "ChaosReportSummary",
    "LoadSheddingSummary",
    "ErrorBudgetGateSummary",
    "ShadowProSummary",
    "AutomatedActionsSummary",
    "DLQFailureTypeBreakdown",
    "DLQPendingBreakdown",
    "DailyAutonomousReport",
    "DailyReportData",
]
