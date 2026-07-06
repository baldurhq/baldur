"""
Precomputed Cache Settings - Pydantic v2.

Pre-computed cache settings for L3 Observability endpoint performance optimization.

Source:
- services/precomputed_cache.py

Environment Variables:
    BALDUR_PRECOMPUTED_CACHE_ENABLED=True
    BALDUR_PRECOMPUTED_CACHE_L1_TTL_SECONDS=2.0
    BALDUR_PRECOMPUTED_CACHE_L2_TTL_SECONDS=15.0
    BALDUR_PRECOMPUTED_CACHE_REFRESH_INTERVAL_SECONDS=10.0
    BALDUR_PRECOMPUTED_CACHE_L1_MAXSIZE=100
"""

import structlog
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings

from baldur.settings.base import make_settings_config

logger = structlog.get_logger()


class PrecomputedCacheSettings(BaseSettings):
    """
    Pre-computed cache settings.

    Defines multi-tier cache (L1 In-Process, L2 Redis) configuration.
    V3 optimization: settings for achieving L3 overhead under 50ms.
    """

    model_config = make_settings_config("BALDUR_PRECOMPUTED_CACHE_")

    enabled: bool = Field(
        default=True,
        description="Enable the proactive background refresh worker.",
    )

    # ==========================================================================
    # L1 In-Process Cache (from precomputed_cache.py line 66-67)
    # ==========================================================================
    l1_ttl_seconds: float = Field(
        default=2.0,
        ge=0.1,
        le=60.0,
        description="L1 in-process cache TTL (seconds). 0ms overhead.",
    )
    l1_maxsize: int = Field(
        default=100,
        ge=10,
        le=10000,
        description="L1 cache maximum entry count",
    )

    # ==========================================================================
    # L2 Redis Cache (from precomputed_cache.py line 68)
    # ==========================================================================
    l2_ttl_seconds: float = Field(
        default=15.0,
        ge=1.0,
        le=300.0,
        description="L2 Redis cache TTL (seconds). 1-5ms overhead.",
    )

    # ==========================================================================
    # Background Refresh (from precomputed_cache.py line 69)
    # ==========================================================================
    refresh_interval_seconds: float = Field(
        default=10.0,
        ge=1.0,
        le=300.0,
        description="Background refresh interval (seconds). Must be less than L2 TTL.",
    )

    # ==========================================================================
    # L3 Circuit Breaker Protection (doc 445 G1)
    # ==========================================================================
    l3_cb_enabled: bool = Field(
        default=True,
        description="Enable CB protection on L3 compute path.",
    )
    l3_cb_failure_threshold: int = Field(
        default=3,
        ge=1,
        le=20,
        description="Failure count before CB opens.",
    )
    l3_cb_recovery_timeout: int = Field(
        default=30,
        ge=5,
        le=600,
        description="Seconds before CB transitions from OPEN to HALF_OPEN.",
    )

    # ==========================================================================
    # Jitter / Backoff (doc 445 G2)
    # ==========================================================================
    jitter_enabled: bool = Field(
        default=True,
        description="Enable schedule jitter on refresh interval.",
    )
    backoff_max_delay_seconds: float = Field(
        default=300.0,
        ge=1.0,
        le=3600.0,
        description="Maximum backoff interval on consecutive failures (seconds).",
    )

    @field_validator("refresh_interval_seconds")
    @classmethod
    def validate_refresh_interval(cls, v: float) -> float:
        """Validate that refresh_interval is less than L2 TTL."""
        # Note: cross-field validation should use model_validator,
        # but we only emit a warning here
        if v > 15.0:
            logger.warning(
                "precomputed_cache_settings.cache_expire_before_refresh",
                setting_value=v,
            )
        return v


# =============================================================================
# Singleton Pattern
# =============================================================================


def get_precomputed_cache_settings() -> "PrecomputedCacheSettings":
    """
    Return cached PrecomputedCacheSettings instance.

    Returns:
        PrecomputedCacheSettings: Singleton instance
    """
    from baldur.settings.root import get_config

    return get_config().services_group.precomputed_cache


def reset_precomputed_cache_settings() -> None:
    """
    Reset cached settings (for testing).

    Call this function to reload settings after changing environment variables.
    """
    from baldur.settings.root import get_config

    try:
        del get_config().services_group.__dict__["precomputed_cache"]
    except KeyError:
        pass
