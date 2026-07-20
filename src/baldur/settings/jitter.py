"""
Jitter Settings - Pydantic v2.

Jitter settings for thundering herd prevention.
Spreads DB queries over time across instances that start simultaneously in a
distributed deployment.

Also holds the AdaptiveJitter thresholds:
- Danger/safe classification from the error budget
- High/low load classification from system load

Environment Variables:
    BALDUR_JITTER_MAX_DELAY_SECONDS=60.0
    BALDUR_JITTER_MIN_DELAY_SECONDS=0.0
    BALDUR_JITTER_ERROR_BUDGET_DANGER_THRESHOLD=0.2
    BALDUR_JITTER_ERROR_BUDGET_SAFE_THRESHOLD=0.5
    BALDUR_JITTER_LOAD_HIGH_THRESHOLD=0.8
    BALDUR_JITTER_LOAD_LOW_THRESHOLD=0.3
"""

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings

from baldur.settings.base import make_settings_config


class JitterSettings(BaseSettings):
    """
    Jitter settings.

    Defines the random delay used to prevent a thundering herd.
    Recommended values per environment:
    - Single server: 0s (disabled)
    - K8s 10 pods: 30s
    - K8s 100+ pods: 60s
    """

    model_config = make_settings_config("BALDUR_JITTER_")

    # ==========================================================================
    # Delay Settings (from utils/jitter.py)
    # ==========================================================================
    max_delay_seconds: float = Field(
        default=60.0,
        ge=0.0,
        le=300.0,
        description="Maximum delay time (seconds)",
    )
    min_delay_seconds: float = Field(
        default=0.0,
        ge=0.0,
        le=60.0,
        description="Minimum delay time (seconds)",
    )

    # ==========================================================================
    # Startup Jitter (for AppConfig.ready())
    # ==========================================================================
    startup_max_delay_seconds: float = Field(
        default=30.0,
        ge=0.0,
        le=120.0,
        description="Maximum startup delay time (seconds)",
    )

    # ==========================================================================
    # Feature Toggle
    # ==========================================================================
    enabled: bool = Field(
        default=True,
        description="Enable jitter. If False, no delay is applied.",
    )

    # ==========================================================================
    # AdaptiveJitter thresholds (error budget based)
    # ==========================================================================
    error_budget_danger_threshold: float = Field(
        default=0.2,
        ge=0.01,
        le=0.5,
        description="Error budget danger threshold. At or below this level, maximum jitter is applied.",
    )
    error_budget_safe_threshold: float = Field(
        default=0.5,
        ge=0.3,
        le=0.9,
        description="Error budget safe threshold. At or above this level, minimum jitter is applied.",
    )

    # ==========================================================================
    # AdaptiveJitter thresholds (load based)
    # ==========================================================================
    load_high_threshold: float = Field(
        default=0.8,
        ge=0.5,
        le=0.99,
        description="High load threshold. At or above this level, the system is in danger state.",
    )
    load_low_threshold: float = Field(
        default=0.3,
        ge=0.01,
        le=0.5,
        description="Low load threshold. At or below this level, the system is in relaxed state.",
    )

    @model_validator(mode="after")
    def validate_delay_range(self) -> "JitterSettings":
        """Check min_delay <= max_delay and validate threshold ordering."""
        if self.min_delay_seconds > self.max_delay_seconds:
            raise ValueError(
                f"min_delay_seconds ({self.min_delay_seconds}) cannot be greater than "
                f"max_delay_seconds ({self.max_delay_seconds})"
            )
        if self.error_budget_danger_threshold >= self.error_budget_safe_threshold:
            raise ValueError(
                f"error_budget_danger_threshold ({self.error_budget_danger_threshold}) "
                f"must be less than error_budget_safe_threshold ({self.error_budget_safe_threshold})"
            )
        if self.load_low_threshold >= self.load_high_threshold:
            raise ValueError(
                f"load_low_threshold ({self.load_low_threshold}) "
                f"must be less than load_high_threshold ({self.load_high_threshold})"
            )
        return self


def get_jitter_settings() -> "JitterSettings":
    from baldur.settings.root import get_config

    return get_config().testing.jitter


def reset_jitter_settings() -> None:
    from baldur.settings.root import get_config

    try:
        del get_config().testing.__dict__["jitter"]
    except KeyError:
        pass
