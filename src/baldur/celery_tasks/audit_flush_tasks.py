"""Audit-buffer drain Celery tasks.

Safely drains the Redis audit buffer to the configured audit sink.

The whole pipeline is gated behind the effective drain switch (master audit
``enabled`` AND ``buffer_redis_enabled``). When the gate is off every task
early-exits issuing zero Redis commands, so writer-less deployments pay no
cost. Concurrent flushes are prevented by a distributed lock (ImportError
fail-open for OSS single-worker), and the Processing-Queue pattern guards
against data loss.

Tasks (queue ``audit_flush``):
- ``flush_redis_audit_buffer`` — buffer -> audit sink (Processing-Queue safe)
- ``recover_orphaned_processing_queues`` — restore timed-out processing queues
- ``apply_audit_buffer_safety_ltrim`` — trim oversized buffers

Registration is import-driven: the ``@shared_task`` decorators register against
Celery on import. The package ``__init__`` imports this module so Django
autodiscover registers the tasks, and plain-Celery wiring imports it during
beat-schedule composition (``include_audit_flush`` resolution) — so beat
injection and task registration stay structurally in sync.
"""

from __future__ import annotations

import time
from datetime import timedelta
from typing import Any

import structlog
from celery import shared_task

from baldur.adapters.audit.redis_buffer import get_redis_audit_buffer

logger = structlog.get_logger(__name__)


def _drain_disabled_result(task_id: str) -> dict[str, Any]:
    """Uniform early-exit payload when the effective drain gate is off."""
    return {
        "status": "disabled",
        "reason": "drain_gate_disabled",
        "task_id": task_id,
    }


@shared_task(
    bind=True,
    name="baldur.celery_tasks.flush_redis_audit_buffer",
    queue="audit_flush",
    max_retries=3,
    default_retry_delay=30,
    time_limit=300,
    soft_time_limit=290,
    acks_late=True,
)
def flush_redis_audit_buffer(  # noqa: C901, PLR0915
    self,
    batch_size: int | None = None,
) -> dict:
    """Drain the Redis audit buffer to the configured audit sink.

    A distributed lock prevents concurrent flushes (multi-worker beat
    overlap); the Processing-Queue pattern prevents data loss. Flushed
    entries join the host's audit stream via the registry-resolved adapter.

    Args:
        batch_size: Per-domain batch size. None reads AuditSettings.

    Returns:
        Result dict.
    """
    from baldur.settings.audit import is_redis_drain_enabled

    task_id = self.request.id or "unknown"
    bound_logger = logger.bind(task_id=task_id)

    # Gate early-exit FIRST — zero Redis commands when the drain is disabled.
    if not is_redis_drain_enabled():
        bound_logger.debug("audit_flush.flush_disabled")
        return _drain_disabled_result(task_id)

    start_time = time.time()

    if batch_size is None:
        from baldur.settings.audit import get_audit_settings

        batch_size = get_audit_settings().buffer_redis_batch_size

    # Distributed lock acquisition
    lock_acquired = False
    lock = None
    lock_namespace = "audit-buffer-flush"
    session_id = f"celery-{task_id}"

    try:
        from baldur_pro.services.coordination.distributed_recovery_lock import (
            DistributedRecoveryLock,
        )

        lock = DistributedRecoveryLock(lock_timeout=timedelta(minutes=5))
        lock_acquired = lock.acquire(
            namespace=lock_namespace,
            session_id=session_id,
            blocking=False,
        )

    except ImportError:
        # Proceed without a distributed lock (single-worker environment).
        bound_logger.warning("redis_audit_buffer.distributed_lock_unavailable")
        lock_acquired = True
    except Exception as e:
        bound_logger.exception(
            "flush_redis_audit_buffer.lock_acquisition_error",
            error=e,
        )
        lock_acquired = True  # Fail-open

    if not lock_acquired:
        bound_logger.info(
            "audit_flush.flush_skipped_lock_unacquired",
            reason="lock_not_acquired",
        )
        return {
            "status": "skipped",
            "reason": "lock_not_acquired",
            "task_id": task_id,
        }

    try:
        # Acquire the process-lifetime drain buffer (D1 accessor).
        redis_buffer = get_redis_audit_buffer()
        if redis_buffer is None:
            return {
                "status": "error",
                "reason": "redis_buffer_not_available",
                "task_id": task_id,
            }

        # Resolve the canonical audit sink (registry default may be null).
        from baldur.adapters.audit.null_adapter import NullAuditLogAdapter
        from baldur.factory import ProviderRegistry

        target_adapter = ProviderRegistry.get_audit_adapter()
        if isinstance(target_adapter, NullAuditLogAdapter):
            # No real audit sink registered — skip the flush so entries stay in
            # Redis under the TTL backstop instead of being silently discarded.
            bound_logger.warning(
                "audit_flush.flush_blocked",
                reason="null_target_adapter",
            )
            return {
                "status": "blocked",
                "reason": "null_target_adapter",
                "task_id": task_id,
            }

        flushed_count = redis_buffer.flush_to_external_safe(
            target_adapter=target_adapter,
            batch_size=batch_size,
        )

        duration_ms = (time.time() - start_time) * 1000

        bound_logger.info(
            "audit_flush.redis_buffer_flushed",
            flushed_count=flushed_count,
            duration_ms=round(duration_ms, 1),
        )

        # Record metrics
        try:
            from baldur.metrics.audit_buffer_metrics import record_flush

            record_flush("all", flushed_count)
        except ImportError:
            pass

        return {
            "status": "success",
            "flushed_count": flushed_count,
            "duration_ms": round(duration_ms, 1),
            "task_id": task_id,
        }

    except Exception as e:
        bound_logger.exception(
            "flush_redis_audit_buffer.failed",
            error=e,
        )

        # Retry
        raise self.retry(exc=e) from e

    finally:
        # Release the lock
        if lock is not None and lock_acquired:
            try:
                lock.release(namespace=lock_namespace, session_id=session_id)
            except Exception as e:
                bound_logger.warning(
                    "flush_redis_audit_buffer.lock_release_failed",
                    error=e,
                )


@shared_task(
    bind=True,
    name="baldur.celery_tasks.recover_orphaned_processing_queues",
    queue="audit_flush",
    max_retries=1,
    time_limit=120,
    soft_time_limit=110,
)
def recover_orphaned_processing_queues(
    self,
    timeout_seconds: int = 300,
) -> dict:
    """Recover timed-out orphaned processing queues.

    Restores items from processing queues that have been stuck for longer
    than the timeout back into the buffer.

    Args:
        timeout_seconds: Orphan threshold in seconds (default 5 minutes).

    Returns:
        Result dict.
    """
    from baldur.settings.audit import is_redis_drain_enabled

    task_id = self.request.id or "unknown"
    bound_logger = logger.bind(task_id=task_id)

    if not is_redis_drain_enabled():
        bound_logger.debug("audit_flush.recovery_disabled")
        return _drain_disabled_result(task_id)

    try:
        redis_buffer = get_redis_audit_buffer()
        if redis_buffer is None:
            return {
                "status": "error",
                "reason": "redis_buffer_not_available",
                "task_id": task_id,
            }

        recovered_total = redis_buffer.recover_orphaned_processing_queues(
            timeout_seconds=timeout_seconds
        )

        bound_logger.info(
            "audit_flush.orphaned_queues_recovered",
            recovered_total=recovered_total,
        )

        # Record metrics
        try:
            from baldur.metrics.audit_buffer_metrics import record_orphan_recovery

            record_orphan_recovery("all", recovered_total)
        except ImportError:
            pass

        return {
            "status": "success",
            "recovered_total": recovered_total,
            "task_id": task_id,
        }

    except Exception as e:
        bound_logger.exception(
            "recover_orphaned_processing_queues.failed",
            error=e,
        )
        return {
            "status": "error",
            "error": str(e),
            "task_id": task_id,
        }


@shared_task(
    bind=True,
    name="baldur.celery_tasks.apply_audit_buffer_safety_ltrim",
    queue="audit_flush",
    max_retries=0,
    time_limit=60,
)
def apply_audit_buffer_safety_ltrim(self) -> dict:
    """Apply a safety LTRIM to the audit buffer.

    Drops the oldest entries when a buffer exceeds its safety threshold.

    Returns:
        Result dict.
    """
    from baldur.settings.audit import is_redis_drain_enabled

    task_id = self.request.id or "unknown"
    bound_logger = logger.bind(task_id=task_id)

    if not is_redis_drain_enabled():
        bound_logger.debug("audit_flush.safety_ltrim_disabled")
        return _drain_disabled_result(task_id)

    try:
        redis_buffer = get_redis_audit_buffer()
        if redis_buffer is None:
            return {
                "status": "error",
                "reason": "redis_buffer_not_available",
                "task_id": task_id,
            }

        trimmed = redis_buffer.apply_safety_ltrim()

        total_trimmed = sum(trimmed.values())

        bound_logger.info(
            "audit_flush.safety_ltrim_applied",
            trimmed_domains=trimmed,
            total_trimmed=total_trimmed,
        )

        return {
            "status": "success",
            "trimmed_domains": trimmed,
            "total_trimmed": total_trimmed,
            "task_id": task_id,
        }

    except Exception as e:
        bound_logger.exception(
            "apply_audit_buffer_safety_ltrim.failed",
            error=e,
        )
        return {
            "status": "error",
            "error": str(e),
            "task_id": task_id,
        }


def get_audit_flush_beat_schedule() -> dict[str, Any]:
    """Celery Beat schedule for the audit-buffer drain tasks.

    The flush interval is read from ``AuditSettings`` at call time. The
    beat-injection gate (``include_audit_flush`` resolution in
    ``adapters/celery/beat_schedule.py``) decides whether these entries are
    injected at all; this getter only describes them.

    Returns:
        Beat schedule dict.
    """
    from baldur.settings.audit import get_audit_settings

    interval = get_audit_settings().buffer_redis_flush_interval

    return {
        "flush-redis-audit-buffer": {
            "task": "baldur.celery_tasks.flush_redis_audit_buffer",
            "schedule": interval,
            "options": {
                "queue": "audit_flush",
                "expires": interval * 3,
            },
        },
        "recover-orphaned-processing-queues": {
            "task": "baldur.celery_tasks.recover_orphaned_processing_queues",
            "schedule": 300.0,
            "options": {
                "queue": "audit_flush",
            },
        },
        "apply-audit-buffer-safety-ltrim": {
            "task": "baldur.celery_tasks.apply_audit_buffer_safety_ltrim",
            "schedule": 60.0,
            "options": {
                "queue": "audit_flush",
            },
        },
    }


__all__ = [
    "flush_redis_audit_buffer",
    "recover_orphaned_processing_queues",
    "apply_audit_buffer_safety_ltrim",
    "get_audit_flush_beat_schedule",
]
