"""
Notification Channel Settings - Pydantic v2.

Per-channel rate limiting and retry policy settings for notifications.

Replaces:
- services/unified_notification.py: channel mapping
- core/safe_defaults.py: notification-related settings
- notification_policy.py:cooldown_seconds

Environment Variables:
    BALDUR_NOTIFICATION_CHANNEL_RATE_LIMIT_PER_MINUTE=60
    BALDUR_NOTIFICATION_CHANNEL_MAX_RETRY=3
    BALDUR_NOTIFICATION_CHANNEL_COOLDOWN_SECONDS=300
"""

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings

from baldur.settings.base import make_settings_config
from baldur.settings.field_types import (
    LargeCount,
)
from baldur.settings.validators import warn_above


class NotificationChannelSettings(BaseSettings):
    """
    Notification channel settings.

    Manages severity-based channel mapping and rate limiting.

    Features:
    - Severity-based channel routing (CRITICAL → slack,email,pagerduty)
    - Rate limiting to prevent notification floods
    - Retry policy
    - Cooldown to suppress duplicate notifications
    """

    model_config = make_settings_config("BALDUR_NOTIFICATION_CHANNEL_")

    # ==========================================================================
    # Rate Limiting (from safe_defaults.py, notification_config.py)
    # ==========================================================================
    rate_limit_per_minute: LargeCount = Field(
        default=60,
        description="Maximum notifications per minute",
    )

    rate_limit_per_hour: int = Field(
        default=300,
        ge=10,
        le=5000,
        description="Maximum notifications per hour",
    )

    # ==========================================================================
    # Retry Settings (from notification_config.py)
    # ==========================================================================
    max_retry: int = Field(
        default=3,
        ge=0,
        le=10,
        description="Maximum retry count for notification delivery",
    )

    retry_delay_seconds: int = Field(
        default=30,
        ge=5,
        le=300,
        description="Retry interval (seconds)",
    )

    # ==========================================================================
    # Cooldown Settings (from notification_policy.py#L100)
    # ==========================================================================
    cooldown_seconds: int = Field(
        default=300,
        ge=60,
        le=3600,
        description="Cooldown before resending the same notification (seconds)",
    )

    # ==========================================================================
    # Escalation Settings (from models.py#L351)
    # ==========================================================================
    escalate_on_emergency: bool = Field(
        default=True,
        description="Auto-escalate on emergency situations",
    )

    @field_validator("rate_limit_per_minute")
    @classmethod
    def _warn_rate_limit(cls, v: int) -> int:
        """Warn when the rate limit is too high."""
        return warn_above(100, "notification_channel.rate_limit_high")(v)


def get_notification_channel_settings() -> "NotificationChannelSettings":
    from baldur.settings.root import get_config

    return get_config().adapters.notification_channel


def reset_notification_channel_settings() -> None:
    from baldur.settings.root import get_config

    try:
        del get_config().adapters.__dict__["notification_channel"]
    except KeyError:
        pass
