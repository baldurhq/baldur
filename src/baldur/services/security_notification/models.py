"""
Security Notification Models and Data Classes.

Enums, dataclasses, and configuration for security notifications.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import structlog

logger = structlog.get_logger()


# =============================================================================
# Constants - loaded from config
# =============================================================================


def _get_notification_limits():
    """Lazy-load notification limits via the layered provider so a console edit
    of the notification domain takes effect; env base when no
    RuntimeConfigManager is registered."""
    from baldur.settings.layered_provider import get_layered_settings
    from baldur.settings.notification import NotificationSettings

    return get_layered_settings(NotificationSettings, "notification")


# =============================================================================
# Enums
# =============================================================================

# NotificationChannel: single source in interfaces/notification.py (Item 3 dedup)
from enum import Enum  # noqa: E402

from baldur.interfaces.notification import NotificationChannel  # noqa: E402, F401


class PagerDutySeverity(str, Enum):
    """PagerDuty Events API v2 standard severity values.

    Determines incident urgency: critical/error -> high urgency (pages),
    warning/info -> low urgency (notification only).
    """

    CRITICAL = "critical"
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


# =============================================================================
# Configuration
# =============================================================================


@dataclass
class NotificationConfig:
    """Configuration for security notifications."""

    # Slack configuration
    slack_webhook_url: str = ""
    slack_critical_channel: str = "#critical-alerts"
    slack_high_channel: str = "#ops-alerts"
    slack_medium_channel: str = "#dev-alerts"

    # PagerDuty configuration
    pagerduty_service_key: str = ""
    pagerduty_enabled: bool = False

    # Generic outbound webhook configuration (PRO channel — OSS observes; PRO notifies)
    webhook_urls: list[str] = field(default_factory=list)
    # Values are credentials (auth tokens, etc.) — never logged.
    webhook_headers: dict[str, str] = field(default_factory=dict)

    # General settings
    enabled: bool = True
    dry_run: bool = False  # For testing - log instead of send

    @classmethod
    def from_settings(cls) -> NotificationConfig:
        """Load configuration from ChannelTargetSettings and NotificationSettings."""
        from baldur.settings.channel_target import get_channel_target_settings
        from baldur.settings.layered_provider import get_layered_settings
        from baldur.settings.notification import NotificationSettings

        targets = get_channel_target_settings()
        # Layered read (686 D1/D5) so a console edit of the notification channels /
        # enabled flag takes effect; env base when no manager is registered.
        notification = get_layered_settings(NotificationSettings, "notification")

        return cls(
            slack_webhook_url=targets.slack_webhook_url,
            slack_critical_channel=notification.critical_channel,
            slack_high_channel=notification.high_channel,
            slack_medium_channel=notification.medium_channel,
            pagerduty_service_key=targets.pagerduty_service_key,
            pagerduty_enabled=targets.pagerduty_enabled,
            webhook_urls=list(targets.webhook_urls),
            # SecretStr values are resolved here for the transport; this config
            # object is transient/in-memory and never logged.
            webhook_headers={
                k: v.get_secret_value() for k, v in targets.webhook_headers.items()
            },
            enabled=notification.enabled,
            dry_run=targets.dry_run,
        )


# =============================================================================
# Result Data Classes
# =============================================================================


@dataclass
class ChannelDeliveryResult:
    """Result of a notification attempt."""

    channel: str
    success: bool
    message: str = ""
    error: str | None = None


@dataclass
class SecurityNotificationResult:
    """Aggregate result of all notification attempts."""

    incident_id: int
    results: list[ChannelDeliveryResult] = field(default_factory=list)

    @property
    def all_success(self) -> bool:
        """Check if all notifications were successful."""
        return all(r.success for r in self.results)

    @property
    def any_success(self) -> bool:
        """Check if any notification was successful."""
        return any(r.success for r in self.results)

    def add_result(self, result: ChannelDeliveryResult) -> None:
        """Add a notification result."""
        self.results.append(result)
