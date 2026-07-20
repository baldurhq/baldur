"""
Celery task that sends SLA notifications asynchronously.

Handles SLA violation events (warning/critical/recovered) asynchronously.
autoretry retries automatically on delivery failure (max 3 attempts, 30s
apart), and acks_late returns unfinished tasks to the broker when a worker
shuts down.
"""

from __future__ import annotations

from typing import Any

import structlog
from celery import shared_task

logger = structlog.get_logger(__name__)


@shared_task(
    bind=True,
    name="baldur.adapters.celery.tasks.send_sla_notification",
    queue="baldur",
    autoretry_for=(Exception,),
    max_retries=3,
    default_retry_delay=30,
    acks_late=True,
    time_limit=60,
    soft_time_limit=55,
)
def send_sla_notification(
    self,
    event_data: dict,
    notification_type: str,
) -> dict[str, Any]:
    """
    Task that sends an SLA notification asynchronously.

    Args:
        event_data: SLA event data (rtt_ms, threshold_ms, service_name, etc.)
        notification_type: Notification kind ("warning", "critical",
            "recovered")

    Returns:
        Delivery result dictionary
    """
    try:
        from baldur_pro.services.throttle.sla_notification import (
            _send_limit_recovered_sync,
            _send_sla_critical_sync,
            _send_sla_warning_sync,
        )
    except ImportError:
        _send_limit_recovered_sync = None  # type: ignore[assignment,misc]
        _send_sla_critical_sync = None  # type: ignore[assignment,misc]
        _send_sla_warning_sync = None  # type: ignore[assignment,misc]

    dispatch = {
        "warning": _send_sla_warning_sync,
        "critical": _send_sla_critical_sync,
        "recovered": _send_limit_recovered_sync,
    }

    handler = dispatch.get(notification_type)
    if handler:
        handler(event_data)
        logger.info(
            "send_sla_notification.sent_notification_attempt",
            notification_type=notification_type,
            retry_attempt=self.request.retries + 1,
        )
    else:
        logger.warning(
            "send_sla_notification.unknown_notification_type",
            notification_type=notification_type,
        )

    return {"status": "sent", "type": notification_type}
