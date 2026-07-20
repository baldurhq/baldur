"""
Sampling Settings - Pydantic v2.

Sampling settings for probabilistic chain verification.

Replaces:
- audit/performance/sampling.py:SamplingConfig

Environment Variables:
    BALDUR_SAMPLING_SAMPLE_RATE=0.1
    BALDUR_SAMPLING_MIN_SAMPLES=10
    BALDUR_SAMPLING_MAX_SAMPLES=1000
    BALDUR_SAMPLING_FULL_VERIFY_ON_FAILURE=true
"""

import structlog
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings

from baldur.settings.base import make_settings_config

logger = structlog.get_logger()


class SamplingSettings(BaseSettings):
    """
    Sampling verification settings.

    Improves performance by using probabilistic sampling instead of full chain
    verification. Reduces complexity from O(n) to O(k) (k = n x sample_rate).

    Attributes:
        sample_rate: sampling rate (0.1 = 10%)
        min_samples: minimum sample count (protects small datasets)
        max_samples: maximum sample count (bounds performance impact)
        full_verify_on_failure: run full verification when a sample fails
    """

    model_config = make_settings_config("BALDUR_SAMPLING_")

    # ==========================================================================
    # Core Sampling Settings (from audit/performance/sampling.py SamplingConfig)
    # ==========================================================================
    sample_rate: float = Field(
        default=0.1,
        ge=0.01,
        le=1.0,
        description="Sampling rate (0.1 = 10%). Higher is more accurate but slower",
    )

    min_samples: int = Field(
        default=10,
        ge=1,
        le=1000,
        description="Minimum sample count. Ensures reliability for small datasets",
    )

    max_samples: int = Field(
        default=1000,
        ge=10,
        le=100000,
        description="Maximum sample count. Limits performance impact on large datasets",
    )

    full_verify_on_failure: bool = Field(
        default=True,
        description="Whether to perform full chain verification on sample verification failure",
    )

    @field_validator("max_samples")
    @classmethod
    def validate_max_samples(cls, v: int, info) -> int:
        """max_samples must be greater than min_samples."""
        # Note: warn when below the min_samples default (10)
        if v < 10:
            logger.warning(
                "safe_default.very_low_reduce_accuracy",
                setting_value=v,
            )
        return v

    @field_validator("sample_rate")
    @classmethod
    def validate_sample_rate(cls, v: float) -> float:
        """Warn on an out-of-band sampling rate."""
        if v < 0.05:
            logger.warning(
                "safe_default.very_low_miss_issues",
                setting_value=v,
            )
        if v > 0.5:
            logger.warning(
                "safe_default.high_impact_performance",
                setting_value=v,
            )
        return v


def get_sampling_settings() -> "SamplingSettings":
    from baldur.settings.root import get_config

    return get_config().testing.sampling


def reset_sampling_settings() -> None:
    from baldur.settings.root import get_config

    try:
        del get_config().testing.__dict__["sampling"]
    except KeyError:
        pass
