"""
Rate Limit Coordinator - Models

Dataclasses for rate limit coordination configuration and results, plus the
cooldown-deferral signal raised when a required wait exceeds the caller's bound.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from baldur.core.exceptions import ResilienceError
from baldur.settings import get_config


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
    """The remaining cooldown exceeded the caller's bound, so nothing was waited.

    A deferral is a refusal, not a permit: the caller must not proceed with the
    request before ``not_before``. Waiting a shorter slice would not make the
    request legal any sooner, so no partial sleep is performed.
    """

    not_before: float | None = None
    """Earliest Unix timestamp at which a request may be retried (deferral only)."""


class RateLimitDeferredError(ResilienceError):
    """Raised when an outbound 429 cooldown outlasts the caller's wait budget.

    Outbound cooldown deferral signal — distinct from ``RateLimitExceeded``
    (inbound limiter rejection) and ``RateLimitStorageError`` (storage-backend
    failure).

    The call was never attempted: retrying it at or after ``not_before`` is
    safe, which makes this signal suitable for a DLQ/scheduler requeue.
    """

    def __init__(
        self,
        message: str = "",
        *,
        key: str = "",
        not_before: float | None = None,
    ):
        if not message:
            message = f"Rate limit cooldown deferred: key={key!r}"
            if not_before is not None:
                import time

                message += f", retry in {max(0.0, not_before - time.time()):.1f}s"
        super().__init__(message)
        self.key = key
        self.not_before = not_before

    def extra_context(self) -> dict[str, Any]:
        return {"key": self.key, "not_before": self.not_before}
