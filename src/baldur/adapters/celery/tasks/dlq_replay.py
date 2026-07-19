"""
DLQ Replay Celery Tasks.

These tasks handle replay of failed operations from the Dead Letter Queue.

RBAC role information is propagated through the actor_info parameter.

Usage in CELERY_BEAT_SCHEDULE:
    'cleanup-dlq-entries': {
        'task': 'baldur.adapters.celery.tasks.cleanup_resolved_dlq_entries',
        'schedule': 86400.0,  # Daily
    },
"""

from typing import Any

import structlog
from celery import shared_task

logger = structlog.get_logger(__name__)


@shared_task(
    bind=True,
    name="baldur.adapters.celery.tasks.replay_single_dlq_entry",
    queue="dlq_processing",
    max_retries=0,
    time_limit=120,
    soft_time_limit=110,
    acks_late=True,
)
def replay_single_dlq_entry(
    self,
    dlq_id: str,
    actor_info: dict[str, Any] | None = None,  # RBAC role propagation
    trace_info: dict[str, Any] | None = None,  # trace_id propagation
    trigger: str = "manual_replay",
) -> dict:
    """
    Replay a single DLQ entry.

    This task delegates to ReplayService which handles all safety checks:
    - Kill Switch
    - Emergency Level (LEVEL_2+)
    - ErrorBudgetGate

    The manual caller's RBAC role is propagated through actor_info, and the
    original request's trace_id through trace_info.

    - When actor_info is None the call is treated as an automatic (Beat) call
      and SYSTEM_ACTOR is used.
    - When trace_info is None a trace_id of the form INTERNAL_BEAT_xxx is
      generated automatically.

    Args:
        dlq_id: ID of the FailedOperation to replay
        actor_info: Actor information from calling context (optional)
        trace_info: Trace information from calling context (optional)
        trigger: Provenance trigger stamped into resolution_type (default:
            manual_replay)

    Returns:
        Dictionary with replay result
    """
    logger.info(
        "dlq.replay_task_starting",
        dlq_id=dlq_id,
    )

    try:
        from baldur.context.actor_context import restore_actor_from_celery
        from baldur.services.replay_service import ReplayService

        # trace_id is injected automatically by the task_prerun signal.
        # actor_info present -> restore ActorContext (manual call).
        # actor_info absent -> SYSTEM_ACTOR (automatic Beat call).
        # The restored ActorContext is the audit principal for the replay.
        with restore_actor_from_celery(actor_info or {}):
            service = ReplayService()
            result = service.replay_single(dlq_id, trigger=trigger)

        return {
            "success": result.success,
            "dlq_id": dlq_id,
            "message": result.message if result.success else "",
            "error": result.error,
            "data": result.data,
        }

    except Exception as e:
        logger.exception(
            "dlq.replay_task_unexpected",
            dlq_id=dlq_id,
            error=e,
        )
        return {
            "success": False,
            "dlq_id": dlq_id,
            "error": str(e),
        }


@shared_task(
    bind=True,
    name="baldur.adapters.celery.tasks.replay_batch_by_domain",
    queue="dlq_processing",
    max_retries=0,
    time_limit=600,
    soft_time_limit=580,
    acks_late=True,
)
def replay_batch_by_domain(
    self,
    domain: str,
    max_items: int = 100,
    actor_info: dict[str, Any] | None = None,  # RBAC role propagation
    trace_info: dict[str, Any] | None = None,  # trace_id propagation
    trigger: str = "manual_replay",
) -> dict:
    """
    Replay all pending DLQ entries for a specific domain.

    This task delegates to ReplayService which handles all safety checks:
    - Kill Switch
    - Emergency Level (LEVEL_2+)
    - ErrorBudgetGate

    The manual caller's RBAC role is propagated through actor_info, and the
    original request's trace_id through trace_info.

    Args:
        domain: The domain to filter by (payment, point, inventory, etc.)
        max_items: Maximum number of items to replay
        actor_info: Actor information from calling context (optional)
        trace_info: Trace information from calling context (optional)
        trigger: Provenance trigger stamped into resolution_type (default:
            manual_replay)

    Returns:
        Dictionary with batch replay summary
    """
    logger.info(
        "dlq.batch_replay_task",
        healing_domain=domain,
        max_items=max_items,
    )

    try:
        from baldur.context.actor_context import restore_actor_from_celery
        from baldur.services.replay_service import ReplayService

        # trace_id is injected automatically by the task_prerun signal.
        # actor_info present -> restore ActorContext (manual call).
        with restore_actor_from_celery(actor_info or {}):
            service = ReplayService()
            result = service.replay_batch(
                domain=domain, max_items=max_items, trigger=trigger
            )

        return {
            "success": result.success_count > 0 or result.total == 0,
            "domain": domain,
            "total": result.total,
            "success_count": result.success_count,
            "failed_count": result.failed_count,
            "skipped_count": result.skipped_count,
        }

    except Exception as e:
        logger.exception(
            "dlq.batch_replay_task",
            error=e,
        )
        return {
            "success": False,
            "domain": domain,
            "error": str(e),
            "total": 0,
            "success_count": 0,
            "failed_count": 0,
        }


@shared_task(
    bind=True,
    name="baldur.adapters.celery.tasks.cleanup_resolved_dlq_entries",
    queue="maintenance",
    max_retries=1,
    time_limit=300,
    soft_time_limit=290,
)
def cleanup_resolved_dlq_entries(self, days_old: int = 30) -> dict:
    """
    Archive old resolved DLQ entries.

    This task runs periodically to clean up old entries.
    Entries are marked as ARCHIVED (soft-delete) for audit trail.

    Uses ProviderRegistry for statistics repository access.

    Args:
        days_old: Archive entries older than this many days

    Returns:
        Dictionary with cleanup summary
    """
    logger.info(
        "dlq.cleanup_starting_cleanup",
        days_old=days_old,
    )

    try:
        from baldur.factory import ProviderRegistry
        from baldur.services.daily_report import record_cleanup_result

        if not ProviderRegistry.has_statistics_adapter():
            logger.info("dlq_cleanup.stats_adapter_unavailable")
            return {
                "success": True,
                "skipped": True,
                "reason": "no_statistics_adapter",
            }

        stats_repo = ProviderRegistry.get_statistics_repo()

        archived_count = stats_repo.archive_old_entries(older_than_days=days_old)

        logger.info(
            "dlq.cleanup_completed",
            archived_count=archived_count,
        )

        cleanup_summary = {
            "success": True,
            "archived_count": archived_count,
        }
        record_cleanup_result(
            "baldur.adapters.celery.tasks.cleanup_resolved_dlq_entries",
            cleanup_summary,
        )
        return cleanup_summary

    except Exception as e:
        logger.exception(
            "dlq.cleanup_unexpected_error",
            error=e,
        )
        return {
            "success": False,
            "error": str(e),
        }
