"""
Channel Routing Settings - Pydantic v2.

Configurable channel routing rules for the unified notification system.
Replaces hardcoded RoutingPolicy defaults with settings-driven configuration.

Environment Variables:
    BALDUR_CHANNEL_ROUTING_PRIORITY_CHANNELS='{"critical":["slack","pagerduty"]}'
    BALDUR_CHANNEL_ROUTING_CATEGORY_CHANNELS='{"security":["slack"]}'
    BALDUR_CHANNEL_ROUTING_CATEGORY_SLACK_TARGETS='{"security":"#security-incidents"}'

Reference:
    410 (DC-2, ID-2, ID-3)
"""

from __future__ import annotations

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings

from baldur.settings.base import make_settings_config


class ChannelRoutingSettings(BaseSettings):
    """
    Channel routing configuration.

    Defines which channels to use based on priority and category,
    and per-category cooldown seconds for suppression logic.
    """

    model_config = make_settings_config("BALDUR_CHANNEL_ROUTING_")

    priority_channels: dict[str, list[str]] = Field(
        default={
            "critical": ["slack", "pagerduty"],
            "high": ["slack"],
            "medium": ["slack"],
            "low": ["slack"],
            "info": [],
        },
        description="Priority → channel types mapping",
    )

    category_channels: dict[str, list[str]] = Field(
        default={
            "security": ["slack"],
            "approval": ["slack"],
            "report": ["slack", "pagerduty"],
            "governance": ["slack"],
        },
        description="Category → channel types override",
    )

    category_slack_targets: dict[str, str] = Field(
        default={},
        description="Optional: category → Slack channel override (e.g. security → #security-incidents)",
    )

    category_cooldown_seconds: dict[str, int] = Field(
        default={
            "security": 60,
            "operations": 300,
            "sla": 1800,
            "circuit_breaker": 300,
            "governance": 900,
            "approval": 0,
            "report": 0,
            "error": 60,
            "chaos": 300,
        },
        description="Per-category cooldown seconds. UNM reads directly for suppression logic.",
    )

    @field_validator("priority_channels")
    @classmethod
    def _validate_priority_keys(cls, v: dict[str, list[str]]) -> dict[str, list[str]]:
        """Validate priority keys against NotificationPriority enum values."""
        from baldur.models.notification import NotificationPriority

        valid_keys = {p.value for p in NotificationPriority}
        unknown = set(v.keys()) - valid_keys
        if unknown:
            raise ValueError(f"Unknown priority keys: {unknown}. Valid: {valid_keys}")
        return v

    @field_validator("category_channels", "category_cooldown_seconds")
    @classmethod
    def _validate_category_keys(cls, v: dict) -> dict:
        """Validate category keys against NotificationCategory enum values."""
        from baldur.models.notification import NotificationCategory

        valid_keys = {c.value for c in NotificationCategory}
        unknown = set(v.keys()) - valid_keys
        if unknown:
            raise ValueError(f"Unknown category keys: {unknown}. Valid: {valid_keys}")
        return v

    @field_validator("priority_channels", "category_channels")
    @classmethod
    def _validate_channel_values(cls, v: dict[str, list[str]]) -> dict[str, list[str]]:
        """Reject channel-type values not defined in the MessageChannel enum.

        Loud config-load rejection (vs a silent runtime skip): a routing config
        that lists a removed/unknown channel (e.g. ``email``/``sms``) fails here
        instead of at incident time. The allowlist is derived from the enum, so
        it stays self-maintaining as channels are added or removed.
        """
        from baldur.interfaces.messaging_common import MessageChannel

        valid_values = {c.value for c in MessageChannel}
        unknown = {ch for channels in v.values() for ch in channels} - valid_values
        if unknown:
            raise ValueError(f"Unknown channel types: {unknown}. Valid: {valid_values}")
        return v


# ==========================================================================
# Singleton management
# ==========================================================================
def get_channel_routing_settings() -> ChannelRoutingSettings:
    """Get cached ChannelRoutingSettings instance."""
    from baldur.runtime import get_runtime

    return get_runtime().get_settings(ChannelRoutingSettings)


def reset_channel_routing_settings() -> None:
    """Reset cached settings (for testing)."""
    from baldur.runtime import get_runtime

    get_runtime().reset_settings(ChannelRoutingSettings)


__all__ = [
    "ChannelRoutingSettings",
    "get_channel_routing_settings",
    "reset_channel_routing_settings",
]
