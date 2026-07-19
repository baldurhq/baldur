"""
Postmortem Celery Tasks.

Periodic postmortem maintenance tasks.

Celery Beat schedule:
    - postmortem_auto_seal: daily (86400s)

The composed schedule injects this lane's entry, but postmortem_auto_seal
carries no task registration, so a worker cannot resolve the name and the lane
does not currently run.

Usage:
    # Normally unnecessary: configure_baldur_celery(app) merges this lane.
    CELERY_BEAT_SCHEDULE.update(get_postmortem_beat_schedule())
"""

from __future__ import annotations

from typing import Any

import structlog

__all__ = ["postmortem_auto_seal", "get_postmortem_beat_schedule"]

logger = structlog.get_logger()


def postmortem_auto_seal() -> dict[str, Any]:
    """Auto-seal postmortems that exceed auto_seal_days.

    Delegates to PostmortemRevisionManager.check_and_auto_seal().
    Scheduled via Celery Beat (daily).

    Returns:
        Execution result dict.
    """
    try:
        from baldur_pro.services.postmortem.revision import (
            get_postmortem_revision_manager,
        )
    except ImportError:
        get_postmortem_revision_manager = None  # type: ignore[assignment,misc]

    try:
        manager = get_postmortem_revision_manager()
        sealed_ids = manager.check_and_auto_seal()

        return {
            "status": "completed",
            "sealed_count": len(sealed_ids),
            "sealed_ids": sealed_ids,
        }
    except Exception as e:
        logger.exception(
            "postmortem_tasks.auto_seal_failed",
            error=str(e),
        )
        return {
            "status": "failed",
            "error": str(e),
        }


def get_postmortem_beat_schedule() -> dict[str, dict[str, Any]]:
    """Get Celery Beat schedule for postmortem tasks.

    Returns:
        Celery Beat schedule configuration.
    """
    return {
        "postmortem-auto-seal": {
            "task": "baldur.tasks.postmortem_tasks.postmortem_auto_seal",
            "schedule": 86400.0,  # daily
        },
    }
