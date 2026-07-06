"""
Rate Limit Settings - Pydantic v2.

Single Source of Truth for rate limit coordination configuration.

Replaces:
- core/config.py:RateLimitConfig (lines 137-163)
- core/safe_defaults.py:SAFE_DEFAULTS["rate_limit"]
- core/safe_defaults.py:VALIDATION_RULES["rate_limit"]

Environment Variables:
    BALDUR_RATE_LIMIT_BASE_DELAY=1.0
    BALDUR_RATE_LIMIT_MAX_DELAY=60.0
    BALDUR_RATE_LIMIT_CONTROL_API_RATE_LIMIT=100
    ... etc

Reference:
- docs/baldur/middleware_system/40_PYDANTIC_CONFIG_MIGRATION.md
"""

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings

from baldur.settings.base import make_settings_config
from baldur.settings.field_types import (
    STANDARD_BACKOFF_MULTIPLIER,
    STANDARD_BASE_DELAY,
    BackoffMultiplier,
    HugeCount,
    IntervalDuration,
    LargeCount,
    MediumCount,
    Percentage,
    ShortDuration,
)
from baldur.settings.validators import warn_above


class RateLimitSettings(BaseSettings):
    """
    Rate Limit coordination configuration with validation.

    Includes both retry backoff settings and Control API rate limiting.

    All defaults match core/config.py:RateLimitConfig
    All validation rules match core/safe_defaults.py:VALIDATION_RULES["rate_limit"]
    """

    model_config = make_settings_config("BALDUR_RATE_LIMIT_")

    # ==========================================================================
    # Retry Backoff Settings (for 429 handling)
    # From core/config.py lines 144-149
    # Validation rules from core/safe_defaults.py lines 254-261
    # ==========================================================================
    base_delay: ShortDuration = Field(
        default=STANDARD_BASE_DELAY,
        description="Base delay in seconds",
    )
    max_delay: float = Field(
        default=60.0,
        ge=1.0,
        le=300.0,
        description="Maximum delay cap in seconds",
    )
    jitter_percent: Percentage = Field(
        default=30.0,
        description="±% random jitter",
    )
    default_retry_after: ShortDuration = Field(
        default=5.0,
        description="Default delay if no Retry-After header",
    )
    backoff_multiplier: BackoffMultiplier = Field(
        default=STANDARD_BACKOFF_MULTIPLIER,
        description="Cooldown multiplier for consecutive 429s",
    )

    # ==========================================================================
    # Control API Rate Limiting (HybridRateLimitMiddleware)
    # From core/config.py lines 152-156
    # Validation rules from core/safe_defaults.py lines 258-260
    # ==========================================================================
    control_api_rate_limit: HugeCount = Field(
        default=100,
        description="Requests/minute in normal mode (Redis)",
    )
    control_api_window_seconds: IntervalDuration = Field(
        default=60,
        description="Window size for rate limiting",
    )
    emergency_rate_limit: MediumCount = Field(
        default=10,
        description="Requests/minute when Redis fails",
    )
    emergency_window_seconds: IntervalDuration = Field(
        default=60,
        description="Emergency window size",
    )

    # ==========================================================================
    # Framework-agnostic middleware rate limiting (api/middleware/rate_limit.py)
    # Used by BaldurMiddleware (FastAPI) and init_flask (Flask). Disabled by
    # default (0) so mounting the middleware for CB/backpressure protection does
    # not unexpectedly rate-limit user traffic. Operators opt in via env var or
    # per-instance kwargs on BaldurMiddleware / init_flask.
    # ==========================================================================
    middleware_rate_limit: int = Field(
        default=0,
        ge=0,
        le=10000,
        description=(
            "Framework-middleware rate limit (req/window). 0 = disabled. "
            "In-process (L1) only on FastAPI/Flask: under N worker processes "
            "the effective global limit is this value x N. Use the Django "
            "hybrid path or a shared limiter for a cluster-wide cap."
        ),
    )
    middleware_window_seconds: IntervalDuration = Field(
        default=60,
        description="Framework-middleware rate-limit window size (seconds).",
    )

    # ==========================================================================
    # Function-level @rate_limit decorator toggle (D5 of 458_DX_DECORATORS.md)
    # Distinct from middleware_rate_limit (HTTP-middleware-only).
    # When False, @rate_limit short-circuits at wrapper entry and calls the
    # wrapped function directly without consulting SlidingWindowLimiter.
    # ==========================================================================
    decorator_enabled: bool = Field(
        default=True,
        description="Enable/disable @rate_limit decorator globally. When False, "
        "decorated functions execute without rate-limit checks.",
    )

    # ==========================================================================
    # Redis Storage TTL - from adapters/rate_limit/redis_adapter.py
    # ==========================================================================
    redis_ttl: int = Field(
        default=3600,
        ge=60,
        le=86400,
        description="TTL for Rate Limit state stored in Redis (seconds). Default 1 hour.",
    )

    # ==========================================================================
    # In-memory storage cleanup cadence (adapters/rate_limit/memory_adapter.py)
    # ==========================================================================
    memory_cleanup_interval_ops: LargeCount = Field(
        default=100,
        description=(
            "Storage operations between expired-entry cleanup sweeps in the "
            "in-memory rate-limit adapter."
        ),
    )

    @field_validator("emergency_rate_limit")
    @classmethod
    def _warn_emergency_rate_limit(cls, v: int) -> int:
        """Emergency rate limit should be conservative."""
        return warn_above(50, "safe_default.high_consider_using_safety")(v)


# =============================================================================
# Singleton Pattern (cached settings)
# =============================================================================


def get_rate_limit_settings() -> "RateLimitSettings":
    from baldur.settings.root import get_config

    return get_config().scaling.rate_limit


def reset_rate_limit_settings() -> None:
    from baldur.settings.root import get_config

    try:
        del get_config().scaling.__dict__["rate_limit"]
    except KeyError:
        pass
