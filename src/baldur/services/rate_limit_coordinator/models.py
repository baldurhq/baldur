"""
Rate Limit Coordinator - Models

Dataclasses for rate limit coordination configuration and results.
"""

from __future__ import annotations

from dataclasses import dataclass

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

    # Cooldown multiplier for consecutive 429s
    # delay = min(base_delay * (2 ^ consecutive_429s), max_delay)
    backoff_multiplier: float = 2.0

    # EventBus debouncing settings
    debounce_window_seconds: float = 5.0  # Prevent duplicate events within this window

    @classmethod
    def from_settings(cls) -> RateLimitCoordinatorConfig:
        """Load configuration from core config."""
        rate_limit = get_config().scaling.rate_limit

        return cls(
            base_delay=rate_limit.base_delay,
            max_delay=rate_limit.max_delay,
            jitter_percent=rate_limit.jitter_percent,
            default_retry_after=rate_limit.default_retry_after,
            backoff_multiplier=rate_limit.backoff_multiplier,
        )


@dataclass
class RateLimitResult:
    """Result of a rate limit check or wait operation."""

    waited: bool = False
    wait_time: float = 0.0
    was_rate_limited: bool = False
    consecutive_429s: int = 0
    is_canary: bool = False
    """Cooldown 직후 첫 요청 - 복구 정찰 요청 모드."""
