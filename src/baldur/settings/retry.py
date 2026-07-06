"""
Retry Settings - Pydantic v2.

Single Source of Truth for retry mechanism configuration.

Replaces:
- core/config.py:RetryConfig (lines 48-56)
- core/safe_defaults.py:SAFE_DEFAULTS["retry"]
- core/safe_defaults.py:VALIDATION_RULES["retry"]

Environment Variables:
    BALDUR_RETRY_MAX_ATTEMPTS=3
    BALDUR_RETRY_BACKOFF_STRATEGY=exponential
    BALDUR_RETRY_BASE_DELAY=1.0
    ... etc

Note:
    Legacy backoff fields (backoff_base, min_delay, jitter_percent) moved to
    BackoffSettings.legacy_* per 359_SETTINGS_INTERNAL_QUALITY_IMPROVEMENT.md (Option B).
    Env vars: BALDUR_BACKOFF_LEGACY_BASE, BALDUR_BACKOFF_LEGACY_MIN_DELAY,
    BALDUR_BACKOFF_LEGACY_JITTER_PERCENT.

Reference:
- docs/baldur/middleware_system/40_PYDANTIC_CONFIG_MIGRATION.md
"""

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings

from baldur.settings.base import make_settings_config
from baldur.settings.field_types import (
    STANDARD_BASE_DELAY,
    STANDARD_MAX_DELAY,
    STANDARD_RETRY_COUNT,
    LongDuration,
    ShortDuration,
    SmallCount,
)
from baldur.settings.validators import warn_above

# Valid backoff strategies (from core/safe_defaults.py)
VALID_BACKOFF_STRATEGIES = {"exponential", "linear", "constant", "decorrelated_jitter"}


class RetrySettings(BaseSettings):
    """
    Retry mechanism configuration with validation.

    All defaults match core/config.py:RetryConfig
    All validation rules match core/safe_defaults.py:VALIDATION_RULES["retry"]

    Note:
        Legacy backoff fields (backoff_base, min_delay, jitter_percent) are
        now in BackoffSettings (settings/backoff.py) as legacy_base,
        legacy_min_delay, legacy_jitter_percent. See doc 359 Option B.
    """

    model_config = make_settings_config("BALDUR_RETRY_")

    # ==========================================================================
    # Master Toggle
    # ==========================================================================
    enabled: bool = Field(
        default=True,
        description="Enable/disable retry globally. When False, RetryPolicy executes "
        "the function once without retry.",
    )

    # ==========================================================================
    # Core Settings (from core/config.py lines 50-56)
    # Validation rules from core/safe_defaults.py lines 246-252
    # ==========================================================================
    max_attempts: SmallCount = Field(
        default=STANDARD_RETRY_COUNT,
        description="Maximum number of retry attempts",
    )
    backoff_strategy: str = Field(
        default="exponential",
        description="Backoff strategy: exponential, linear, constant, decorrelated_jitter",
    )
    base_delay: ShortDuration = Field(
        default=STANDARD_BASE_DELAY,
        description="Base delay in seconds",
    )
    max_delay: LongDuration = Field(
        default=STANDARD_MAX_DELAY,
        description="Maximum delay cap in seconds",
    )

    @field_validator("backoff_strategy")
    @classmethod
    def validate_backoff_strategy(cls, v: str) -> str:
        """Validate backoff strategy is one of the allowed values."""
        if v not in VALID_BACKOFF_STRATEGIES:
            raise ValueError(
                f"backoff_strategy must be one of {VALID_BACKOFF_STRATEGIES}, got '{v}'"
            )
        return v

    @field_validator("max_delay")
    @classmethod
    def _warn_max_delay(cls, v: float) -> float:
        """Warn if max_delay is very high."""
        return warn_above(600, "safe_default.high_consider_using_responsiveness")(v)


# =============================================================================
# Singleton Pattern (cached settings)
# =============================================================================


def get_retry_settings() -> "RetrySettings":
    from baldur.settings.root import get_config

    return get_config().core.retry


def reset_retry_settings() -> None:
    from baldur.settings.root import get_config

    try:
        del get_config().core.__dict__["retry"]
    except KeyError:
        pass
