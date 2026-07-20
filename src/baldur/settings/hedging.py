"""
Hedging Settings - Pydantic v2 based settings.

The hedging strategy defaults are configurable through environment variables.

Environment Variables:
    BALDUR_HEDGING_DEFAULT_MODE=delayed
    BALDUR_HEDGING_DEFAULT_TIMEOUT=5.0
    BALDUR_HEDGING_DEFAULT_DELAY=0.1
    BALDUR_HEDGING_MAX_CANDIDATES=3
"""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings

from baldur.settings.base import make_settings_config
from baldur.settings.field_types import (
    STANDARD_POOL_SIZE,
    BackoffMultiplier,
    ShortDuration,
    TinyCount,
)


class HedgingSettings(BaseSettings):
    """
    Hedging settings.

    Configurable through environment variables.
    Prefix: BALDUR_HEDGING_
    """

    model_config = make_settings_config("BALDUR_HEDGING_")

    # ==========================================================================
    # Master Toggle
    # ==========================================================================
    enabled: bool = Field(
        default=True,
        description="Enable/disable hedging globally. When False, HedgingPolicy "
        "falls back to single execution without parallel candidates.",
    )

    # ==========================================================================
    # Default Settings
    # ==========================================================================
    default_mode: str = Field(
        default="delayed",
        description="Default hedging mode (immediate, delayed, adaptive)",
    )

    default_timeout: ShortDuration = Field(
        default=5.0,
        description="Default timeout (seconds)",
    )

    default_delay: float = Field(
        default=0.1,
        ge=0.0,
        le=10.0,
        description="Default delay for DELAYED mode (seconds)",
    )

    max_candidates: TinyCount = Field(
        default=3,
        description="Maximum concurrent execution candidates",
    )

    # ==========================================================================
    # Thread pool
    # ==========================================================================
    executor_max_workers: int = Field(
        default=STANDARD_POOL_SIZE,
        ge=1,
        le=50,
        description="Hedging thread pool maximum workers",
    )

    # ==========================================================================
    # Backpressure integration
    # ==========================================================================
    disable_on_load_level: str = Field(
        default="high",
        description="Disable hedging at or above this load level (none, low, medium, high, critical)",
    )

    delay_multiplier_on_medium: BackoffMultiplier = Field(
        default=2.0,
        description="Delay multiplier at MEDIUM load level",
    )

    delay_multiplier_on_high: float = Field(
        default=5.0,
        ge=1.0,
        le=20.0,
        description="Delay multiplier at HIGH load level",
    )


def get_hedging_settings() -> HedgingSettings:
    from baldur.settings.root import get_config

    return get_config().resilience.hedging


def reset_hedging_settings() -> None:
    from baldur.settings.root import get_config

    try:
        del get_config().resilience.__dict__["hedging"]
    except KeyError:
        pass
