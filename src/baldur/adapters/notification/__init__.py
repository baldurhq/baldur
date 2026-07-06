"""OSS notification delivery adapter.

Exposes the OSS-tier outbound notification transport
(:class:`SlackWebhookNotificationAdapter`). It is constructed directly by the
circuit-breaker notification task on an OSS-only install — the single sanctioned
out-of-seam external-push exception to the "OSS observes; PRO notifies"
boundary. The notification registry seam itself stays logging-only on OSS, so
nothing here registers into it.
"""

from __future__ import annotations

from ._cb_delivery import (
    _send_cb_close_notification_oss,
    _send_cb_notification_oss,
    _send_cb_open_notification_oss,
)
from .webhook_adapter import SlackWebhookNotificationAdapter

# The OSS CB-delivery helpers stay private (leading underscore) and are NOT
# exported in __all__ — they are an internal OSS delivery seam imported by
# explicit name from both the Celery task module and the EventBus CB handlers.
__all__ = [
    "SlackWebhookNotificationAdapter",
]
