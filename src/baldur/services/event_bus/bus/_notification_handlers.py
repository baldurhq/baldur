"""
Notification event handlers — structured logging for delivery failures.

Handles:
- NOTIFICATION_DELIVERY_FAILED
"""

from __future__ import annotations

import structlog

from . import BaldurEvent

logger = structlog.get_logger()


def _on_notification_delivery_failed(event: BaldurEvent) -> None:
    """Handle NOTIFICATION_DELIVERY_FAILED event.

    Provides structured logging for the EventBus audit trail.
    Metric recording is handled separately by the NotificationMetricRecorder
    at the call site (_send_to_channels), following DailyReport precedent.
    """
    logger.warning(
        "unified_notification.delivery_failed",
        channel=event.data.get("channel"),
        priority=event.data.get("priority"),
        error=event.data.get("error"),
        source=event.data.get("source"),
        title=event.data.get("title"),
    )
