"""
Metrics Celery Tasks

Tasks for observability and SLA monitoring.
"""

import structlog
from celery import shared_task

logger = structlog.get_logger(__name__)


@shared_task(
    bind=True,
    name="baldur.celery_tasks.collect_baldur_metrics",
    queue="monitoring",
    max_retries=1,
    time_limit=60,
    soft_time_limit=55,
)
def collect_baldur_metrics(self) -> dict:
    """
    Periodic task to collect and update baldur Prometheus metrics.

    Updates gauge metrics that require database queries:
    - DLQ pending counts by domain
    - DLQ items by status
    - Circuit breaker states
    - Retry success rates

    This task should be scheduled to run every minute.

    Returns:
        Dictionary with collected metric values
    """
    from baldur.services import collect_all_metrics

    task_id = self.request.id or "unknown"
    bound_logger = logger.bind(task_id=task_id)

    bound_logger.debug("metrics_tasks.collection_started")

    try:
        metrics = collect_all_metrics()

        bound_logger.debug(
            "metrics.collection_complete_values",
            dlq_pending_total=sum(metrics.get("dlq_pending_by_domain", {}).values()),
        )

        return {
            "success": True,
            **metrics,
        }

    except Exception as e:
        bound_logger.exception(
            "metrics.collect_metrics_failed",
            error=e,
        )
        return {
            "success": False,
            "error": str(e),
        }


@shared_task(
    bind=True,
    name="baldur.celery_tasks.check_and_report_sla_breaches",
    queue="monitoring",
    max_retries=1,
    time_limit=120,
    soft_time_limit=110,
)
def check_and_report_sla_breaches(self) -> dict:
    """
    Periodic task to check for SLA breaches and record metrics.

    SLA thresholds are configured in services/baldur/config.py.
    See SLAThresholds class for default values and customization.

    This task should be scheduled to run every 5 minutes.

    Returns:
        Dictionary with SLA breach information
    """
    from baldur.factory.registry import ProviderRegistry
    from baldur.services import record_sla_breach

    task_id = self.request.id or "unknown"
    bound_logger = logger.bind(task_id=task_id)

    bound_logger.debug("sla_check.breach_check_started")

    try:
        dlq_service = ProviderRegistry.dlq_service.safe_get()
        if dlq_service is None:
            raise RuntimeError("baldur_pro DLQService not registered")
        breached_entries = dlq_service.get_sla_breached_entries()

        breaches_by_domain: dict[str, int] = {}

        for entry in breached_entries:
            domain = entry.domain
            breaches_by_domain[domain] = breaches_by_domain.get(domain, 0) + 1
            record_sla_breach(domain)

        total_breaches = sum(breaches_by_domain.values())

        if total_breaches > 0:
            bound_logger.warning(
                "sla.check_found_sla",
                total_breaches=total_breaches,
                breaches_by_domain=breaches_by_domain,
            )
        else:
            bound_logger.debug("sla_check.no_breaches_found")

        return {
            "success": True,
            "total_breaches": total_breaches,
            "breaches_by_domain": breaches_by_domain,
        }

    except Exception as e:
        bound_logger.exception(
            "sla.check_failed",
            error=e,
        )
        return {
            "success": False,
            "error": str(e),
        }
