"""
Daily Autonomous Report Task

Thin Task, Fat Service Pattern:
- Task only handles service invocation
- All business logic lives in the services/daily_report/ package

Schedule: daily at 09:00
Queue: reports
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import structlog

logger = structlog.get_logger()


# =============================================================================
# Thin Wrapper Functions (delegate to service)
# =============================================================================


def generate_daily_autonomous_report(
    date: datetime | None = None,
    channels: list[str] | None = None,
) -> dict[str, Any]:
    """
    Generate and send daily autonomous operations report.

    This is a Thin Wrapper that delegates to DailyReportService.

    Args:
        date: Report date (default: yesterday)
        channels: Notification channels (default: ["slack"])

    Returns:
        dict: Report generation result
    """
    from baldur.settings.daily_report import get_daily_report_settings

    if not get_daily_report_settings().enabled:
        logger.debug("daily_report.disabled")
        return {"status": "disabled", "reason": "Daily report feature is disabled"}

    from baldur.services.daily_report import get_daily_report_service

    service = get_daily_report_service()
    result = service.generate_and_send_report(date=date, channels=channels)
    return result.to_dict()


# =============================================================================
# Celery Task Registration
# =============================================================================

try:
    from celery import shared_task

    from baldur.settings.daily_report import get_daily_report_settings

    # Cache settings at module-load time for task decorator parameters
    _daily_report_settings = get_daily_report_settings()

    @shared_task(
        name="baldur.tasks.daily_report.generate_daily_autonomous_report",
        bind=False,
        max_retries=_daily_report_settings.max_retries,
        default_retry_delay=_daily_report_settings.retry_delay,
    )
    def generate_daily_autonomous_report_task(
        date_str: str | None = None,
        channels: list[str] | None = None,
    ) -> dict[str, Any]:
        """
        Celery task wrapper for daily report generation.

        Args:
            date_str: ISO format date string (default: yesterday)
            channels: Notification channels
        """
        date = None
        if date_str:
            date = datetime.fromisoformat(date_str)

        return generate_daily_autonomous_report(date=date, channels=channels)

except ImportError:
    logger.debug("daily_report.celery_available_skipping_task")


# =============================================================================
# Beat Schedule
# =============================================================================


def get_daily_report_beat_schedule() -> dict[str, dict[str, Any]]:
    """
    Get Celery Beat schedule for daily report task.

    Returns:
        dict: Celery Beat schedule configuration

    Usage:
        from baldur.tasks.daily_report import get_daily_report_beat_schedule
        CELERY_BEAT_SCHEDULE.update(get_daily_report_beat_schedule())
    """
    from baldur.settings.daily_report import get_daily_report_settings

    settings = get_daily_report_settings()
    return {
        "generate-daily-autonomous-report": {
            "task": "baldur.tasks.daily_report.generate_daily_autonomous_report",
            "schedule": {
                "hour": settings.default_hour,
                "minute": settings.default_minute,
            },
            "options": {
                "queue": "reports",
            },
        },
    }


# =============================================================================
# Backward Compatibility Re-exports (Lazy Import)
# =============================================================================


def __getattr__(name: str):
    """Lazy import for backward compatibility types."""
    _lazy_imports = {
        "TaskResultEntry",
        "DailyAutonomousReport",
        "DailyReportData",
        "DailyReportCollector",
        "get_daily_report_collector",
        "DAILY_REPORT_CACHE_KEY_PREFIX",
    }
    if name in _lazy_imports:
        from baldur.services import daily_report as dr_module

        return getattr(dr_module, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    # Main functions
    "generate_daily_autonomous_report",
    "generate_daily_autonomous_report_task",
    "get_daily_report_beat_schedule",
    # Backward compatibility exports
    "TaskResultEntry",
    "DailyAutonomousReport",
    "DailyReportData",
    "DailyReportCollector",
    "get_daily_report_collector",
    "DAILY_REPORT_CACHE_KEY_PREFIX",
]
