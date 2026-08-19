"""
Canary Watchdog Settings - Pydantic v2.

Canary Rollout Watchdog task settings.
Zombie rollout detection, automatic rollback, and automatic promotion.

Source:
- tasks/canary_watchdog.py

Environment Variables:
    BALDUR_CANARY_WATCHDOG_ZOMBIE_THRESHOLD_MINUTES=30
    BALDUR_CANARY_WATCHDOG_AUTO_ROLLBACK_AFTER_MINUTES=60
    BALDUR_CANARY_WATCHDOG_MAX_STAGE_DURATION_MINUTES=15
    BALDUR_CANARY_WATCHDOG_ENABLE_AUTO_PROMOTE=false
    BALDUR_CANARY_WATCHDOG_ENABLE_AUTO_ROLLBACK=false
    BALDUR_CANARY_WATCHDOG_NOTIFICATION_ENABLED=true
"""

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings

from baldur.settings.base import make_settings_config


class CanaryWatchdogSettings(BaseSettings):
    """
    Canary Watchdog settings.

    Defines the zombie rollout detection threshold, automatic
    rollback/promotion, and notification settings.

    Slack *targets* are not configured here: the watchdog's alerts route
    through the unified notification manager, whose per-category Slack target
    is owned by ``BALDUR_CHANNEL_ROUTING_CATEGORY_SLACK_TARGETS``.
    """

    model_config = make_settings_config("BALDUR_CANARY_WATCHDOG_")

    # ==========================================================================
    # Zombie Detection
    # ==========================================================================
    zombie_threshold_minutes: int = Field(
        default=30,
        ge=5,
        le=240,
        description="Time to consider a rollout as stalled/zombie (minutes)",
    )

    # ==========================================================================
    # Auto Rollback
    # ==========================================================================
    auto_rollback_after_minutes: int = Field(
        default=60,
        ge=10,
        le=480,
        description="Wait time before automatic rollback (minutes)",
    )

    # ==========================================================================
    # Stage Duration
    # ==========================================================================
    max_stage_duration_minutes: int = Field(
        default=15,
        ge=1,
        le=120,
        description="Maximum duration per stage (minutes)",
    )

    # ==========================================================================
    # Feature Toggles
    # ==========================================================================
    # Opt-in by default: the watchdog lane's non-mutating work (config-lock
    # renewal, stall notification, metric collection) runs as soon as the lane
    # is composed, but the two mutating actions stay off until an operator
    # turns them on. Activating the lane must not start promoting or rolling
    # back rollouts on installs that have never had either.
    enable_auto_promote: bool = Field(
        default=False,
        description="Enable automatic promotion (opt-in)",
    )
    enable_auto_rollback: bool = Field(
        default=False,
        description="Enable automatic rollback for zombies (opt-in)",
    )
    notification_enabled: bool = Field(
        default=True,
        description="Enable Slack notifications",
    )

    @model_validator(mode="after")
    def validate_timing(self) -> "CanaryWatchdogSettings":
        """Ensure auto_rollback is greater than zombie_threshold."""
        if self.auto_rollback_after_minutes <= self.zombie_threshold_minutes:
            raise ValueError(
                f"auto_rollback_after_minutes ({self.auto_rollback_after_minutes}) "
                f"must be greater than zombie_threshold_minutes ({self.zombie_threshold_minutes})"
            )
        return self


# =============================================================================
# Singleton Pattern
# =============================================================================


def get_canary_watchdog_settings() -> "CanaryWatchdogSettings":
    """
    Return the cached CanaryWatchdogSettings instance.

    Returns:
        CanaryWatchdogSettings: The singleton instance
    """
    from baldur.settings.root import get_config

    return get_config().services_group.canary_watchdog


def reset_canary_watchdog_settings() -> None:
    """
    Reset the cached settings (for tests).

    Call this to reload the settings after changing environment variables.
    """
    from baldur.settings.root import get_config

    try:
        del get_config().services_group.__dict__["canary_watchdog"]
    except KeyError:
        pass
