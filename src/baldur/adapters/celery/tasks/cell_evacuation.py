"""
Async Celery tasks for cell evacuation.

Handles audit logging and event publication asynchronously when a cell is
isolated or restored. Invoked from CellEvacuationPolicy's fire-and-forget
notifications.

autoretry retries automatically on delivery failure (max 3 attempts, 30s
apart), and acks_late returns unfinished tasks to the broker when a worker
shuts down.
"""

from __future__ import annotations

from typing import Any

import structlog
from celery import shared_task

logger = structlog.get_logger(__name__)


@shared_task(
    bind=True,
    name="baldur.adapters.celery.tasks.notify_cell_isolation",
    queue="baldur",
    autoretry_for=(Exception,),
    max_retries=3,
    default_retry_delay=30,
    acks_late=True,
    time_limit=60,
    soft_time_limit=55,
)
def notify_cell_isolation(
    self,
    cell_id: str,
    reason: str,
    duration_seconds: int = 3600,
) -> dict[str, Any]:
    """
    Task that asynchronously emits the cell-isolation audit log.

    Calls RegionalIsolationGate.isolate_region() to record the audit log and
    the global event.

    Args:
        cell_id: Identifier of the cell to isolate
        reason: Isolation reason
        duration_seconds: Isolation duration (seconds)

    Returns:
        Result dictionary
    """
    from baldur.services.isolation.regional_gate import (
        get_regional_isolation_gate,
    )

    task_id = self.request.id or "unknown"
    bound_logger = logger.bind(task_id=task_id)

    gate = get_regional_isolation_gate()
    result = gate.isolate_region(
        region=cell_id,
        reason=reason,
        duration_seconds=duration_seconds,
    )

    bound_logger.info(
        "cell_evacuation.isolation_notified",
        cell_id=cell_id,
        reason=reason,
        result=result,
        attempt=self.request.retries + 1,
    )
    return {"status": "notified", "cell_id": cell_id, "isolated": result}


@shared_task(
    bind=True,
    name="baldur.adapters.celery.tasks.notify_cell_blast_radius",
    queue="baldur",
    autoretry_for=(Exception,),
    max_retries=3,
    default_retry_delay=30,
    acks_late=True,
    time_limit=60,
    soft_time_limit=55,
)
def notify_cell_blast_radius(
    self,
    cell_id: str,
    affected_services: list[str] | None = None,
) -> dict[str, Any]:
    """
    Task that asynchronously sets the cell blast-radius policy.

    Calls BlastRadiusService.set_policy() to record the audit log.

    Args:
        cell_id: Identifier of the target cell
        affected_services: List of affected services

    Returns:
        Result dictionary
    """
    from baldur.services.blast_radius.models import BlastRadiusLevel
    from baldur.services.blast_radius.service import BlastRadiusService

    task_id = self.request.id or "unknown"
    bound_logger = logger.bind(task_id=task_id)

    blast_service = BlastRadiusService()
    blast_service.set_policy(
        service_name=cell_id,
        level=BlastRadiusLevel.CRITICAL,
        affected_services=affected_services or [],
        max_affected_percentage=0.0,
        auto_isolate=True,
    )

    bound_logger.info(
        "cell_evacuation.blast_radius_notified",
        cell_id=cell_id,
        affected_services_count=len(affected_services or []),
        attempt=self.request.retries + 1,
    )
    return {
        "status": "notified",
        "cell_id": cell_id,
        "affected_services_count": len(affected_services or []),
    }


@shared_task(
    bind=True,
    name="baldur.adapters.celery.tasks.notify_cell_restoration",
    queue="baldur",
    autoretry_for=(Exception,),
    max_retries=3,
    default_retry_delay=30,
    acks_late=True,
    time_limit=60,
    soft_time_limit=55,
)
def notify_cell_restoration(
    self,
    cell_id: str,
) -> dict[str, Any]:
    """
    Task that asynchronously emits the cell-restoration audit log.

    Calls RegionalIsolationGate.restore_region() to record the restoration
    audit log.

    Args:
        cell_id: Identifier of the cell to restore

    Returns:
        Result dictionary
    """
    from baldur.services.isolation.regional_gate import (
        get_regional_isolation_gate,
    )

    task_id = self.request.id or "unknown"
    bound_logger = logger.bind(task_id=task_id)

    gate = get_regional_isolation_gate()
    result = gate.restore_region(cell_id)

    bound_logger.info(
        "cell_evacuation.restoration_notified",
        cell_id=cell_id,
        result=result,
        attempt=self.request.retries + 1,
    )
    return {"status": "notified", "cell_id": cell_id, "restored": result}
