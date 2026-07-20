"""
Rate Limit Throttle Integration Settings - Pydantic v2.

Defines how 429 responses feed back into AdaptiveThrottle.

Features:
    - Automatic throttle limit reduction on a 429
    - Reduction ratio per consecutive 429 count
    - Key-service mapping (prevents interference between neighbours)
    - Recovery strategy settings
    - Escalation settings

Environment Variables:
    BALDUR_RATE_LIMIT_THROTTLE_INTEGRATION_ENABLED=true
    BALDUR_RATE_LIMIT_THROTTLE_INTEGRATION_DEBOUNCE_WINDOW_SECONDS=5.0
    BALDUR_RATE_LIMIT_THROTTLE_INTEGRATION_ESCALATION_ENABLED=true
    ... etc
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings

from baldur.settings.base import make_settings_config
from baldur.settings.field_types import (
    TinyCount,
)


class RateLimitThrottleIntegrationSettings(BaseSettings):
    """
    429-throttle integration settings.

    Settings for automatically adjusting the AdaptiveThrottle limit when a
    429 response is received from an external API.
    """

    model_config = make_settings_config("BALDUR_RATE_LIMIT_THROTTLE_INTEGRATION_")

    # =========================================================================
    # Base enablement
    # =========================================================================
    enabled: bool = Field(
        default=True,
        description="Enable throttle limit reduction on 429 responses",
    )

    # =========================================================================
    # Limit reduction ratio per consecutive 429 count
    # =========================================================================
    reduction_ratio_1: float = Field(
        default=0.8,
        ge=0.1,
        le=1.0,
        description="Retention ratio after 1st 429 (0.8 = 20% reduction)",
    )
    reduction_ratio_2: float = Field(
        default=0.6,
        ge=0.1,
        le=1.0,
        description="Retention ratio after 2 consecutive 429s (0.6 = 40% reduction)",
    )
    reduction_ratio_3: float = Field(
        default=0.5,
        ge=0.1,
        le=1.0,
        description="Retention ratio after 3+ consecutive 429s (0.5 = 50% reduction)",
    )

    # =========================================================================
    # Recovery strategy settings
    # =========================================================================
    recovery_strategy: Literal["immediate", "gradual"] = Field(
        default="gradual",
        description="Limit recovery strategy after cooldown expires",
    )
    recovery_dampening_steps: TinyCount = Field(
        default=3,
        description="Number of steps for gradual recovery",
    )

    # =========================================================================
    # EventBus debouncing
    # =========================================================================
    debounce_window_seconds: float = Field(
        default=5.0,
        ge=0.0,
        le=60.0,
        description="Event deduplication window for the same key (seconds)",
    )

    # =========================================================================
    # Key-service mapping (prevents interference between neighbours)
    # =========================================================================
    default_service: str = Field(
        default="default",
        description="Default service for unmapped keys",
    )

    # Note: key_to_service_mapping is awkward to express as an env var, so set it
    # directly in code or from a separate config file.

    def get_reduction_ratio(self, consecutive_429s: int) -> float:
        """
        Return the reduction ratio for the given consecutive 429 count.

        Args:
            consecutive_429s: number of consecutive 429s

        Returns:
            Retention ratio (e.g. 0.8 = 20% reduction)
        """
        if consecutive_429s >= 3:
            return self.reduction_ratio_3
        if consecutive_429s == 2:
            return self.reduction_ratio_2
        return self.reduction_ratio_1


def get_rate_limit_throttle_integration_settings() -> (
    RateLimitThrottleIntegrationSettings
):
    from baldur.settings.root import get_config

    return get_config().scaling.rate_limit_throttle_integration


# Backward-compatible alias
get_rate_limit_throttle_settings = get_rate_limit_throttle_integration_settings


def reset_rate_limit_throttle_integration_settings() -> None:
    from baldur.settings.root import get_config

    try:
        del get_config().scaling.__dict__["rate_limit_throttle_integration"]
    except KeyError:
        pass


# Backward-compatible alias
clear_settings_cache = reset_rate_limit_throttle_integration_settings
