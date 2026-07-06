"""OSS circuit-breaker notification delivery (out-of-seam, celery-free).

Home of the OSS-only CB open/close Slack push. Relocated here from the Celery
task module so BOTH delivery callers can share a single payload source:

- the Celery task ``send_cb_*_notification`` ``except ImportError`` branch
  (``baldur_pro`` absent, ``celery`` extra installed); and
- the EventBus notify handlers' ``except ImportError`` branch (core-only OSS
  install, no ``celery`` extra at all).

This module imports no ``celery`` symbol, so it is importable on a core-only
OSS install where ``baldur.celery_tasks.circuit_breaker_tasks`` is not. The push
it builds is the single sanctioned out-of-seam external-push exception to
the "OSS observes; PRO notifies" boundary; the notification registry seam
itself stays logging-only on OSS. Best-effort and fail-open throughout.

The two thin wrappers ``_send_cb_open_notification_oss`` /
``_send_cb_close_notification_oss`` own the OPEN / CLOSED payload literals
(title / message / priority / event type / metadata), so each event kind's
payload shape exists in exactly one place across both callers.
"""

from __future__ import annotations

import structlog

logger = structlog.get_logger(__name__)


def _send_cb_notification_oss(
    *,
    service_name: str,
    title: str,
    message: str,
    priority_name: str,
    event_type: str,
    timestamp: str,
    extra_metadata: dict | None = None,
) -> dict:
    """Deliver a CB notification on an OSS-only install, outside the seam.

    Reached on either the ``except ImportError`` branch of
    ``send_cb_*_notification`` (``baldur_pro`` absent) or the CB notify
    handlers' ``except ImportError`` branch (no ``celery`` extra). It constructs
    its own Slack transport directly from ``MetaWatchdogSettings.slack_webhook_url``
    — a configured URL builds a :class:`SlackWebhookNotificationAdapter`, an
    unset (or empty) URL falls back to a :class:`LoggingNotificationAdapter`
    ("OSS logs intent"). The notification registry seam is never resolved, so it
    stays logging-only on OSS (OSS observes; PRO notifies); the OSS circuit-breaker Slack push is
    the single sanctioned out-of-seam external-push exception. Best-effort and
    fail-open — ``adapter.send()`` never raises, so a delivery failure returns
    ``notification_sent=False`` without raising into the caller.
    """
    from baldur.interfaces.notification import (
        LoggingNotificationAdapter,
        NotificationAdapter,
    )
    from baldur.models.notification import (
        NotificationCategory,
        NotificationPayload,
        NotificationPriority,
    )
    from baldur.settings.meta_watchdog import get_meta_watchdog_settings

    from .webhook_adapter import SlackWebhookNotificationAdapter

    metadata = {
        "service_name": service_name,
        "event_type": event_type,
        "trigger_time": timestamp,
    }
    if extra_metadata:
        metadata.update(extra_metadata)

    payload = NotificationPayload(
        title=title,
        message=message,
        priority=NotificationPriority(priority_name),
        category=NotificationCategory.CIRCUIT_BREAKER,
        source="circuit_breaker_service",
        metadata=metadata,
    )

    webhook_url = get_meta_watchdog_settings().slack_webhook_url
    if webhook_url:
        adapter: NotificationAdapter = SlackWebhookNotificationAdapter(
            webhook_url=webhook_url
        )
    else:
        adapter = LoggingNotificationAdapter()
    sent = adapter.send(payload)
    logger.info(
        "send_cb_notification.oss_seam_dispatched",
        service_name=service_name,
        event_type=event_type,
        notification_sent=sent,
        channel=adapter.channel.value,
    )
    return {
        "success": sent,
        "service_name": service_name,
        "notification_sent": sent,
        "channel": adapter.channel.value,
    }


def _send_cb_open_notification_oss(
    *,
    service_name: str,
    timestamp: str,
) -> dict:
    """Build + deliver the OSS CB OPEN push (single home of the OPEN payload).

    Owns the OPEN title / message / priority / event-type literals; forwards to
    :func:`_send_cb_notification_oss`. Called by both the Celery task and the
    EventBus handler ``ImportError`` branches so the OPEN payload exists once.
    """
    return _send_cb_notification_oss(
        service_name=service_name,
        title=f"\U0001f534 Circuit Breaker OPEN: {service_name}",
        message=f"Circuit Breaker opened for service '{service_name}'.",
        priority_name="high",
        event_type="circuit_breaker_opened",
        timestamp=timestamp,
    )


def _send_cb_close_notification_oss(
    *,
    service_name: str,
    timestamp: str,
    previous_state: str,
    trigger: str,
) -> dict:
    """Build + deliver the OSS CB CLOSED (recovery stand-down) push.

    Single home of the CLOSED payload literals (title / message / priority /
    event-type / ``previous_state`` + ``trigger`` metadata); forwards to
    :func:`_send_cb_notification_oss`. Low-urgency parity with the OPEN wrapper.
    """
    return _send_cb_notification_oss(
        service_name=service_name,
        title=f"\U0001f7e2 Circuit Breaker recovered: {service_name}",
        message=(
            f"Circuit Breaker recovered for service '{service_name}' "
            f"(trigger: {trigger or 'unknown'})."
        ),
        priority_name="low",
        event_type="circuit_breaker_closed",
        timestamp=timestamp,
        extra_metadata={
            "previous_state": previous_state,
            "trigger": trigger,
        },
    )
