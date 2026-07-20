"""
X-Test Artifact Cleanup Celery Tasks

Celery tasks that automatically clean up test artifacts after an X-Test session
ends.

Thin Task, Fat Service principle:
- The functions in this file act purely as delegators
- All business logic lives in XTestCleanupService

Tasks:
1. cleanup_xtest_artifacts - clean up expired X-Test sessions and artifacts

Schedule:
- Runs every 30 minutes (configurable)
"""

from __future__ import annotations

from typing import Any

import structlog

logger = structlog.get_logger()


# =============================================================================
# Thin Task Wrappers
# =============================================================================


def cleanup_xtest_artifacts() -> dict[str, Any]:
    """
    Clean up expired X-Test sessions and their artifacts.

    Thin wrapper delegating to XTestCleanupService.

    Cleanup targets:
    - Expired X-Test session metadata
    - Circuit Breaker xtest_mode state restoration
    - DLQ x-test-mode entry deletion
    - Idempotency xtest key deletion
    - Rate Limit xtest counter reset
    - Scenario result cleanup

    Returns:
        dict: {
            "success": bool,
            "sessions_cleaned": int,
            "cb_states_restored": int,
            "dlq_entries_purged": int,
            "idempotency_keys_cleared": int,
            "rate_limit_counters_reset": int,
            "scenario_results_cleared": int,
            "errors": list,
        }
    """
    from baldur.services.xtest_cleanup_service import get_xtest_cleanup_service

    try:
        service = get_xtest_cleanup_service()
        result = service.cleanup_expired_sessions()

        logger.info(
            "x_test_cleanup_task.completed",
            sessions_cleaned=result.sessions_cleaned,
            cb_states_restored=result.cb_states_restored,
            dlq_entries_purged=result.dlq_entries_purged,
            idempotency_keys_cleared=result.idempotency_keys_cleared,
        )

        return result.to_dict()

    except Exception as e:
        logger.exception(
            "x_test_cleanup_task.failed",
            error=e,
        )
        raise


def get_xtest_cleanup_stats() -> dict[str, Any]:
    """
    Get statistics about the X-Test cleanup targets.

    Returns:
        dict: cleanup target statistics
    """
    from baldur.services.xtest_cleanup_service import get_xtest_cleanup_service

    try:
        service = get_xtest_cleanup_service()
        return service.get_cleanup_stats()

    except Exception as e:
        logger.exception(
            "x_test_cleanup_task.failed",
            error=e,
        )
        return {"error": str(e)}


# =============================================================================
# Celery Task Registration
# =============================================================================

try:
    from celery import shared_task

    from baldur.settings.xtest_cleanup import get_xtest_cleanup_settings

    # Cache the settings at module load time
    _xtest_cleanup_settings = get_xtest_cleanup_settings()

    @shared_task(
        name="baldur.cleanup_xtest_artifacts",
        bind=True,
        max_retries=_xtest_cleanup_settings.max_retries,
        default_retry_delay=_xtest_cleanup_settings.retry_delay,
    )
    def cleanup_xtest_artifacts_task(self):
        """Celery task wrapper for cleanup_xtest_artifacts."""
        return cleanup_xtest_artifacts()

    @shared_task(
        name="baldur.get_xtest_cleanup_stats",
        bind=True,
    )
    def get_xtest_cleanup_stats_task(self):
        """Celery task wrapper for get_xtest_cleanup_stats."""
        return get_xtest_cleanup_stats()

    CELERY_TASKS_AVAILABLE = True

except ImportError:
    logger.debug("x_test_cleanup_tasks.celery_available_skipping_task")
    CELERY_TASKS_AVAILABLE = False


# =============================================================================
# Beat Schedule definition
# =============================================================================


def get_xtest_cleanup_beat_schedule() -> dict[str, Any]:
    """
    Return the X-Test cleanup Beat schedule.

    Returns:
        dict: Celery Beat schedule configuration
    """
    try:
        from celery.schedules import crontab

        from baldur.settings.xtest_cleanup import get_xtest_cleanup_settings

        settings = get_xtest_cleanup_settings()
        interval_minutes = settings.cleanup_interval_minutes

        return {
            # Every 30 minutes (default) - X-Test artifact cleanup
            "cleanup-xtest-artifacts": {
                "task": "baldur.cleanup_xtest_artifacts",
                "schedule": crontab(minute=f"*/{interval_minutes}"),
                "options": {"queue": "maintenance"},
            },
        }

    except ImportError:
        logger.debug("x_test_cleanup_tasks.celery_available_beat_schedule")
        return {}


__all__ = [
    # Thin wrapper functions
    "cleanup_xtest_artifacts",
    "get_xtest_cleanup_stats",
    # Beat schedule
    "get_xtest_cleanup_beat_schedule",
    # Celery availability flag
    "CELERY_TASKS_AVAILABLE",
]
