"""
🧠 Intelligence Lane (Analyze & Learn) Celery Tasks

Autonomous operations intelligence-lane tasks.

Tasks:
1. CheckSLADriftTask - SLA drift detection and warning
2. AnalyzeForensicPendingTask - forensic analysis of long-pending items

AnalyzeCrossStageInsightsTask moved to baldur_dormant.services.learning.tasks
(599 D10 - the learning feature relocated to the private distribution).
"""

from __future__ import annotations

from typing import Any

import structlog

from baldur.tasks.base import BaseNotifyingTask
from baldur.tasks.notification_policy import (
    NotificationPolicy,
    NotificationTiming,
)

logger = structlog.get_logger()


# =============================================================================
# Task 1: Check SLA Drift (migrated from existing)
# =============================================================================


class CheckSLADriftTask(BaseNotifyingTask):
    """
    SLA drift detection and warning.

    Analyzes the gap between configured SLA thresholds and actual recovery performance.

    Schedule: hourly
    Queue: analysis
    Notification: immediately on warning (REALTIME)

    Returns:
        dict: {
            "success": bool,
            "warnings_count": int,
            "warnings": list,
            "metrics": dict,
        }
    """

    name = "baldur.check_sla_drift"

    @property
    def notification_policy(self) -> NotificationPolicy:  # type: ignore[override]
        """Dynamically build notification_policy from Settings."""
        settings = self._get_intelligence_settings()
        return NotificationPolicy(
            timing=NotificationTiming.REALTIME,
            threshold=1,  # only notify when there is at least 1 warning
            threshold_field="warnings_count",
            default_severity="warning",
            cooldown_seconds=settings.default_cooldown_seconds,
        )

    @staticmethod
    def _get_intelligence_settings():
        """Look up IntelligenceTaskSettings."""
        try:
            from baldur.settings.intelligence_task import (
                get_intelligence_task_settings,
            )

            return get_intelligence_task_settings()
        except Exception:
            # return a temporary object
            class _FallbackSettings:
                default_cooldown_seconds = 3600
                execution_threshold = 10
                analysis_threshold_minutes = 60
                batch_size = 100
                severity_high_threshold = 50
                severity_medium_threshold = 10
                reconciliation_cutoff_minutes = 30
                insight_threshold = 3
                recovery_check_cooldown_seconds = 120

            return _FallbackSettings()

    def run(self) -> dict[str, Any]:
        """Run the SLA drift detection task."""
        logger.info("check_sla_drift.starting_sla_drift_detection")

        try:
            # DI helpers from celery_tasks layer
            from baldur.celery_tasks.drift_detection_tasks import (
                _get_failed_operations_factory,
                _get_sla_thresholds,
                _record_sla_breach,
            )
            from baldur.tasks.drift_detection import SLADriftDetector

            detector = SLADriftDetector(
                get_sla_thresholds=_get_sla_thresholds,
                get_failed_operations=_get_failed_operations_factory(),
                record_sla_breach=_record_sla_breach,
            )
            result = detector.check_drift()

            warnings = result.get("warnings", [])

            logger.info(
                "check_sla_drift.completed_warning",
                warnings_count=len(warnings),
            )

            return {
                "success": result.get("success", True),
                "warnings_count": len(warnings),
                "warnings": warnings,
                "metrics": result.get("metrics", {}),
            }

        except Exception as e:
            logger.exception(
                "check_sla_drift.failed",
                error=e,
            )
            return {
                "success": False,
                "error": str(e),
                "warnings_count": 0,
            }

    def _get_severity(self, result: dict[str, Any]) -> str:
        """Determine severity based on the warning count."""
        count = result.get("warnings_count", 0)
        if count >= 5:
            return "critical"
        if count >= 1:
            return "warning"
        return "info"

    def _get_summary_message(self, result: dict[str, Any]) -> str:
        """Build the notification message."""
        if result.get("error"):
            return f"❌ SLA drift detection failed: {result['error']}"

        count = result.get("warnings_count", 0)
        if count == 0:
            return "✅ No SLA drift - all metrics normal"

        return f"⚠️ SLA drift detected: {count} warning(s)"


# =============================================================================
# Task 2: Analyze Forensic Pending (P1)
# =============================================================================


class AnalyzeForensicPendingTask(BaseNotifyingTask):
    """
    Forensic analysis of long-pending items.

    Analyzes items that linger in the DLQ for a long time and provides patterns
    and recommended actions.

    Schedule: every 30 minutes
    Queue: analysis
    Notification: immediately when 10+ suspicious items (REALTIME)

    Args:
        threshold_minutes: analysis cutoff time (uses the Settings default if None)

    Returns:
        dict: {
            "success": bool,
            "suspicious_count": int,
            "stuck_patterns": list,
            "recommendations": list,
        }
    """

    name = "baldur.analyze_forensic_pending"

    @property
    def notification_policy(self) -> NotificationPolicy:  # type: ignore[override]
        """Dynamically build notification_policy from Settings."""
        settings = CheckSLADriftTask._get_intelligence_settings()
        return NotificationPolicy(
            timing=NotificationTiming.REALTIME,
            threshold=settings.execution_threshold,  # only when 10+
            threshold_field="suspicious_count",
            default_severity="warning",
            cooldown_seconds=settings.default_cooldown_seconds,
        )

    def run(self, threshold_minutes: int | None = None) -> dict[str, Any]:
        """Run the forensic analysis task."""
        settings = CheckSLADriftTask._get_intelligence_settings()
        if threshold_minutes is None:
            threshold_minutes = settings.analysis_threshold_minutes

        logger.info(
            "analyze_forensic_pending.starting_analysis_items_pending",
            threshold_minutes=threshold_minutes,
        )

        try:
            from baldur.factory.registry import ProviderRegistry

            dlq_service = ProviderRegistry.dlq_service.safe_get()
            if dlq_service is None:
                raise RuntimeError("baldur_pro DLQService not registered")
            pending_entries = dlq_service.get_pending_entries(
                limit=settings.batch_size,
            )

            # Classify pending entries by status/action
            results_by_action: dict[str, int] = {}
            for entry in pending_entries:
                action = getattr(entry, "status", "unknown") or "unknown"
                results_by_action[action] = results_by_action.get(action, 0) + 1

            raw_result = {
                "success": True,
                "analyzed_count": len(pending_entries),
                "results_by_action": results_by_action,
            }

            analyzed_count = raw_result.get("analyzed_count", 0)
            results_by_action_obj = raw_result.get("results_by_action", {})
            results_by_action = (
                results_by_action_obj if isinstance(results_by_action_obj, dict) else {}
            )

            # count suspicious items (requires_review or stuck)
            suspicious_count = (
                results_by_action.get("requires_review", 0)
                + results_by_action.get("stuck", 0)
                + results_by_action.get("unknown", 0)
            )

            # extract patterns
            stuck_patterns = self._extract_patterns(results_by_action)

            # build recommendations
            recommendations = self._generate_recommendations(
                results_by_action, suspicious_count
            )

            logger.info(
                "analyze_forensic_pending.completed",
                analyzed_count=analyzed_count,
                suspicious_count=suspicious_count,
            )

            return {
                "success": True,
                "analyzed_count": analyzed_count,
                "suspicious_count": suspicious_count,
                "stuck_patterns": stuck_patterns,
                "recommendations": recommendations,
                "results_by_action": results_by_action,
            }

        except Exception as e:
            logger.exception(
                "analyze_forensic_pending.failed",
                error=e,
            )
            return {
                "success": False,
                "error": str(e),
                "suspicious_count": 0,
            }

    def _extract_patterns(self, results_by_action: dict[str, int]) -> list:
        """Extract patterns from the results."""
        patterns = []
        for action, count in results_by_action.items():
            if count > 0:
                patterns.append(
                    {
                        "action": action,
                        "count": count,
                        "percentage": 0,  # ratio vs total (needs calculation)
                    }
                )
        return patterns

    def _generate_recommendations(
        self, results_by_action: dict[str, int], suspicious_count: int
    ) -> list:
        """Build recommendations."""
        recommendations = []

        if results_by_action.get("stuck", 0) > 5:
            recommendations.append("Many stuck items found - manual review recommended")

        if results_by_action.get("requires_review", 0) > 10:
            recommendations.append(
                "Many items require review - check the DLQ dashboard"
            )

        if suspicious_count > 20:
            recommendations.append(
                "Surge in suspicious items - system health check recommended"
            )

        return recommendations

    def _get_severity(self, result: dict[str, Any]) -> str:
        """Determine severity based on the suspicious item count."""
        settings = CheckSLADriftTask._get_intelligence_settings()
        count = result.get("suspicious_count", 0)
        if count >= settings.severity_high_threshold:
            return "critical"
        if count >= settings.severity_medium_threshold:
            return "warning"
        return "info"

    def _get_summary_message(self, result: dict[str, Any]) -> str:
        """Build the notification message."""
        if result.get("error"):
            return f"❌ Forensic analysis failed: {result['error']}"

        patterns_count = len(result.get("stuck_patterns", []))

        return (
            f"🔍 Forensic analysis result\n"
            f"• Suspicious items: {result['suspicious_count']}\n"
            f"• Patterns: {patterns_count} found"
        )


# =============================================================================
# Task 4: Check Recovery Transitions (notification added to existing task)
# =============================================================================


class CheckRecoveryTransitionsTask(BaseNotifyingTask):
    """
    Circuit Breaker recovery status check.

    Detects CB state changes and sends a notification when recovery completes.

    Schedule: every 2 minutes
    Queue: realtime
    Notification: immediately on state change (REALTIME)

    Returns:
        dict: {
            "success": bool,
            "transitions_count": int,
            "circuits_recovered": list,
        }
    """

    name = "baldur.check_recovery_transitions"

    @property
    def notification_policy(self) -> NotificationPolicy:  # type: ignore[override]
        """Dynamically build notification_policy from Settings."""
        settings = CheckSLADriftTask._get_intelligence_settings()
        return NotificationPolicy(
            timing=NotificationTiming.REALTIME,
            threshold=1,  # only when there is at least 1 change
            threshold_field="transitions_count",
            default_severity="info",
            cooldown_seconds=settings.recovery_check_cooldown_seconds,
        )

    def run(self) -> dict[str, Any]:
        """Run the recovery status check task."""
        logger.info("check_recovery_transitions.checking_circuit_breaker_states")

        try:
            from baldur.services import get_circuit_breaker_service

            service = get_circuit_breaker_service()
            cb_result = service.check_recovery_transitions()

            # Map CB service result to task result format
            transitioned = cb_result.get("transitioned", [])
            result = {
                "success": cb_result.get("success", True),
                "transitions_count": cb_result.get("count", 0),
                "circuits_recovered": transitioned,
            }

            transitions = result.get("transitions_count", 0)
            recovered = result.get("circuits_recovered", [])

            logger.info(
                "check_recovery_transitions.completed",
                transitions=transitions,
                recovered_count=len(recovered),
            )

            return {
                "success": True,
                "transitions_count": transitions,
                "circuits_recovered": recovered,
            }

        except Exception as e:
            logger.exception(
                "check_recovery_transitions.failed",
                error=e,
            )
            return {
                "success": False,
                "error": str(e),
                "transitions_count": 0,
            }

    def _get_severity(self, result: dict[str, Any]) -> str:
        """Determine severity based on state."""
        recovered = result.get("circuits_recovered", [])
        if len(recovered) > 0:
            return "info"  # recovery is good news
        return "info"

    def _get_summary_message(self, result: dict[str, Any]) -> str:
        """Build the notification message."""
        if result.get("error"):
            return f"❌ Recovery status check failed: {result['error']}"

        recovered = result.get("circuits_recovered", [])
        if len(recovered) > 0:
            circuits = ", ".join(recovered[:3])
            if len(recovered) > 3:
                circuits += f" +{len(recovered) - 3} more"
            return f"✅ Circuit Breaker recovered: {circuits}"

        return f"ℹ️ Circuit Breaker status change(s): {result['transitions_count']}"


# =============================================================================
# Task 5: Verify Reconciliation Accuracy
# =============================================================================


class VerifyReconciliationAccuracyTask(BaseNotifyingTask):
    """
    Shadow Budget estimate accuracy verification.

    Compares against the actual error count 30 minutes after approval/rejection
    and records the estimate accuracy.

    Schedule: every 5 minutes (piggybacks on Beat)
    Queue: analysis

    Returns:
        dict: {
            "success": bool,
            "verified_count": int,
            "high_variance_count": int,
        }
    """

    name = "baldur.verify_reconciliation_accuracy"

    notification_policy = NotificationPolicy(
        timing=NotificationTiming.AGGREGATED,  # included in the daily summary
        threshold=0,  # always run (log only, notification optional)
        cooldown_seconds=0,
    )

    def run(self) -> dict[str, Any]:
        """Process Shadow Budgets awaiting verification."""
        logger.info("verify_reconciliation_accuracy.starting_accuracy_verification")

        try:
            from datetime import timedelta

            from baldur.utils.time import utc_now as get_now

            try:
                from baldur_pro.services.error_budget.reconciliation import (
                    get_reconciliation_service,
                )
            except ImportError:
                get_reconciliation_service = None  # type: ignore[assignment,misc]

            service = get_reconciliation_service()
            verified_count = 0
            high_variance_count = 0

            # filter items past the approval/rejection cutoff (looked up from Settings)
            settings = CheckSLADriftTask._get_intelligence_settings()
            cutoff = get_now() - timedelta(
                minutes=settings.reconciliation_cutoff_minutes
            )

            for shadow in service.get_all_shadow_budgets():
                # skip already-verified items
                if shadow.verified_at:
                    continue

                # confirm that the cutoff minutes have elapsed after approval/rejection
                if shadow.reviewed_at and shadow.reviewed_at < cutoff:
                    variance = self._verify_accuracy(shadow, service)
                    verified_count += 1

                    # 10%+ variance is notable
                    if variance and variance > 10.0:
                        high_variance_count += 1

            logger.info(
                "verify_reconciliation_accuracy.completed",
                verified_count=verified_count,
                high_variance_count=high_variance_count,
            )

            return {
                "success": True,
                "verified_count": verified_count,
                "high_variance_count": high_variance_count,
            }

        except Exception as e:
            logger.exception(
                "verify_reconciliation_accuracy.failed",
                error=e,
            )
            return {
                "success": False,
                "error": str(e),
                "verified_count": 0,
            }

    def _verify_accuracy(self, shadow, service) -> float | None:
        """
        Verify the accuracy of a single Shadow Budget.

        Returns:
            variance_percent, or None (on verification failure)
        """
        from datetime import timedelta

        from baldur.utils.time import utc_now as get_now

        try:
            # look up the actual error count (Prometheus or DLQ)
            actual_errors = self._get_actual_errors(
                start=shadow.failsafe_period_end,
                end=shadow.failsafe_period_end + timedelta(minutes=30),
            )

            # compute the variance
            if shadow.estimated_errors > 0:
                variance_percent = abs(
                    (shadow.estimated_errors - actual_errors)
                    / shadow.estimated_errors
                    * 100
                )
            else:
                variance_percent = 0.0 if actual_errors == 0 else 100.0

            # update the model
            shadow.verified_at = get_now()
            shadow.accuracy_variance_percent = variance_percent

            # audit record
            self._record_accuracy_audit(shadow, actual_errors, variance_percent)

            logger.debug(
                "verify_reconciliation_accuracy.verified",
                shadow=shadow.calculation_id,
                estimated_errors=shadow.estimated_errors,
                actual_errors=actual_errors,
                variance_percent=variance_percent,
            )

            return float(variance_percent)

        except Exception as e:
            logger.warning(
                "verify_reconciliation_accuracy.verify_failed",
                shadow=shadow.calculation_id,
                error=e,
            )
            return None

    def _get_actual_errors(self, start, end) -> int:
        """
        Look up the actual error count for the given window.

        Uses the DLQ's time-filtered entry count as the windowed source. The
        in-process ``prometheus_adapter.query_error_count`` is intentionally NOT
        consulted here: it reads the local registry's all-time cumulative
        counter and ignores ``start``/``end`` (the in-process registry cannot
        answer a windowed query), so feeding it into this 30-minute variance
        would be strictly worse than the bounded DLQ count. A genuinely windowed
        Prometheus source needs a remote range query (deferred follow-up).
        """
        try:
            # DLQ time-filtered entry count (windowed actual source)
            from baldur.factory.registry import ProviderRegistry

            dlq_service = ProviderRegistry.dlq_service.safe_get()
            if dlq_service is None:
                raise RuntimeError("baldur_pro DLQService not registered")
            entries = dlq_service.query_entries(
                start_time=start,
                end_time=end,
            )
            return len(entries) if entries else 0
        except Exception:
            pass

        # no data source
        return 0

    def _record_accuracy_audit(
        self,
        shadow,
        actual_errors: int,
        variance_percent: float,
    ) -> None:
        """Record the accuracy verification result in the Audit."""
        try:
            from baldur.audit.continuous_audit import (
                get_continuous_audit_recorder as get_audit_recorder,
            )
            from baldur.audit.event_buffer import AuditEvent, AuditEventType

            event = AuditEvent(
                event_type=AuditEventType.RECONCILIATION_ACCURACY_VERIFIED,
                source="verify_reconciliation_accuracy_task",
                details={
                    "calculation_id": shadow.calculation_id,
                    "estimated_errors": shadow.estimated_errors,
                    "actual_errors_30m": actual_errors,
                    "variance_percent": round(variance_percent, 2),
                    "log_source": shadow.log_source,
                    "status": shadow.status.value if shadow.status else "unknown",
                },
                actor_type="system",
            )

            recorder = get_audit_recorder()
            record_fn = getattr(recorder, "record", None) if recorder else None
            if record_fn is not None:
                record_fn(event)

        except Exception as e:
            logger.debug(
                "verify_reconciliation_accuracy.audit_recording_failed",
                error=e,
            )

    def _get_severity(self, result: dict[str, Any]) -> str:
        """Severity based on the high-variance item count."""
        high_variance = result.get("high_variance_count", 0)
        if high_variance >= 3:
            return "warning"
        return "info"

    def _get_summary_message(self, result: dict[str, Any]) -> str:
        """Build the notification message."""
        if result.get("error"):
            return f"❌ Accuracy verification failed: {result['error']}"

        verified = result.get("verified_count", 0)
        high_variance = result.get("high_variance_count", 0)

        if high_variance > 0:
            return f"⚠️ Reconciliation accuracy verification: {verified} done, {high_variance} high-variance"

        return f"✅ Reconciliation accuracy verification: {verified} done"


# =============================================================================
# Task Registry (for Celery registration)
# =============================================================================


# list of task classes (used for Celery registration)
INTELLIGENCE_TASKS = [
    CheckSLADriftTask,
    AnalyzeForensicPendingTask,
    CheckRecoveryTransitionsTask,
    VerifyReconciliationAccuracyTask,
]


# =============================================================================
# Celery shared_task wrappers (for Django project integration)
# =============================================================================


def register_intelligence_tasks_with_celery(app):
    """
    Register intelligence-lane tasks with the Celery app.

    Usage:
        from celery import Celery
        from baldur.tasks.intelligence_tasks import (
            register_intelligence_tasks_with_celery,
        )

        app = Celery('myproject')
        register_intelligence_tasks_with_celery(app)
    """
    for task_class in INTELLIGENCE_TASKS:
        # No "bind" key here: bind is a decorator option for function tasks,
        # and Task.bind is the classmethod Celery calls from register_task.
        # Setting it as a class attribute shadows that method and registration
        # fails with "'bool' object is not callable". These are class-based
        # tasks whose run(self) already receives self.
        wrapped = type(
            task_class.__name__,
            (task_class, app.Task),
            {"name": task_class.name},
        )
        app.register_task(wrapped())
        logger.info(
            "cell_registry.bulkheads_registered",
            task_class=task_class.name,
        )


# =============================================================================
# Beat Schedule definition
# =============================================================================


def get_intelligence_beat_schedule() -> dict[str, Any]:
    """
    Return the intelligence-lane Beat Schedule.

    Returns:
        dict: Celery Beat Schedule config
    """
    from celery.schedules import crontab

    return {
        # every 2 minutes - recovery status check
        "check-recovery-transitions": {
            "task": "baldur.check_recovery_transitions",
            "schedule": crontab(minute="*/2"),
            "options": {"queue": "realtime"},
        },
        # every 30 minutes - forensic analysis
        "analyze-forensic-pending": {
            "task": "baldur.analyze_forensic_pending",
            "schedule": crontab(minute="*/30"),
            "options": {"queue": "analysis"},
            "kwargs": {"threshold_minutes": 60},
        },
        # hourly - SLA drift check
        "check-sla-drift": {
            "task": "baldur.check_sla_drift",
            "schedule": crontab(minute=0),  # on the hour
            "options": {"queue": "analysis"},
        },
        # analyze-cross-stage-insights moved to the private learning lane
        # (baldur_dormant.services.learning.tasks
        # .get_learning_beat_schedule, 599 D10)
        # every 5 minutes - Reconciliation accuracy verification
        "verify-reconciliation-accuracy": {
            "task": "baldur.verify_reconciliation_accuracy",
            "schedule": crontab(minute="*/5"),
            "options": {"queue": "analysis"},
        },
    }


__all__ = [
    # Task Classes
    "CheckSLADriftTask",
    "AnalyzeForensicPendingTask",
    "CheckRecoveryTransitionsTask",
    "VerifyReconciliationAccuracyTask",
    # Registry
    "INTELLIGENCE_TASKS",
    "register_intelligence_tasks_with_celery",
    "get_intelligence_beat_schedule",
]
