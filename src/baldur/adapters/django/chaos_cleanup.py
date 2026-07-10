"""
Django ORM adapter for resolving expired chaos experiments.

Queries FailedOperation entries that have chaos experiment metadata
with expired TTLs and auto-resolves them.

Usage:
    from baldur.adapters.django.chaos_cleanup import (
        resolve_expired_chaos_experiments,
    )

    resolved_count = resolve_expired_chaos_experiments()
"""

from __future__ import annotations

import structlog

from baldur.services.chaos_context import (
    ChaosExperimentContext,
    ChaosExperimentStatus,
    is_chaos_experiment,
)
from baldur.utils.time import utc_now

logger = structlog.get_logger()


def resolve_expired_chaos_experiments() -> int:
    """
    Find and resolve expired chaos experiments in the DLQ.

    Scans FailedOperation entries for chaos experiment metadata
    where the experiment has expired and auto_resolve is True.
    Marks them as resolved with an appropriate resolution note.

    Returns:
        Number of experiments resolved.
    """
    try:
        from baldur.adapters.django.models import FailedOperation
    except ImportError:
        logger.debug("chaos_cleanup.django_models_unavailable")
        return 0

    resolved_count = 0
    current_time = utc_now()

    try:
        # Query pending/failed operations that might be chaos experiments.
        # Chaos experiment entries store context in the metadata JSON field.
        pending_ops = (
            FailedOperation.objects.filter(
                status__in=["pending", "failed"],
            )
            .exclude(
                metadata={},
            )
            .exclude(
                metadata__isnull=True,
            )
        )

        for operation in pending_ops.iterator():
            if not is_chaos_experiment(operation):
                continue

            chaos_data = operation.metadata.get("chaos_experiment_context", {})
            context = ChaosExperimentContext.from_dict(chaos_data)

            # Skip if not expired or auto_resolve is disabled
            if not context.is_expired():
                continue
            if not context.auto_resolve:
                continue

            # Resolve the operation
            try:
                context.status = ChaosExperimentStatus.EXPIRED.value
                context.resolved_at = current_time.isoformat()
                context.resolution_note = "Auto-resolved: chaos experiment TTL expired"

                operation.metadata["chaos_experiment_context"] = context.to_dict()
                operation.status = "resolved"
                operation.resolution_type = "auto_resolved"
                operation.resolution_note = (
                    f"Chaos experiment {context.experiment_id} expired "
                    f"(TTL: {context.expected_duration_seconds}s)"
                )
                operation.resolved_at = current_time
                operation.save(
                    update_fields=[
                        "metadata",
                        "status",
                        "resolution_type",
                        "resolution_note",
                        "resolved_at",
                    ]
                )

                resolved_count += 1

                logger.debug(
                    "chaos_cleanup.experiment_resolved",
                    experiment_id=context.experiment_id,
                    operation_id=operation.id,
                )

            except Exception as e:
                logger.warning(
                    "chaos_cleanup.resolve_failed",
                    operation_id=operation.id,
                    error=str(e),
                )

    except Exception as e:
        logger.warning(
            "chaos_cleanup.query_failed",
            error=str(e),
        )

    if resolved_count > 0:
        logger.info(
            "chaos_cleanup.completed",
            resolved_count=resolved_count,
        )

    return resolved_count


__all__ = [
    "resolve_expired_chaos_experiments",
]
