"""
Rate Limit Coordinator - Models

Dataclasses for rate limit coordination configuration and results. The
cooldown-deferral signal, ``RateLimitDeferredError``, is defined in
``baldur.core.exceptions`` (it is part of the retry loops' default
non-retryable set, which must not import this package) and re-exported here
so every existing import path keeps working.
"""

from __future__ import annotations

from dataclasses import dataclass

from baldur.core.exceptions import RateLimitDeferredError
from baldur.settings import get_config

__all__ = [
    "RateLimitCoordinatorConfig",
    "RateLimitDeferredError",
    "RateLimitResult",
]


@dataclass
class RateLimitCoordinatorConfig:
    """Configuration for rate limit coordination."""

    # Backoff settings
    base_delay: float = 1.0  # Base delay in seconds
    max_delay: float = 60.0  # Maximum delay cap
    jitter_percent: float = 30.0  # ±30% random jitter

    # 429 response settings
    default_retry_after: float = 5.0  # Default if no Retry-After header

    # Cooldown multiplier for consecutive 429s. The cooldown a 429 installs is:
    #
    #   ladder = jitter(default_retry_after * multiplier^(consecutive - 1))
    #            hard-capped at max_delay
    #   delay  = max(_MIN_COOLDOWN_SECONDS, ladder)                  # no header
    #   delay  = max(_MIN_COOLDOWN_SECONDS,
    #                min(retry_after_ceiling, max(retry_after, ladder)))
    #                                                                # header present
    #
    # i.e. a provider Retry-After acts as a floor (never undercut, never jittered)
    # bounded by retry_after_ceiling, while the ladder remains Baldur's own guard
    # against a provider that keeps 429ing with a small header.
    backoff_multiplier: float = 2.0

    # EventBus debouncing settings
    debounce_window_seconds: float = 5.0  # Prevent duplicate events within this window

    # Upper bound on an honored provider Retry-After header (seconds).
    retry_after_ceiling: float = 3600.0

    @classmethod
    def from_settings(cls) -> RateLimitCoordinatorConfig:
        """Load configuration from the rate-limit backoff settings."""
        backoff = get_config().scaling.rate_limit_backoff

        return cls(
            base_delay=backoff.base_delay,
            max_delay=backoff.max_delay,
            jitter_percent=backoff.jitter_percent,
            default_retry_after=backoff.default_retry_after,
            backoff_multiplier=backoff.backoff_multiplier,
            debounce_window_seconds=backoff.debounce_window_seconds,
            retry_after_ceiling=backoff.retry_after_ceiling,
        )


@dataclass
class RateLimitResult:
    """Result of a rate limit check or wait operation."""

    waited: bool = False
    wait_time: float = 0.0
    was_rate_limited: bool = False
    consecutive_429s: int = 0
    is_canary: bool = False
    """First request right after a cooldown — recovery scout mode."""

    deferred: bool = False
    """The remaining cooldown exceeded the caller's bound, so nothing further was waited.

    A deferral is a refusal, not a permit: the caller must not proceed with the
    request before ``not_before``. Waiting a shorter slice would not make the
    request legal any sooner, so no partial sleep is performed. A deferral
    decided at entry slept nothing (``waited=False``); one decided after a
    served segment — a peer extended the cooldown past what was left of the
    bound — reports the time already slept in ``wait_time`` (``waited=True``).
    Read ``deferred`` before ``waited``.
    """

    not_before: float | None = None
    """Earliest Unix timestamp at which a request may be retried (deferral only)."""
