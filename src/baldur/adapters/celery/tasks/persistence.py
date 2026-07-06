"""
Async Persistence Tasks for Hybrid Storage Support.

These tasks handle asynchronous persistence of DLQ entries to the statistics store
(ORM) without blocking the critical path (Redis).

Design Principle (07_HYBRID_STORAGE_ARCHITECTURE.md):
- Runtime (Redis): Fast, synchronous, 1-2ms
- Statistics (ORM): Async, can tolerate 10-100ms

Usage:
    Add these tasks to Celery:

    # Triggered by DLQ store operations
    async_persist_dlq_entry.delay(entry_data)

    # Batch sync from AuditMiddleware
    async_persist_batch.delay(entries)
"""

from typing import Any

import structlog
from celery import shared_task

logger = structlog.get_logger(__name__)


@shared_task(
    bind=True,
    name="baldur.adapters.celery.tasks.async_persist_dlq_entry",
    queue="persistence",
    max_retries=3,
    time_limit=30,
    soft_time_limit=25,
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=60,
)
def async_persist_dlq_entry(self, entry_data: dict[str, Any]) -> dict:
    """
    Asynchronously persist a DLQ entry to the statistics store.

    This task is triggered when DLQ entries are created in Redis,
    ensuring ORM persistence doesn't block the critical path.

    Args:
        entry_data: DLQ entry data from Redis

    Returns:
        Dictionary with persistence result
    """
    logger.debug(
        "async_persist.persisting_dlq_entry",
        entry_data=entry_data.get("id", "unknown"),
    )

    try:
        from baldur.factory import ProviderRegistry

        if not ProviderRegistry.has_statistics_adapter():
            logger.debug("async_persist.stats_adapter_unavailable")
            return {
                "success": True,
                "skipped": True,
                "reason": "no_statistics_adapter",
            }

        stats_repo = ProviderRegistry.get_statistics_repo()
        entry_id = stats_repo.persist_entry(entry_data)

        if entry_id:
            logger.info("async_persist.entry_persisted", entry_id=entry_id)
            return {
                "success": True,
                "entry_id": entry_id,
            }
        logger.warning("async_persist.entry_returned_none")
        return {
            "success": False,
            "error": "persist_returned_none",
        }

    except Exception as e:
        logger.exception(
            "async_persist.persist_dlq_entry_failed",
            error=e,
        )
        raise  # Re-raise for Celery retry


@shared_task(
    bind=True,
    name="baldur.adapters.celery.tasks.async_persist_batch",
    queue="persistence",
    max_retries=2,
    time_limit=120,
    soft_time_limit=110,
)
def async_persist_batch(self, entries: list[dict[str, Any]]) -> dict:
    """
    Batch persist DLQ entries to the statistics store.

    Used for bulk sync from Redis to ORM, typically called from
    AuditMiddleware's batch flush or periodic sync tasks.

    Args:
        entries: List of DLQ entry data

    Returns:
        Dictionary with batch persistence result
    """
    logger.info(
        "async_persist.batch_persisting_entries",
        entries_count=len(entries),
    )

    try:
        from baldur.factory import ProviderRegistry

        if not ProviderRegistry.has_statistics_adapter():
            return {
                "success": True,
                "skipped": True,
                "count": 0,
                "reason": "no_statistics_adapter",
            }

        stats_repo = ProviderRegistry.get_statistics_repo()
        synced = stats_repo.sync_from_runtime(entries)

        logger.info(
            "async_persist.batch_persisted_entries",
            synced=synced,
            entries_count=len(entries),
        )
        return {
            "success": True,
            "synced": synced,
            "total": len(entries),
        }

    except Exception as e:
        logger.exception(
            "async_persist.batch_persist_failed",
            error=e,
        )
        return {
            "success": False,
            "error": str(e),
        }


@shared_task(
    bind=True,
    name="baldur.adapters.celery.tasks.link_audit_to_dlq",
    queue="persistence",
    max_retries=2,
    time_limit=30,
    soft_time_limit=25,
)
def link_audit_to_dlq(
    self,
    entity_id: str,
    entity_type: str,
    action: str,
    actor_id: str | None = None,
    status: str | None = None,
    details: str | None = None,
    audit_record_hash: str | None = None,
) -> dict:
    """
    Link an audit record to a DLQ entity.

    Creates the relationship between DLQ entries and their audit trail,
    enabling the "Master Trail" feature for technical due diligence.

    Args:
        entity_id: DLQ entry ID
        entity_type: Entity type (usually "dlq_entry")
        action: Action performed (store, replay, resolve, etc.)
        actor_id: Who performed the action
        status: New status after action
        details: Additional details
        audit_record_hash: Hash from audit system for chain verification

    Returns:
        Dictionary with link result
    """
    logger.debug(
        "audit_link.linking_audit",
        entity_type=entity_type,
        entity_id=entity_id,
    )

    try:
        from baldur.factory import ProviderRegistry

        if not ProviderRegistry.has_statistics_adapter():
            return {"success": True, "skipped": True}

        stats_repo = ProviderRegistry.get_statistics_repo()
        success = stats_repo.link_audit_entry(
            entity_id=entity_id,
            entity_type=entity_type,
            action=action,
            actor_id=actor_id,
            status=status,
            details=details,
            audit_record_hash=audit_record_hash,
        )

        return {"success": success}

    except Exception as e:
        logger.exception(
            "audit_link.failed",
            error=e,
        )
        return {"success": False, "error": str(e)}
