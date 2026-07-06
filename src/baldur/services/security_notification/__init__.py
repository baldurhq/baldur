"""
Security Notification Value Surface (OSS).

OSS-importable value types and pure formatters for security notifications.
Per the notification tier principle ("OSS observes, PRO notifies"), the
concrete external-push transports and the ``SecurityNotificationService`` live
in the private PRO distribution. This package
keeps only the tier-neutral value/format surface that OSS consumers
(``daily_report``, ``security_review``) and the PRO pipeline import.

Notification severity routing (realized by the PRO transports):
- CRITICAL: Slack + PagerDuty
- HIGH: Slack
- MEDIUM: Slack only
"""

from __future__ import annotations

from .formatters import (
    format_alert_message,
    format_incident_message,
    format_report_message,
    truncate_with_ellipsis,
)

# Models and data classes
from .models import (
    ChannelDeliveryResult,
    NotificationChannel,
    NotificationConfig,
    PagerDutySeverity,
    SecurityNotificationResult,
    _get_notification_limits,
)

__all__ = [
    # Models
    "NotificationChannel",
    "NotificationConfig",
    "ChannelDeliveryResult",
    "SecurityNotificationResult",
    "PagerDutySeverity",
    "_get_notification_limits",
    # Formatters
    "truncate_with_ellipsis",
    "format_alert_message",
    "format_report_message",
    "format_incident_message",
]
