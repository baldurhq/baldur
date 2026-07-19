"""
Reporting Lane Celery Tasks

Baldur metrics collection and compliance check tasks.

Tasks:
1. CollectBaldurMetricsTask - collect Baldur metrics
2. GenerateDailyAutonomousReportTask - generate the daily autonomous-operations report (daily_report.py)

GenerateFinOpsReportTask moved to baldur_pro.services.finops.tasks and
RunComplianceCheckTask moved to baldur_dormant.services.compliance.tasks
(599 D10 - both features relocated to the private distribution).
"""

from __future__ import annotations

from typing import Any

import structlog

from baldur.tasks.base import BaseNotifyingTask
from baldur.tasks.notification_policy import (
    NotificationPolicy,
    NotificationTiming,
)
from baldur.utils.time import utc_now

logger = structlog.get_logger()


# =============================================================================
# Task 1: Collect Baldur Metrics
# =============================================================================


class CollectBaldurMetricsTask(BaseNotifyingTask):
    """
    Collect Baldur metrics.

    Collects and stores system-wide Baldur metrics.

    Schedule: every 30 minutes
    Queue: metrics
    Notification: logs only (no notification)

    Returns:
        dict: {
            "success": bool,
            "metrics_collected": int,
            "timestamp": str,
        }
    """

    name = "baldur.collect_baldur_metrics"

    notification_policy = NotificationPolicy(
        timing=NotificationTiming.AGGREGATED,
        aggregate=True,
        threshold=float("inf"),  # effectively no notification
        threshold_field="always_skip",
        default_severity="info",
    )

    def run(self) -> dict[str, Any]:
        """Run the metrics collection task."""
        logger.info("collect_baldur_metrics.starting_metrics_collection")

        try:
            metrics_collected = 0

            # Circuit Breaker metrics
            try:
                from baldur.audit.resilience.circuit_breaker import (
                    CircuitBreakerRegistry,
                )

                registry = CircuitBreakerRegistry.get_instance()
                if registry:
                    cb_count = len(registry.get_all_stats())
                    metrics_collected += cb_count
            except Exception as e:
                logger.debug(
                    "cb.metrics_available",
                    error=e,
                )

            from baldur.factory.registry import ProviderRegistry

            # DLQ metric
            try:
                if ProviderRegistry.dlq_service.safe_get() is not None:
                    metrics_collected += 1
            except Exception as e:
                logger.debug("dlq.metrics_available", error=e)

            # Emergency Mode metric
            try:
                manager = ProviderRegistry.emergency_manager.safe_get()
                if manager is not None:
                    manager.get_current_level()
                    metrics_collected += 1
            except Exception as e:
                logger.debug("emergency.mode_metrics_available", error=e)

            timestamp = utc_now().isoformat()

            logger.info(
                "collect_baldur_metrics.collected_metrics",
                metrics_collected=metrics_collected,
            )

            return {
                "success": True,
                "metrics_collected": metrics_collected,
                "timestamp": timestamp,
            }

        except Exception as e:
            logger.exception(
                "collect_baldur_metrics.failed",
                error=e,
            )
            return {
                "success": False,
                "error": str(e),
                "metrics_collected": 0,
            }

    def _get_summary_message(self, result: dict[str, Any]) -> str:
        """Build the notification message (not actually used)."""
        return f"📊 Metrics collected: {result.get('metrics_collected', 0)}"


# =============================================================================
# NOTE: GenerateDailyAutonomousReportTask lives in daily_report.py.
# =============================================================================


# =============================================================================
# Task Registry (for Celery registration)
# =============================================================================


# List of task classes (used during Celery registration)
# NOTE: GenerateDailyAutonomousReportTask lives in daily_report.py
COMPLIANCE_TASKS = [
    CollectBaldurMetricsTask,
]


# =============================================================================
# Celery shared_task wrappers (for Django project integration)
# =============================================================================


def register_compliance_tasks_with_celery(app):
    """
    Register the reporting lane tasks with the Celery app.

    Usage:
        from celery import Celery
        from baldur.tasks.compliance_tasks import (
            register_compliance_tasks_with_celery,
        )

        app = Celery('myproject')
        register_compliance_tasks_with_celery(app)
    """
    for task_class in COMPLIANCE_TASKS:
        # No "bind" key here — see register_intelligence_tasks_with_celery.
        wrapped = type(
            task_class.__name__,
            (task_class, app.Task),
            {"name": task_class.name},
        )
        app.register_task(wrapped())
        logger.info(
            "compliance_tasks.task_registered",
            task_name=task_class.name,
        )


# =============================================================================
# Beat Schedule definitions
# =============================================================================


def get_compliance_beat_schedule() -> dict[str, Any]:
    """
    Return the reporting lane Beat Schedule.

    Returns:
        dict: Celery Beat Schedule configuration
    """
    from celery.schedules import crontab

    return {
        # generate-finops-report moved to the private finops lane
        # (baldur_pro.services.finops.tasks.get_finops_beat_schedule, 599 D10)
        # Every 30 minutes - metrics collection
        "collect-baldur-metrics": {
            "task": "baldur.collect_baldur_metrics",
            "schedule": crontab(minute="*/30"),
            "options": {"queue": "metrics"},
        },
        # run-compliance-check moved to the private compliance lane
        # (baldur_dormant.services.compliance.tasks
        # .get_compliance_check_beat_schedule, 599 D10)
        # NOTE: generate-daily-autonomous-report is defined in
        # get_daily_report_beat_schedule() in daily_report.py
    }


__all__ = [
    # Task Classes
    # NOTE: GenerateDailyAutonomousReportTask is exported from daily_report.py
    "CollectBaldurMetricsTask",
    # Registry
    "COMPLIANCE_TASKS",
    "register_compliance_tasks_with_celery",
    "get_compliance_beat_schedule",
]
