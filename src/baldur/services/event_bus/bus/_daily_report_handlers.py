"""
Daily Report event handlers — structured logging + metrics.

Handles:
- DAILY_REPORT_SEND_FAILED
"""

from __future__ import annotations

import structlog

from . import BaldurEvent

logger = structlog.get_logger()


def _on_daily_report_send_failed(event: BaldurEvent) -> None:
    """Handle DAILY_REPORT_SEND_FAILED event.

    Metric recording is intentionally omitted here — the DailyReportService
    already calls record_delivery(channel, False) before emitting this event.
    This handler only provides structured logging for the EventBus audit trail.
    """
    logger.warning(
        "daily_report.send_failed",
        channel=event.data.get("channel"),
        error=event.data.get("error"),
        date=event.data.get("date"),
    )
