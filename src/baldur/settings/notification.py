"""
Notification Settings - Pydantic v2.

Single Source of Truth for notification configuration.

Replaces:
- core/config.py:NotificationConfig
- core/safe_defaults.py:SAFE_DEFAULTS["notification"]
- core/safe_defaults.py:VALIDATION_RULES["notification"]

Environment Variables:
    BALDUR_NOTIFICATION_ENABLED=true
    BALDUR_NOTIFICATION_CRITICAL_THRESHOLD=10
"""

from pydantic import Field
from pydantic_settings import BaseSettings

from baldur.settings.base import make_settings_config
from baldur.settings.field_types import (
    MediumCount,
    ShortInterval,
)


class NotificationSettings(BaseSettings):
    """
    Notification and alerts configuration with validation.

    All defaults match core/config.py:NotificationConfig
    All validation rules match core/safe_defaults.py:VALIDATION_RULES["notification"]
    """

    model_config = make_settings_config("BALDUR_NOTIFICATION_")

    # ==========================================================================
    # Core Settings (from core/config.py:NotificationConfig)
    # Validation rules from core/safe_defaults.py:VALIDATION_RULES["notification"]
    # ==========================================================================
    enabled: bool = Field(
        default=True,
        description="Enable notification system",
    )
    critical_threshold: MediumCount = Field(
        default=10,
        description="Threshold for critical notifications",
    )
    warning_threshold: MediumCount = Field(
        default=5,
        description="Threshold for warning notifications",
    )

    # ==========================================================================
    # Message Limits (from core/config.py:NotificationConfig)
    # ==========================================================================
    description_max_length: int = Field(
        default=500,
        ge=50,
        le=5000,
        description="Maximum description length",
    )
    action_taken_max_length: int = Field(
        default=200,
        ge=50,
        le=1000,
        description="Maximum action taken text length",
    )
    title_max_length: int = Field(
        default=150,
        ge=20,
        le=500,
        description="Maximum title length",
    )
    notification_timeout_seconds: ShortInterval = Field(
        default=10,
        description="Notification timeout in seconds",
    )

    # ==========================================================================
    # Slack Channels (from core/config.py:NotificationConfig)
    # ==========================================================================
    critical_channel: str = Field(
        default="#critical-alerts",
        description="Slack channel for critical alerts",
    )
    high_channel: str = Field(
        default="#ops-alerts",
        description="Slack channel for high priority alerts",
    )
    medium_channel: str = Field(
        default="#dev-alerts",
        description="Slack channel for medium priority alerts",
    )


def get_notification_settings() -> "NotificationSettings":
    """Get cached NotificationSettings instance."""
    from baldur.settings.root import get_config

    return get_config().services_group.notification


def reset_notification_settings() -> None:
    """Reset cached settings (for testing)."""
    from baldur.settings.root import get_config

    try:
        del get_config().services_group.__dict__["notification"]
    except KeyError:
        pass


def get_notification_settings_safe() -> "NotificationSettings":
    """Get notification settings with environment variable drift detection."""
    from baldur.settings.drift_monitor import get_config_drift_monitor

    monitor = get_config_drift_monitor()
    if monitor.check_and_invalidate("notification", "BALDUR_NOTIFICATION_"):
        reset_notification_settings()
    return get_notification_settings()
