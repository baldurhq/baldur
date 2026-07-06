"""
Channel Target Settings - Pydantic v2.

Delivery target configuration for notification channels.
Replaces broken NotificationConfig.from_settings() getattr path
with proper env-var configurable settings.

Environment Variables:
    BALDUR_CHANNEL_TARGET_SLACK_WEBHOOK_URL=https://hooks.slack.com/...
    BALDUR_CHANNEL_TARGET_PAGERDUTY_SERVICE_KEY=...
    BALDUR_CHANNEL_TARGET_WEBHOOK_URLS='["https://example.com/hook"]'
    BALDUR_CHANNEL_TARGET_WEBHOOK_HEADERS='{"Authorization": "Bearer ..."}'
    BALDUR_CHANNEL_TARGET_DRY_RUN=true

Reference:
    410 (DC-6, ID-12)
"""

from __future__ import annotations

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings

from baldur.settings.base import make_settings_config


class ChannelTargetSettings(BaseSettings):
    """
    Channel delivery target configuration.

    Defines where to deliver notifications (webhook URLs, recipients, API keys).
    Concern separation:
    - ChannelRoutingSettings: which channels to use (routing rules)
    - ChannelTargetSettings: where to deliver (concrete targets)
    - NotificationSettings: operational parameters (thresholds, limits)
    """

    model_config = make_settings_config("BALDUR_CHANNEL_TARGET_")

    slack_webhook_url: str = Field(
        default="",
        description=(
            "Slack incoming webhook URL read by the PRO unified notification hub. "
            "Note: Meta-Watchdog escalation and OSS Slack delivery read a "
            "different home (MetaWatchdogSettings.slack_webhook_url, "
            "BALDUR_META_WATCHDOG_SLACK_WEBHOOK_URL) — set that one for OSS "
            "circuit-breaker / escalation notifications."
        ),
    )

    pagerduty_service_key: str = Field(
        default="",
        description="PagerDuty Events API routing key",
    )

    pagerduty_enabled: bool = Field(
        default=False,
        description="Enable PagerDuty integration",
    )

    webhook_urls: list[str] = Field(
        default_factory=list,
        description="Generic outbound webhook URLs (PRO channel — endpoints accepting canonical Baldur JSON)",
    )

    webhook_headers: dict[str, SecretStr] = Field(
        default_factory=dict,
        description=(
            "Per-request headers for outbound webhooks (e.g. auth token). "
            "Values are credentials — SecretStr repr-masked, never logged."
        ),
    )

    dry_run: bool = Field(
        default=False,
        description="Log instead of send",
    )


# ==========================================================================
# Singleton management
# ==========================================================================
def get_channel_target_settings() -> ChannelTargetSettings:
    """Get cached ChannelTargetSettings instance."""
    from baldur.runtime import get_runtime

    return get_runtime().get_settings(ChannelTargetSettings)


def reset_channel_target_settings() -> None:
    """Reset cached settings (for testing)."""
    from baldur.runtime import get_runtime

    get_runtime().reset_settings(ChannelTargetSettings)


__all__ = [
    "ChannelTargetSettings",
    "get_channel_target_settings",
    "reset_channel_target_settings",
]
