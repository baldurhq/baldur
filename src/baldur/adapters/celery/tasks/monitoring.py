"""
Metrics & Monitoring Celery Tasks.

These tasks collect metrics, check SLA breaches, and provide health monitoring.

These tasks are registered but are NOT part of the composed beat schedule
that configure_baldur_celery() injects. To run them periodically, add them
to your own beat config:

    CELERY_BEAT_SCHEDULE.update({
        "collect-baldur-metrics": {
            "task": "baldur.adapters.celery.tasks.collect_baldur_metrics",
            "schedule": 60.0,  # Every minute
        },
        "check-sla-breaches": {
            "task": "baldur.adapters.celery.tasks.check_and_report_sla_breaches",
            "schedule": 300.0,  # Every 5 minutes
        },
        "emit-baldur-heartbeat": {
            "task": "baldur.adapters.celery.tasks.emit_baldur_heartbeat",
            "schedule": 60.0,  # Should match heartbeat_interval_seconds config
        },
    })
"""

from datetime import UTC, datetime, timedelta

import structlog
from celery import shared_task

logger = structlog.get_logger(__name__)


@shared_task(
    bind=True,
    name="baldur.adapters.celery.tasks.collect_baldur_metrics",
    queue="monitoring",
    max_retries=1,
    time_limit=60,
    soft_time_limit=55,
)
def collect_baldur_metrics(self) -> dict:
    """
    Periodic task to collect and update baldur metrics.

    Collects:
    - DLQ pending counts by domain
    - DLQ items by status
    - Circuit breaker states
    - Retry success rates

    Uses ProviderRegistry for statistics repository access.

    Returns:
        Dictionary with collected metric values
    """
    logger.debug("celery_monitoring.collection_started")

    try:
        from baldur.factory import ProviderRegistry

        stats_repo = ProviderRegistry.get_statistics_repo()

        # DLQ stats
        status_counts = stats_repo.get_status_counts()
        domain_dist = stats_repo.get_domain_distribution(limit=20)

        dlq_by_domain = {d.domain: d.count for d in domain_dist}
        dlq_by_status = {
            "pending": status_counts.pending,
            "resolved": status_counts.resolved,
            "failed": status_counts.failed,
            "archived": status_counts.archived,
        }

        # Circuit breaker stats from Redis
        cb_summary = stats_repo.get_circuit_breaker_summary()

        metrics = {
            "dlq_pending_by_domain": dlq_by_domain,
            "dlq_by_status": dlq_by_status,
            "circuit_breakers_open": cb_summary.open,
            "circuit_breakers_half_open": cb_summary.half_open,
        }

        logger.debug(
            "metrics.collection_complete",
            status_counts=status_counts.pending,
        )

        return {
            "success": True,
            **metrics,
        }

    except Exception as e:
        logger.exception(
            "metrics.collect_metrics_failed",
            error=e,
        )
        return {
            "success": False,
            "error": str(e),
        }


@shared_task(
    bind=True,
    name="baldur.adapters.celery.tasks.check_and_report_sla_breaches",
    queue="monitoring",
    max_retries=1,
    time_limit=120,
    soft_time_limit=110,
)
def check_and_report_sla_breaches(self) -> dict:
    """
    Periodic task to check for SLA breaches.

    Checks pending DLQ entries against SLA thresholds and records breaches.
    Uses ProviderRegistry for statistics repository access.

    Returns:
        Dictionary with SLA breach information
    """
    logger.debug("sla_check.breach_check_started")

    try:
        from baldur.factory import ProviderRegistry

        stats_repo = ProviderRegistry.get_statistics_repo()

        # Default SLA: 4 hours for resolution
        sla_threshold = timedelta(hours=4)
        datetime.now(UTC) - sla_threshold

        # Get SLA breaches from statistics repository
        breaches_by_domain = stats_repo.get_sla_breaches(
            sla_threshold_hours=4,
            statuses=["pending", "reviewing", "requires_review"],
        )

        total_breaches = sum(breaches_by_domain.values())

        if total_breaches > 0:
            logger.warning(
                "sla_check.breaches_found",
                total_sla_breaches=total_breaches,
                by_domain=breaches_by_domain,
            )
        else:
            logger.debug("sla_check.no_breaches_found")

        return {
            "success": True,
            "total_breaches": total_breaches,
            "breaches_by_domain": breaches_by_domain,
        }

    except Exception as e:
        logger.exception(
            "sla.check_failed",
            error=e,
        )
        return {
            "success": False,
            "error": str(e),
        }


@shared_task(
    bind=True,
    name="baldur.adapters.celery.tasks.emit_baldur_heartbeat",
    queue="monitoring",
    max_retries=0,
    time_limit=10,
    soft_time_limit=8,
)
def emit_baldur_heartbeat(self, component: str = "error_budget") -> dict:
    """
    Periodic heartbeat task for Dead Man's Snitch.

    This task should be scheduled at a regular interval (default: 60 seconds).
    If the heartbeat metric stops being updated, Prometheus will fire an alert.

    Args:
        component: The component emitting the heartbeat (default: 'error_budget')

    Returns:
        Dictionary with heartbeat status

    Prometheus Alert Rules:
        - BaldurServiceDead: time() - baldur_heartbeat_timestamp_seconds > 120
        - BaldurHeartbeatMissing: absent(baldur_heartbeat_timestamp_seconds) == 1
    """
    import time

    try:
        from baldur.factory.registry import ProviderRegistry

        manager = ProviderRegistry.runtime_config_manager.safe_get()
        if manager is None:
            raise RuntimeError("baldur_pro RuntimeConfigManager not registered")
        config = manager.get_error_budget_config()

        from baldur.settings.error_budget import get_error_budget_settings

        settings = get_error_budget_settings()

        if not config.get("heartbeat_enabled", settings.heartbeat_enabled):
            logger.debug(
                "heartbeat.disabled",
                monitored_component=component,
            )
            return {
                "success": True,
                "component": component,
                "status": "disabled",
                "timestamp": time.time(),
            }

        from baldur.services.metrics.recorders import emit_heartbeat

        emit_heartbeat(component=component)

        current_time = time.time()
        logger.debug(
            "heartbeat.emitted",
            monitored_component=component,
            current_time=current_time,
        )

        return {
            "success": True,
            "component": component,
            "status": "alive",
            "timestamp": current_time,
            "interval_seconds": config.get(
                "heartbeat_interval_seconds", settings.heartbeat_interval_seconds
            ),
            "timeout_seconds": config.get(
                "heartbeat_timeout_seconds", settings.heartbeat_timeout_seconds
            ),
        }

    except Exception as e:
        logger.exception(
            "heartbeat.emit_heartbeat_failed",
            error=e,
        )
        try:
            from baldur.services.metrics.recorders import emit_heartbeat

            emit_heartbeat(component=f"{component}_degraded")
        except Exception:
            pass

        return {
            "success": False,
            "component": component,
            "status": "error",
            "error": str(e),
        }


@shared_task(
    bind=True,
    name="baldur.adapters.celery.tasks.notify_failsafe_recovery",
    queue="monitoring",
    max_retries=1,
    time_limit=30,
    soft_time_limit=25,
)
def notify_failsafe_recovery(
    self,
    component: str,
    downtime_seconds: float,
    recovery_reason: str = "System recovered automatically",
) -> dict:
    """
    Send recovery notification when fail-safe mode is deactivated.

    This task should be called when the system transitions from fail-safe
    mode back to normal operation.

    Args:
        component: The component that recovered
        downtime_seconds: How long the component was in fail-safe mode
        recovery_reason: Why the system recovered

    Returns:
        Dictionary with notification status
    """
    try:
        from baldur.factory.registry import ProviderRegistry
        from baldur.settings.error_budget import get_error_budget_settings

        manager = ProviderRegistry.runtime_config_manager.safe_get()
        if manager is None:
            raise RuntimeError("baldur_pro RuntimeConfigManager not registered")
        config = manager.get_error_budget_config()
        settings = get_error_budget_settings()

        if not config.get("recovery_alert_enabled", settings.recovery_alert_enabled):
            logger.info(
                "recovery.recovery_alert_disabled_skipping",
                monitored_component=component,
            )
            return {
                "success": True,
                "component": component,
                "status": "disabled",
            }

        from baldur.services.metrics.recorders import (
            record_failsafe_recovered,
            record_recovery_alert,
        )

        record_recovery_alert(component=component)
        record_failsafe_recovered(component=component)

        # Gate downtime info based on recovery_alert_include_downtime setting
        include_downtime = settings.recovery_alert_include_downtime
        alert_downtime = downtime_seconds if include_downtime else None

        try:
            from baldur.factory import ProviderRegistry

            # Use the GenericProviderRegistry slot's get() — there is no
            # standalone `get_alert_adapter()` classmethod (P7 sub-PR 4 drift).
            alert_adapter = ProviderRegistry.alert.get()

            if hasattr(alert_adapter, "alert_failsafe_recovered"):
                alert_adapter.alert_failsafe_recovered(
                    component=component,
                    downtime_seconds=alert_downtime or 0.0,
                    recovery_reason=recovery_reason,
                )
                logger.info(
                    "recovery.sent_recovery_alert",
                    monitored_component=component,
                    downtime_seconds=alert_downtime,
                )
            else:
                logger.warning("recovery.alert_adapter_support_recovery")
        except Exception as adapter_error:
            logger.warning(
                "recovery.send_alert_via_adapter",
                adapter_error=adapter_error,
            )

        return {
            "success": True,
            "component": component,
            "downtime_seconds": downtime_seconds,
            "recovery_reason": recovery_reason,
            "alert_sent": True,
        }

    except Exception as e:
        logger.exception(
            "recovery.send_recovery_notification_failed",
            error=e,
        )
        return {
            "success": False,
            "component": component,
            "error": str(e),
        }
