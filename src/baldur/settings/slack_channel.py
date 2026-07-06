"""
Slack Channel Settings - Pydantic v2.

Slack channel mapping and related settings.

Replaces:
- services/unified_notification.py channel mapping
- services/notification_policy.py settings

Environment Variables:
    BALDUR_SLACK_CHANNEL_DEFAULT_CHANNEL=#baldur-alerts
    BALDUR_SLACK_CHANNEL_CRITICAL_CHANNEL=#baldur-critical

Reference:
- docs/baldur/middleware_system/92_CONFIG_IMPLEMENTATION_GUIDE.md (Week 4 [24])
- docs/baldur/middleware_system/91_CONFIG_INVENTORY.md §6.33
"""

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings

from baldur.settings.base import make_settings_config


class SlackChannelSettings(BaseSettings):
    """
    Slack channel settings.

    Channel mapping:
    - default_channel: default alert channel
    - critical_channel: critical alert channel
    - emergency_channel: emergency channel
    - recovery_channel: recovery alert channel
    - audit_channel: audit log channel

    Message settings:
    - block_text_limit: Slack block text limit (3000)
    - max_attachments: max attachment count (10)
    """

    model_config = make_settings_config("BALDUR_SLACK_CHANNEL_")

    # ==========================================================================
    # Channel Names - from unified_notification.py
    # ==========================================================================
    default_channel: str = Field(
        default="#baldur-alerts",
        description="Default alert channel",
    )

    critical_channel: str = Field(
        default="#baldur-critical",
        description="Critical alert channel",
    )

    emergency_channel: str = Field(
        default="#baldur-emergency",
        description="Emergency channel",
    )

    recovery_channel: str = Field(
        default="#baldur-recovery",
        description="Recovery alert channel",
    )

    audit_channel: str = Field(
        default="#baldur-audit",
        description="Audit log channel",
    )

    on_call_channel: str = Field(
        default="#on-call",
        description="On-call alert channel",
    )

    # ==========================================================================
    # Message Limits - from notification config
    # ==========================================================================
    block_text_limit: int = Field(
        default=3000,
        ge=1000,
        le=10000,
        description="Slack block text max length",
    )

    max_attachments: int = Field(
        default=10,
        ge=1,
        le=50,
        description="Max attachments per message",
    )

    @field_validator(
        "default_channel",
        "critical_channel",
        "emergency_channel",
        "recovery_channel",
        "audit_channel",
        "on_call_channel",
    )
    @classmethod
    def validate_channel_name(cls, v: str) -> str:
        """Verify channel name starts with # or C."""
        if not v.startswith("#") and not v.startswith("C"):
            raise ValueError(
                f"Channel name must start with '#' (name) or 'C' (ID): {v}"
            )
        return v


# ==========================================================================
# Singleton management
# ==========================================================================
def get_slack_channel_settings() -> "SlackChannelSettings":
    """Get cached SlackChannelSettings instance."""
    from baldur.runtime import get_runtime

    return get_runtime().get_settings(SlackChannelSettings)


def reset_slack_channel_settings() -> None:
    """Reset cached settings (for testing)."""
    from baldur.runtime import get_runtime

    get_runtime().reset_settings(SlackChannelSettings)
