"""
Canary Interlock Settings - Pydantic v2.

Configuration for the EmergencyStateRefresher — the per-worker daemon that
re-reads the emergency level and applies the CanarySafetyInterlock ladder
(L2 pause / L3 rollback) to active rollouts on an escalation.

Environment Variables:
    BALDUR_CANARY_INTERLOCK_REFRESHER_ENABLED=true
    BALDUR_CANARY_INTERLOCK_REFRESH_INTERVAL_SECONDS=30
    BALDUR_CANARY_INTERLOCK_JITTER_MAX_SECONDS=5
    BALDUR_CANARY_INTERLOCK_MAX_CONSECUTIVE_FAILURES=3
    BALDUR_CANARY_INTERLOCK_ON_REFRESH_FAILURE_ACTION=log_and_continue
"""

from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings

from baldur.settings.base import make_settings_config


class CanaryInterlockSettings(BaseSettings):
    """
    Canary Interlock (EmergencyStateRefresher) settings.

    The refresher's behavioral consumer is the PRO startup starter
    (``_start_canary_interlock_refresher_if_enabled``), which maps these values
    onto a ``StateRefresherConfig`` and starts the daemon — so the default-ON
    ``refresher_enabled`` flag is a real guarantee, not an echo.
    """

    model_config = make_settings_config("BALDUR_CANARY_INTERLOCK_")

    refresher_enabled: bool = Field(
        default=True,
        description="Start the per-worker emergency-state refresher daemon",
    )
    refresh_interval_seconds: int = Field(
        default=30,
        ge=5,
        le=300,
        description="Poll interval for the refresher (seconds). Worst-case "
        "ladder reaction latency when the wake event is lost.",
    )
    jitter_max_seconds: int = Field(
        default=5,
        ge=0,
        le=30,
        description="Maximum jitter added to the poll interval (seconds)",
    )
    max_consecutive_failures: int = Field(
        default=3,
        ge=1,
        le=10,
        description="Consecutive backend-unavailable refreshes before the "
        "failure action fires",
    )
    on_refresh_failure_action: Literal["log_and_continue", "fail_closed"] = Field(
        default="log_and_continue",
        description="Action after max_consecutive_failures: 'log_and_continue' "
        "alerts and keeps polling; 'fail_closed' applies the L3 rollback ladder "
        "once (re-armed after a successful refresh)",
    )


# =============================================================================
# Singleton Pattern
# =============================================================================


def get_canary_interlock_settings() -> "CanaryInterlockSettings":
    """Return the cached CanaryInterlockSettings instance."""
    from baldur.settings.root import get_config

    return get_config().services_group.canary_interlock


def reset_canary_interlock_settings() -> None:
    """Reset the cached settings (for testing)."""
    from baldur.settings.root import get_config

    try:
        del get_config().services_group.__dict__["canary_interlock"]
    except KeyError:
        pass


__all__ = [
    "CanaryInterlockSettings",
    "get_canary_interlock_settings",
    "reset_canary_interlock_settings",
]
