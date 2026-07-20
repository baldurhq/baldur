"""
CBStateCache Settings - Pydantic v2.

Circuit breaker state cache settings.
TTL and jitter range are configurable via environment variables.

Environment Variables:
    BALDUR_STATE_CACHE_BASE_TTL=5.0
    BALDUR_STATE_CACHE_JITTER_RANGE=0.5
"""

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings

from baldur.settings.base import make_settings_config


class StateCacheSettings(BaseSettings):
    """
    CBStateCache settings.

    TTL-based local caching to minimize network calls.
    Polling jitter to prevent a thundering herd.
    """

    model_config = make_settings_config("BALDUR_STATE_CACHE_")

    # ==========================================================================
    # TTL settings
    # ==========================================================================
    base_ttl: float = Field(
        default=5.0,
        ge=0.1,
        le=300.0,
        description="Base cache TTL (seconds). Cache invalidates after this duration.",
    )

    # ==========================================================================
    # Jitter settings
    # ==========================================================================
    jitter_range: float = Field(
        default=0.5,
        ge=0.0,
        le=10.0,
        description="Random jitter range (seconds). Applies +/-jitter_range randomly to TTL.",
    )

    @model_validator(mode="after")
    def validate_jitter(self) -> "StateCacheSettings":
        """jitter_range must not exceed base_ttl."""
        if self.jitter_range > self.base_ttl:
            raise ValueError(
                f"jitter_range ({self.jitter_range}) should not exceed "
                f"base_ttl ({self.base_ttl})"
            )
        return self


# =============================================================================
# Singleton Pattern
# =============================================================================


def get_state_cache_settings() -> "StateCacheSettings":
    """
    Return the cached StateCacheSettings instance.

    Returns:
        StateCacheSettings: singleton instance
    """
    from baldur.settings.root import get_config

    return get_config().scaling.state_cache


def reset_state_cache_settings() -> None:
    """
    Reset cached settings (for testing).
    """
    from baldur.settings.root import get_config

    try:
        del get_config().scaling.__dict__["state_cache"]
    except KeyError:
        pass
