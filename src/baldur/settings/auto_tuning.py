"""
Auto-Tuning Settings - Pydantic v2.

Master toggle and configuration for the AutoTuningService.

Environment Variables:
    BALDUR_AUTO_TUNING_ENABLED=true
"""

from pydantic import Field
from pydantic_settings import BaseSettings

from baldur.settings.base import make_settings_config

__all__ = [
    "AutoTuningSettings",
    "get_auto_tuning_settings",
    "reset_auto_tuning_settings",
]


class AutoTuningSettings(BaseSettings):
    """
    Auto-Tuning service configuration.

    Controls whether the AutoTuningService background loop starts
    and processes tuning adjustments.
    """

    model_config = make_settings_config("BALDUR_AUTO_TUNING_")

    # ==========================================================================
    # Master Toggle
    # ==========================================================================
    enabled: bool = Field(
        default=False,
        description="Enable/disable auto-tuning service. When False, "
        "AutoTuningService.start() returns immediately without starting "
        "the background loop.",
    )


# =============================================================================
# Singleton Pattern
# =============================================================================


def get_auto_tuning_settings() -> "AutoTuningSettings":
    """Return cached AutoTuningSettings instance."""
    from baldur.settings.root import get_config

    return get_config().services_group.auto_tuning


def reset_auto_tuning_settings() -> None:
    """Reset cached settings (for testing)."""
    from baldur.settings.root import get_config

    try:
        del get_config().services_group.__dict__["auto_tuning"]
    except KeyError:
        pass
