"""
SLA Drift Detection Tasks

Task functions for detecting drift between configured SLA thresholds
and actual recovery performance metrics.

Core Principle: "System provides data, humans make decisions."
These tasks ONLY generate warnings - they NEVER auto-adjust settings.

NOTE: This module provides task functions that can be registered with
any task queue system (Celery, RQ, etc.). The actual task registration
is done in the framework-specific adapter layer.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from typing import Any, Protocol

import structlog

from baldur.core.timezone import now
from baldur.notification.helpers import notify_sla

logger = structlog.get_logger()


# =============================================================================
# Protocols (Framework-agnostic interfaces)
# =============================================================================


class FailedOperationQuerySet(Protocol):
    """Protocol for queryset-like objects."""

    def filter(self, **kwargs) -> FailedOperationQuerySet: ...

    def count(self) -> int: ...

    def order_by(self, *args) -> FailedOperationQuerySet: ...

    def __iter__(self): ...

    def __getitem__(self, key): ...


class DriftDetectionOperationProtocol(Protocol):
    """Protocol for FailedOperation-like objects."""

    id: Any
    domain: str
    status: str
    created_at: Any
    resolved_at: Any | None
    metadata: dict[str, Any] | None

    def save(self, update_fields: list[str] | None = None) -> None: ...


class SLAThresholdsProtocol(Protocol):
    """Protocol for SLA thresholds configuration."""

    def get_all_thresholds(self) -> dict[str, timedelta]: ...


# =============================================================================
# SLA Drift Detection
# =============================================================================


class SLADriftDetector:
    """
    Detects drift between configured SLA thresholds and actual performance.

    This is a framework-agnostic implementation that can be used with
    any data access layer.
    """

    def __init__(
        self,
        get_sla_thresholds: Callable[[], SLAThresholdsProtocol],
        get_failed_operations: Callable[..., FailedOperationQuerySet],
        record_sla_breach: Callable[[str], None] | None = None,
    ):
        """
        Initialize detector with required dependencies.

        Args:
            get_sla_thresholds: Function to get SLA thresholds config
            get_failed_operations: Function to query failed operations
            record_sla_breach: Optional function to record metrics
        """
        self.get_sla_thresholds = get_sla_thresholds
        self.get_failed_operations = get_failed_operations
        self.record_sla_breach = record_sla_breach

    def check_drift(self) -> dict[str, Any]:
        """
        Compare configured SLA thresholds with actual recovery metrics.

        Returns:
            Dictionary with drift detection results
        """
        logger.info("drift_detection.sla_check_started")

        try:
            sla_config = self.get_sla_thresholds()
            all_thresholds = sla_config.get_all_thresholds()

            results: dict[str, Any] = {
                "success": True,
                "checked_at": now().isoformat(),
                "domains_checked": [],
                "warnings": [],
                "metrics": {},
            }

            # Time windows for analysis - looked up from Settings
            current_time = now()
            analysis_window = timedelta(hours=self._get_analysis_window_hours())
            window_start = current_time - analysis_window

            for domain, sla_threshold in all_thresholds.items():
                domain_result = self._analyze_domain_sla(
                    domain=domain,
                    sla_threshold=sla_threshold,
                    window_start=window_start,
                    current_time=current_time,
                )

                results["domains_checked"].append(domain)
                results["metrics"][domain] = domain_result["metrics"]

                if domain_result["warning"]:
                    results["warnings"].append(domain_result["warning"])
                    logger.warning(
                        "sla.drift_warning",
                        domain_result=domain_result["warning"]["message"],
                    )

            if results["warnings"]:
                self._send_drift_notifications(results["warnings"])
                logger.warning(
                    "sla.drift_completed_warning",
                    warnings_count=len(results["warnings"]),
                )
            else:
                logger.info("drift_detection.sla_check_no_violations")

            return results

        except Exception as e:
            logger.exception(
                "sla.drift_error_during",
                error=e,
            )
            return {
                "success": False,
                "error": str(e),
                "checked_at": now().isoformat(),
            }

    @staticmethod
    def _get_analysis_window_hours() -> int:
        """Look up analysis_window_hours from Settings."""
        try:
            from baldur.settings.drift_detection import (
                get_drift_detection_settings,
            )

            return get_drift_detection_settings().analysis_window_hours
        except Exception:
            return 24  # default

    def _analyze_domain_sla(  # noqa: C901
        self,
        domain: str,
        sla_threshold: timedelta,
        window_start,
        current_time,
    ) -> dict[str, Any]:
        """Analyze SLA performance for a specific domain."""
        sla_seconds = sla_threshold.total_seconds()

        # Query resolved operations in the window
        resolved_ops = self.get_failed_operations(
            domain=domain,
            status__in=["resolved", "rejected"],
            resolved_at__isnull=False,
            resolved_at__gte=window_start,
        )

        total_resolved = resolved_ops.count()

        if total_resolved == 0:
            return {
                "metrics": {
                    "total_resolved": 0,
                    "avg_recovery_seconds": None,
                    "max_recovery_seconds": None,
                    "sla_threshold_seconds": sla_seconds,
                    "sla_breach_count": 0,
                    "sla_breach_rate": 0.0,
                },
                "warning": None,
            }

        # Calculate recovery times
        recovery_stats = []
        sla_breaches = 0

        for op in resolved_ops:
            if op.resolved_at and op.created_at:
                recovery_seconds = (op.resolved_at - op.created_at).total_seconds()
                recovery_stats.append(recovery_seconds)
                if recovery_seconds > sla_seconds:
                    sla_breaches += 1

        if not recovery_stats:
            return {
                "metrics": {
                    "total_resolved": total_resolved,
                    "avg_recovery_seconds": None,
                    "max_recovery_seconds": None,
                    "sla_threshold_seconds": sla_seconds,
                    "sla_breach_count": 0,
                    "sla_breach_rate": 0.0,
                },
                "warning": None,
            }

        avg_recovery = sum(recovery_stats) / len(recovery_stats)
        max_recovery = max(recovery_stats)
        breach_rate = (sla_breaches / total_resolved) * 100

        # Count pending items approaching SLA breach
        pending_ops = self.get_failed_operations(
            domain=domain,
            status="pending",
        )

        pending_at_risk = 0
        for op in pending_ops:
            age = (current_time - op.created_at).total_seconds()
            if age > sla_seconds * 0.8:
                pending_at_risk += 1

        metrics = {
            "total_resolved": total_resolved,
            "avg_recovery_seconds": round(avg_recovery, 2),
            "max_recovery_seconds": round(max_recovery, 2),
            "sla_threshold_seconds": sla_seconds,
            "sla_breach_count": sla_breaches,
            "sla_breach_rate": round(breach_rate, 2),
            "pending_at_risk": pending_at_risk,
        }

        # Generate warning if needed
        warning = None

        if breach_rate > 10:
            warning = {
                "type": "SLA_BREACH_RATE_HIGH",
                "domain": domain,
                "severity": "critical" if breach_rate > 25 else "warning",
                "message": (
                    f"[{domain}] SLA breach rate is {breach_rate:.1f}%. "
                    f"(threshold: 10%) Configuration review is required."
                ),
                "metrics": metrics,
                "recommendation": (
                    "The configured SLA is hard to meet at the current recovery rate. "
                    "Consider adjusting the SLA or improving the recovery process. "
                    "[ACTION REQUIRED: operator review needed]"
                ),
            }
        elif avg_recovery > sla_seconds * 0.8:
            warning = {
                "type": "SLA_APPROACHING_LIMIT",
                "domain": domain,
                "severity": "warning",
                "message": (
                    f"[{domain}] Average recovery time ({avg_recovery / 3600:.2f}h) "
                    f"exceeded 80% of the SLA ({sla_seconds / 3600:.1f}h)."
                ),
                "metrics": metrics,
                "recommendation": (
                    "The likelihood of an SLA breach is increasing. "
                    "Consider proactive action. "
                    "[ACTION REQUIRED: operator review needed]"
                ),
            }
        elif pending_at_risk > 5:
            warning = {
                "type": "PENDING_ITEMS_AT_RISK",
                "domain": domain,
                "severity": "warning",
                "message": (
                    f"[{domain}] {pending_at_risk} items are at risk of SLA breach. "
                    f"(80% or more of the SLA consumed)"
                ),
                "metrics": metrics,
                "recommendation": (
                    "Many PENDING items are close to SLA expiry. "
                    "Immediate review is required. "
                    "[ACTION REQUIRED: operator review needed]"
                ),
            }

        return {
            "metrics": metrics,
            "warning": warning,
        }

    def _send_drift_notifications(self, warnings: list[dict]) -> None:
        """Send notifications for SLA drift warnings."""
        for warning in warnings:
            domain = warning.get("domain", "unknown")
            severity = warning.get("severity", "warning")
            warning_type = warning.get("type", "SLA_DRIFT")

            # Record SLA breach metric if applicable
            if self.record_sla_breach and warning_type == "SLA_BREACH_RATE_HIGH":
                self.record_sla_breach(domain)

            # Log warning
            logger.warning(
                "sla_drift_warning.event",
                healing_domain=domain,
                warning_type=warning_type,
                severity=severity,
                warning=warning.get("message"),
                recommendation=warning.get("recommendation"),
            )

            # Send notification via unified notification manager
            try:
                notify_sla(
                    title=f"[SLA Drift] {domain}",
                    message=warning.get("message", ""),
                    domain=domain,
                    priority=severity,
                    source="drift_detection",
                    metadata={
                        "type": warning_type,
                        "domain": domain,
                        "recommendation": warning.get("recommendation", ""),
                        **warning.get("metrics", {}),
                    },
                )
            except Exception as e:
                logger.exception(
                    "sla_drift_warning.send_notification_failed",
                    error=e,
                )

        # Batch push aggregate counter to daily report (once per detection run).
        # Individual warning detail already captured by unified_notification entries.
        if warnings:
            try:
                from baldur.services.daily_report import get_daily_report_collector

                get_daily_report_collector().add_result(
                    task_name="sla.drift_warning",
                    result={"drift_warnings_count": len(warnings)},
                )
            except Exception:
                pass


# =============================================================================
# Chaos Experiment Cleanup
# =============================================================================


class ChaosExperimentCleaner:
    """Cleans up expired chaos experiments."""

    def __init__(
        self,
        resolve_expired_experiments: Callable[[], int],
    ):
        """
        Initialize cleaner with dependencies.

        Args:
            resolve_expired_experiments: Function to resolve expired experiments
        """
        self.resolve_expired_experiments = resolve_expired_experiments

    def cleanup(self) -> dict[str, Any]:
        """
        Clean up expired chaos experiments.

        Returns:
            Dictionary with cleanup results
        """
        logger.info("chaos_cleanup.starting_expired_chaos_experiment")

        try:
            resolved_count = self.resolve_expired_experiments()

            logger.info(
                "chaos_cleanup.completed_resolved_expired_experiments",
                resolved_count=resolved_count,
            )

            return {
                "success": True,
                "cleaned_at": now().isoformat(),
                "resolved_count": resolved_count,
            }

        except Exception as e:
            logger.exception(
                "chaos_cleanup.error_during_cleanup",
                error=e,
            )
            return {
                "success": False,
                "error": str(e),
                "cleaned_at": now().isoformat(),
            }


# =============================================================================
# Decision Recording
# =============================================================================


class DecisionRecorder:
    """Records human decisions made based on forensic advisories."""

    def __init__(
        self,
        get_failed_operation: Callable[[int], DriftDetectionOperationProtocol],
    ):
        """
        Initialize recorder with dependencies.

        Args:
            get_failed_operation: Function to get operation by ID
        """
        self.get_failed_operation = get_failed_operation

    def record(
        self,
        operation_id: int,
        decision: str,
        decided_by: str,
        notes: str = "",
    ) -> dict[str, Any]:
        """
        Record a human decision.

        Args:
            operation_id: ID of the FailedOperation
            decision: Decision made (approved_replay, rejected, escalated)
            decided_by: Username or ID of decision maker
            notes: Additional notes

        Returns:
            Dictionary with recording result
        """
        logger.info(
            "decision_record.recording_decision_operation",
            operation_id=operation_id,
            decision=decision,
            decided_by=decided_by,
        )

        try:
            operation = self.get_failed_operation(operation_id)

            advisory = (
                operation.metadata.get("forensic_advisory", {})
                if operation.metadata
                else {}
            )

            decision_record = {
                "decided_at": now().isoformat(),
                "decided_by": decided_by,
                "decision": decision,
                "notes": notes,
                "advisory_at_decision": advisory.get("analyzed_at", ""),
                "advisory_recommendation": advisory.get("recommended_action", ""),
                "advisory_confidence": advisory.get("confidence", 0),
            }

            if operation.metadata is None:
                operation.metadata = {}

            if "decision_records" not in operation.metadata:
                operation.metadata["decision_records"] = []

            operation.metadata["decision_records"].append(decision_record)
            operation.save(update_fields=["metadata", "updated_at"])

            logger.info(
                "decision_record.recorded",
                operation_id=operation_id,
                decision=decision,
                decided_by=decided_by,
                advisory=advisory.get("recommended_action", "N/A"),
            )

            return {
                "success": True,
                "operation_id": operation_id,
                "decision_record": decision_record,
            }

        except Exception as e:
            logger.exception(
                "decision_record.error_recording_decision",
                error=e,
            )
            return {
                "success": False,
                "error": str(e),
            }
