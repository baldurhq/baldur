"""
Drift Detection Celery Tasks

Tasks for SLA drift detection and chaos experiment cleanup.
These tasks use dependency injection for Django model access.
"""

from __future__ import annotations

from typing import Any

import structlog
from celery import shared_task

from baldur.audit.helpers import log_drift_detection_audit

logger = structlog.get_logger(__name__)


# =============================================================================
# Dependency Injection Helpers
# =============================================================================


def _get_sla_thresholds():
    """Get SLA thresholds via the layered provider.

    Layered read (686 D1/D5) so a console edit of the sla domain (default_hours)
    is observed by the SLA drift check; falls back to the env base when no
    RuntimeConfigManager is registered. This is the callable bootstrap injects
    into SLADriftDetector, so swapping it here covers both the Celery task and
    the scheduler tick.
    """
    from baldur.settings.layered_provider import get_layered_settings
    from baldur.settings.sla import SLASettings

    return get_layered_settings(SLASettings, "sla")


def _get_failed_operations_factory():
    """
    Get a factory function for querying failed operations.

    This uses late binding to avoid import errors when Django isn't configured.
    Host applications can override this via baldur configuration.
    """

    def query_fn(**kwargs):
        try:
            # Try Django ORM first
            from django.apps import apps

            if apps.ready:
                from baldur.adapters.django.models import (
                    get_failed_operation_model,
                )

                model = get_failed_operation_model()
                if model:
                    return model.objects.filter(**kwargs)
        except Exception:
            pass

        # Fallback: return empty list
        return []

    return query_fn


def _record_sla_breach(domain: str):
    """Record SLA breach metric."""
    try:
        from baldur.services.metrics.recorders import record_sla_breach

        record_sla_breach(domain)
    except Exception:
        pass


def _resolve_expired_chaos_experiments() -> int:
    """
    Resolve expired chaos experiments.

    Uses late binding for Django model access.
    """
    try:
        from baldur.adapters.django.chaos_cleanup import (
            resolve_expired_chaos_experiments,
        )

        return resolve_expired_chaos_experiments()
    except ImportError:
        logger.debug("drift_detection.django_adapter_available_skipping")
        return 0


# =============================================================================
# Celery Tasks
# =============================================================================


@shared_task(
    bind=True,
    name="baldur.celery_tasks.check_sla_drift",
    queue="maintenance",
    max_retries=1,
    time_limit=120,
    soft_time_limit=110,
)
def check_sla_drift(self) -> dict[str, Any]:
    """
    Compare configured SLA thresholds with actual recovery metrics.

    This task:
    1. Loads SLA thresholds from configuration
    2. Queries actual recovery times from database
    3. Generates SLADriftWarning if actual performance deviates from SLA

    IMPORTANT: This task ONLY generates warnings.
    It NEVER modifies system configuration.

    Returns:
        Dictionary with drift detection results
    """
    from baldur.tasks.drift_detection import SLADriftDetector

    task_id = self.request.id or "unknown"

    try:
        detector = SLADriftDetector(
            get_sla_thresholds=_get_sla_thresholds,
            get_failed_operations=_get_failed_operations_factory(),
            record_sla_breach=_record_sla_breach,
        )

        result = detector.check_drift()

        # Audit logging
        log_drift_detection_audit(
            check_type="sla_drift",
            status=result.get("status", "completed"),
            drift_detected=result.get("drift_detected", False),
            drift_details=result.get("drift_details"),
            operations_analyzed=result.get("operations_analyzed", 0),
            task_id=task_id,
        )

        return result

    except Exception as e:
        # Audit logging (failure)
        log_drift_detection_audit(
            check_type="sla_drift",
            status="error",
            error_message=str(e),
            task_id=task_id,
        )
        raise


@shared_task(
    bind=True,
    name="baldur.celery_tasks.cleanup_expired_chaos_experiments",
    queue="maintenance",
    max_retries=1,
    time_limit=60,
    soft_time_limit=55,
)
def cleanup_expired_chaos_experiments(self) -> dict[str, Any]:
    """
    Clean up expired chaos experiments by auto-resolving them.

    This task runs periodically to:
    1. Find chaos experiment entries that have expired
    2. Auto-resolve them if auto_resolve=True

    Returns:
        Dictionary with cleanup results
    """
    from baldur.tasks.drift_detection import ChaosExperimentCleaner

    task_id = self.request.id or "unknown"

    try:
        cleaner = ChaosExperimentCleaner(
            resolve_expired_experiments=_resolve_expired_chaos_experiments,
        )

        result = cleaner.cleanup()

        # Audit logging
        log_drift_detection_audit(
            check_type="chaos_cleanup",
            status=result.get("status", "completed"),
            task_id=task_id,
            details={
                "resolved_count": result.get("resolved_count", 0),
            },
        )

        return result

    except Exception as e:
        # Audit logging (failure)
        log_drift_detection_audit(
            check_type="chaos_cleanup",
            status="error",
            error_message=str(e),
            task_id=task_id,
        )
        raise
