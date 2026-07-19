"""
DLQ Celery Tasks

Tasks for replaying failed operations from the Dead Letter Queue.
"""

import structlog
from celery import shared_task

from baldur.utils.time import utc_now

logger = structlog.get_logger(__name__)

# Bounds for the compressed-entry lifecycle drain. One run processes at most
# _COMPRESSED_DRAIN_PAGE_SIZE * _COMPRESSED_DRAIN_MAX_ITERATIONS entries per
# transition; whatever is left is picked up by the next daily run.
_COMPRESSED_DRAIN_PAGE_SIZE = 500
_COMPRESSED_DRAIN_MAX_ITERATIONS = 200


@shared_task(
    bind=True,
    name="baldur.celery_tasks.conditional_replay_on_circuit_close",
    queue="dlq_processing",
    max_retries=0,
    time_limit=300,
    soft_time_limit=290,
    acks_late=True,
)
def conditional_replay_on_circuit_close(
    self, service_name: str, max_items: int = 50
) -> dict:
    """
    Trigger conditional replay when a circuit breaker closes.

    This task is called by CircuitBreakerService.force_close() when
    trigger_replay=True is specified.

    Replays DLQ entries that failed due to the recovered service.

    Args:
        service_name: Name of the service that recovered
        max_items: Maximum number of items to replay

    Returns:
        Dictionary with replay result summary
    """
    from baldur.services import get_replay_service

    task_id = self.request.id or "unknown"
    bound_logger = logger.bind(task_id=task_id)

    bound_logger.info(
        "dlq.circuit_recovery_started",
        service_name=service_name,
        max_items=max_items,
    )

    try:
        service = get_replay_service()
        result = service.replay_on_circuit_close(
            service_name=service_name,
            max_items=max_items,
        )

        # Check governance blocking before success
        if result.governance_blocked:
            bound_logger.warning(
                "dlq.circuit_recovery_blocked",
                service_name=service_name,
                reason=result.governance_block_reason,
            )
            return {
                "success": False,
                "service_name": service_name,
                "error": "governance_blocked",
                "block_reason": result.governance_block_reason,
                "total": 0,
            }

        bound_logger.info(
            "dlq.circuit_recovery_completed",
            service_name=service_name,
            dlq_total=result.total,
            success_count=result.success_count,
            failed_count=result.failed_count,
            capped=result.capped,
        )

        return {
            "success": True,
            "service_name": service_name,
            "total": result.total,
            "success_count": result.success_count,
            "failed_count": result.failed_count,
            "capped": result.capped,
        }

    except Exception as e:
        bound_logger.exception(
            "dlq.circuit_recovery_failed",
            service_name=service_name,
            error=str(e),
        )
        return {
            "success": False,
            "service_name": service_name,
            "error": str(e),
        }


@shared_task(
    bind=True,
    name="baldur.celery_tasks.replay_single_dlq_entry",
    queue="dlq_processing",
    max_retries=0,
    time_limit=120,
    soft_time_limit=110,
    acks_late=True,
)
def replay_single_dlq_entry(self, dlq_id: str, trigger: str = "manual_replay") -> dict:
    """
    Replay a single DLQ entry.

    This task is triggered by operators via admin UI or API.

    Args:
        dlq_id: ID of the FailedOperation to replay
        trigger: Provenance trigger stamped into resolution_type (default:
            manual_replay). Operator/manual scheduled batch replay passes
            "scheduled_batch" (there is no automatic scheduled producer).

    Returns:
        Dictionary with replay result
    """
    from baldur.services import get_replay_service

    task_id = self.request.id or "unknown"
    bound_logger = logger.bind(task_id=task_id)

    bound_logger.info(
        "dlq.replay_starting_replay",
        dlq_id=dlq_id,
    )

    try:
        service = get_replay_service()
        result = service.replay_single(dlq_id, trigger=trigger)

        if result.success:
            bound_logger.info(
                "dlq.replay_successfully_replayed",
                dlq_id=dlq_id,
            )
            return {
                "success": True,
                "dlq_id": dlq_id,
                "message": result.message,
                "data": result.data,
            }
        bound_logger.warning(
            "dlq.replay_failed_replay",
            dlq_id=dlq_id,
            result_error=result.error,
        )
        return {
            "success": False,
            "dlq_id": dlq_id,
            "error": result.error,
        }

    except Exception as e:
        bound_logger.exception(
            "dlq.replay_unexpected_error",
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
    name="baldur.celery_tasks.replay_batch_by_failure_type",
    queue="dlq_processing",
    max_retries=0,
    time_limit=600,
    soft_time_limit=580,
    acks_late=True,
)
def replay_batch_by_failure_type(
    self,
    failure_type: str,
    max_items: int = 100,
    trigger: str = "manual_replay",
) -> dict:
    """
    Replay all pending DLQ entries of a specific failure type.

    This task is used for batch recovery after system issues are resolved.

    Args:
        failure_type: The failure type to filter by
        max_items: Maximum number of items to replay
        trigger: Provenance trigger stamped into resolution_type (default:
            manual_replay). Operator/manual scheduled batch replay passes
            "scheduled_batch" (there is no automatic scheduled producer).

    Returns:
        Dictionary with batch replay summary
    """
    from baldur.services import get_replay_service

    task_id = self.request.id or "unknown"
    bound_logger = logger.bind(task_id=task_id)

    bound_logger.info(
        "dlq.batch_replay_starting",
        failure_type=failure_type,
        max_items=max_items,
    )

    try:
        service = get_replay_service()
        result = service.replay_batch(
            failure_type=failure_type,
            max_items=max_items,
            trigger=trigger,
        )

        bound_logger.info(
            "dlq.batch_replay_completed",
            dlq_total=result.total,
            success_count=result.success_count,
            failed_count=result.failed_count,
        )

        return {
            "success": True,
            "total": result.total,
            "success_count": result.success_count,
            "failed_count": result.failed_count,
            "skipped_count": result.skipped_count,
        }

    except Exception as e:
        bound_logger.exception(
            "dlq.batch_replay_unexpected",
            error=e,
        )
        return {
            "success": False,
            "error": str(e),
        }


@shared_task(
    bind=True,
    name="baldur.celery_tasks.replay_batch_by_domain",
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
    trigger: str = "manual_replay",
) -> dict:
    """
    Replay all pending DLQ entries for a specific domain.

    This task is used for domain-wide recovery operations.

    Args:
        domain: The domain to filter by (payment, point, inventory, webhook, notification)
        max_items: Maximum number of items to replay
        trigger: Provenance trigger stamped into resolution_type (default:
            manual_replay). Operator/manual scheduled batch replay passes
            "scheduled_batch" (there is no automatic scheduled producer).

    Returns:
        Dictionary with batch replay summary
    """
    from baldur.services import get_replay_service

    task_id = self.request.id or "unknown"
    bound_logger = logger.bind(task_id=task_id)

    bound_logger.info(
        "dlq.batch_replay_starting",
        healing_domain=domain,
        max_items=max_items,
    )

    try:
        service = get_replay_service()
        result = service.replay_batch(
            domain=domain,
            max_items=max_items,
            trigger=trigger,
        )

        bound_logger.info(
            "dlq.batch_replay_completed",
            dlq_total=result.total,
            success_count=result.success_count,
            failed_count=result.failed_count,
        )

        return {
            "success": True,
            "domain": domain,
            "total": result.total,
            "success_count": result.success_count,
            "failed_count": result.failed_count,
        }

    except Exception as e:
        bound_logger.exception(
            "dlq.batch_replay_unexpected",
            error=e,
        )
        return {
            "success": False,
            "error": str(e),
        }


@shared_task(
    bind=True,
    name="baldur.celery_tasks.evict_overflow_dlq_entries",
    queue="maintenance",
    max_retries=0,
    time_limit=120,
    soft_time_limit=110,
)
def evict_overflow_dlq_entries(self) -> dict:
    """
    Background DLQ overflow eviction with distributed lock.

    Celery Beat: 10s interval recommended.
    3-tier water level based eviction intensity.

    Distributed lock prevents concurrent compression across workers
    when compress_oldest strategy is active.
    """
    from baldur_pro.services.dlq.overflow import run_background_eviction

    task_id = self.request.id or "unknown"
    bound_logger = logger.bind(task_id=task_id)

    # Distributed lock: prevent concurrent compression across workers
    lock = None
    lock_acquired = False
    lock_namespace = "dlq-compression"
    session_id = f"celery-{task_id}"

    try:
        from datetime import timedelta

        from baldur_pro.services.coordination.distributed_recovery_lock import (
            DistributedRecoveryLock,
        )

        lock = DistributedRecoveryLock(lock_timeout=timedelta(minutes=2))
        lock_acquired = lock.acquire(
            namespace=lock_namespace,
            session_id=session_id,
            blocking=False,  # Non-blocking: skip if another worker is processing
        )
    except ImportError:
        # Coordination module not available (e.g., Redis not configured)
        lock_acquired = True  # Fail-open: proceed without lock
    except Exception:
        bound_logger.warning("dlq.compress_lock_acquisition_failed")
        lock_acquired = True  # Fail-open: system stability over strict locking

    if not lock_acquired:
        bound_logger.info(
            "dlq.compress_lock_skipped",
            reason="another_worker_compressing",
        )
        return {"status": "skipped", "reason": "lock_not_acquired"}

    bound_logger.debug("dlq.compress_lock_acquired", session_id=session_id)

    try:
        return run_background_eviction()
    except Exception as e:
        bound_logger.exception("dlq.overflow_eviction_error", error=e)
        return {"success": False, "error": str(e)}
    finally:
        if lock is not None and lock_acquired:
            try:
                lock.release(namespace=lock_namespace, session_id=session_id)
            except Exception:
                bound_logger.warning("dlq.compress_lock_release_failed")


@shared_task(
    bind=True,
    name="baldur.celery_tasks.cleanup_resolved_dlq_entries",
    queue="maintenance",
    max_retries=1,
    time_limit=300,
    soft_time_limit=290,
)
def cleanup_resolved_dlq_entries(self, days_old: int = 30) -> dict:
    """
    Archive old resolved DLQ entries (soft-delete, NOT hard delete).

    This task runs periodically to archive old DLQ entries.
    Entries are marked as ARCHIVED instead of deleted for audit trail.

    Retention Policy:
    - Expired entries: mark as EXPIRED
    - Old resolved/rejected: mark as ARCHIVED (soft-delete)
    - Never hard delete for compliance (payment/point records)

    Args:
        days_old: Archive entries older than this many days

    Returns:
        Dictionary with cleanup summary
    """

    from baldur.factory.registry import ProviderRegistry
    from baldur.services.daily_report import record_cleanup_result

    task_id = self.request.id or "unknown"
    bound_logger = logger.bind(task_id=task_id)

    bound_logger.info(
        "dlq.cleanup_starting_cleanup",
        days_old=days_old,
    )

    try:
        dlq_service = ProviderRegistry.dlq_service.safe_get()
        if dlq_service is None:
            raise RuntimeError("baldur_pro DLQService not registered")
        result = dlq_service.cleanup_old_entries(days_old=days_old)

        bound_logger.info(
            "dlq.cleanup_completed",
            expired_count=result.get("expired_count", 0),
            archived_count=result.get("archived_count", 0),
        )

        cleanup_summary = {
            "success": True,
            **result,
        }
        record_cleanup_result(
            "baldur.celery_tasks.cleanup_resolved_dlq_entries", cleanup_summary
        )
        return cleanup_summary

    except Exception as e:
        bound_logger.exception(
            "dlq.cleanup_unexpected_error",
            error=e,
        )
        return {
            "success": False,
            "error": str(e),
        }


@shared_task(
    bind=True,
    name="baldur.celery_tasks.cleanup_compressed_dlq_entries",
    queue="maintenance",
    max_retries=1,
    time_limit=300,
    soft_time_limit=290,
)
def cleanup_compressed_dlq_entries(self) -> dict:
    """
    Transition compressed entry lifecycle statuses.

    Celery Beat daily schedule:
    - ACTIVE entries older than compress_stale_after_days -> STALE
    - STALE entries older than compress_archive_after_days -> ARCHIVED
    - Never hard delete.
    """
    from datetime import timedelta

    from baldur.factory.registry import ProviderRegistry
    from baldur.settings.dlq import get_dlq_settings

    task_id = self.request.id or "unknown"
    bound_logger = logger.bind(task_id=task_id)

    settings = get_dlq_settings()
    repository = ProviderRegistry.dlq_repository.safe_get()
    if repository is None:
        raise RuntimeError("baldur_pro DLQRepository not registered")

    now = utc_now()
    stale_cutoff = now - timedelta(days=settings.compress_stale_after_days)
    archive_cutoff = now - timedelta(days=settings.compress_archive_after_days)

    from baldur.interfaces.repositories import DLQCompressedStatus

    def _drain(from_status: str, to_status: str, cutoff, is_eligible) -> int:
        """Transition entries older than ``cutoff``, oldest first, in pages.

        The cursor is the last ``compressed_at`` handled, carried forward as
        the next page's lower bound so the window shrinks instead of
        restarting at the oldest entry. That matters on Redis, where status is
        not part of the index: a scan from the head would re-read every entry
        the drain had already transitioned, once per page.

        A transition moves an entry out of ``from_status``, so it drops out of
        the query on its own. Entries that matched but failed ``is_eligible``
        stay, and those below the cursor fall outside the next window — only
        the ones sitting exactly on the cursor would come back, so those are
        counted into ``offset``.
        """
        transitioned = 0
        after = None
        offset = 0

        for _ in range(_COMPRESSED_DRAIN_MAX_ITERATIONS):
            page = repository.get_compressed_entries_before(
                status=from_status,
                before=cutoff,
                limit=_COMPRESSED_DRAIN_PAGE_SIZE,
                offset=offset,
                after=after,
            )
            if not page:
                break

            left_behind = []
            for entry in page:
                if not is_eligible(entry):
                    left_behind.append(entry)
                    continue
                repository.update_compressed_status(entry.id, to_status)
                transitioned += 1

            cursor = page[-1].compressed_at
            stuck = sum(1 for e in left_behind if e.compressed_at == cursor)
            # The cursor only fails to advance when a whole page shares one
            # timestamp; then the skips accumulate rather than reset.
            offset = offset + stuck if after == cursor else stuck
            after = cursor

        return transitioned

    # ACTIVE -> STALE. The cutoff query already encodes eligibility.
    stale_count = _drain(
        DLQCompressedStatus.ACTIVE.value,
        DLQCompressedStatus.STALE.value,
        stale_cutoff,
        lambda entry: True,
    )

    # STALE -> ARCHIVED. Archiving is driven off stale_at, but compressed_at is
    # always earlier than stale_at, so the compressed_at cutoff yields a
    # superset of the eligible entries — the per-entry check narrows it.
    archived_count = _drain(
        DLQCompressedStatus.STALE.value,
        DLQCompressedStatus.ARCHIVED.value,
        archive_cutoff,
        lambda entry: entry.stale_at is not None and entry.stale_at < archive_cutoff,
    )

    bound_logger.info(
        "dlq.compressed_cleanup_completed",
        stale_count=stale_count,
        archived_count=archived_count,
    )

    return {
        "success": True,
        "stale_count": stale_count,
        "archived_count": archived_count,
    }


@shared_task(
    bind=True,
    name="baldur.celery_tasks.release_stale_replaying",
    queue="maintenance",
    max_retries=1,
    time_limit=120,
    soft_time_limit=110,
)
def release_stale_replaying(self) -> dict:
    """
    Release DLQ entries stuck in REPLAYING state back to PENDING.

    Entries get stuck if the replay worker crashes after acquiring but
    before completing. Runs every 15 minutes via Celery Beat.

    Returns:
        Dictionary with released count
    """
    from baldur.factory.registry import ProviderRegistry
    from baldur.settings.dlq import get_dlq_settings

    task_id = self.request.id or "unknown"
    bound_logger = logger.bind(task_id=task_id)

    settings = get_dlq_settings()
    repository = ProviderRegistry.dlq_repository.safe_get()
    if repository is None:
        raise RuntimeError("baldur_pro DLQRepository not registered")

    try:
        released = repository.release_stale_replaying(
            older_than_minutes=settings.stale_replaying_timeout_minutes,
        )

        if released > 0:
            bound_logger.warning(
                "dlq.stale_replaying_released",
                released_count=released,
                timeout_minutes=settings.stale_replaying_timeout_minutes,
            )
        else:
            bound_logger.debug(
                "dlq.stale_replaying_none_found",
                timeout_minutes=settings.stale_replaying_timeout_minutes,
            )

        return {
            "success": True,
            "released_count": released,
        }

    except Exception as e:
        bound_logger.exception(
            "dlq.release_stale_replaying_error",
            error=e,
        )
        return {
            "success": False,
            "error": str(e),
        }


def get_dlq_maintenance_beat_schedule():
    """Beat schedule for DLQ maintenance tasks (eviction + cleanup + stale release)."""
    from celery.schedules import crontab

    return {
        "evict-overflow-dlq-entries": {
            "task": "baldur.celery_tasks.evict_overflow_dlq_entries",
            "schedule": 60.0,
            "options": {"queue": "maintenance"},
        },
        "cleanup-resolved-dlq-entries": {
            "task": "baldur.celery_tasks.cleanup_resolved_dlq_entries",
            "schedule": crontab(hour="*/6"),
            "options": {"queue": "maintenance"},
            "kwargs": {"days_old": 30},
        },
        "release-stale-replaying-entries": {
            "task": "baldur.celery_tasks.release_stale_replaying",
            "schedule": crontab(minute="*/15"),
            "options": {"queue": "maintenance"},
        },
        "cleanup-compressed-dlq-entries": {
            "task": "baldur.celery_tasks.cleanup_compressed_dlq_entries",
            "schedule": crontab(hour=4, minute=30),
            "options": {"queue": "maintenance"},
        },
    }
