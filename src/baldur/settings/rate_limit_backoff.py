"""
Rate Limit Backoff Settings - Pydantic v2.

Outbound 429-response coordination dials: how the rate-limit coordinator
shapes delays when a downstream returns 429. Distinct from both:

- ``baldur.settings.backoff`` — general execution retry backoff strategies;
  this family shapes delays derived from rate-limit (429) responses only.
- ``baldur.settings.rate_limit`` (``BALDUR_RATE_LIMIT_``) — inbound HTTP
  quota dials (Control-API / middleware / decorator rate limiting).

Environment Variables:
    BALDUR_RATE_LIMIT_BACKOFF_BASE_DELAY=1.0
    BALDUR_RATE_LIMIT_BACKOFF_MAX_DELAY=60.0
    BALDUR_RATE_LIMIT_BACKOFF_JITTER_PERCENT=30.0
    BALDUR_RATE_LIMIT_BACKOFF_DEFAULT_RETRY_AFTER=5.0
    BALDUR_RATE_LIMIT_BACKOFF_BACKOFF_MULTIPLIER=2.0
    BALDUR_RATE_LIMIT_BACKOFF_DEBOUNCE_WINDOW_SECONDS=5.0
    BALDUR_RATE_LIMIT_BACKOFF_RETRY_AFTER_CEILING=3600.0
"""

from pydantic import Field
from pydantic_settings import BaseSettings

from baldur.settings.base import make_settings_config
from baldur.settings.field_types import (
    STANDARD_BACKOFF_MULTIPLIER,
    STANDARD_BASE_DELAY,
    BackoffMultiplier,
    Percentage,
    ShortDuration,
)

__all__ = [
    "RateLimitBackoffSettings",
    "get_rate_limit_backoff_settings",
    "reset_rate_limit_backoff_settings",
]


class RateLimitBackoffSettings(BaseSettings):
    """
    Outbound 429-backoff coordination configuration.

    Consumed by the rate-limit coordinator to shape delays in response to
    429s from downstream services. Inbound HTTP quota dials live in
    ``RateLimitSettings`` (``BALDUR_RATE_LIMIT_``).
    """

    model_config = make_settings_config("BALDUR_RATE_LIMIT_BACKOFF_")

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
    debounce_window_seconds: ShortDuration = Field(
        default=5.0,
        description=(
            "EventBus debounce window (seconds) suppressing duplicate "
            "rate-limit events for the same service."
        ),
    )
    # Explicit Field rather than LongDuration: the honored-header range must reach
    # a full day, well past LongDuration's le=3600 (same reason as max_delay above).
    retry_after_ceiling: float = Field(
        default=3600.0,
        ge=60.0,
        le=86400.0,
        description=(
            "Upper bound (seconds) on an honored provider Retry-After header. "
            "Headers above this are clamped and marked; max_delay does not "
            "bound honored headers."
        ),
    )


# =============================================================================
# Singleton Pattern (cached settings)
# =============================================================================


def get_rate_limit_backoff_settings() -> "RateLimitBackoffSettings":
    from baldur.settings.root import get_config

    return get_config().scaling.rate_limit_backoff


def reset_rate_limit_backoff_settings() -> None:
    from baldur.settings.root import get_config

    try:
        del get_config().scaling.__dict__["rate_limit_backoff"]
    except KeyError:
        pass
