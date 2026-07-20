"""
Backpressure Settings - Pydantic v2.

Auto-Scaling & Backpressure configuration.
Traffic control settings (role-separated from the existing ScaleSettings).

Moved from: scaling/config.py (location unification)

Environment variable prefix: BALDUR_BACKPRESSURE_
"""

from __future__ import annotations

from enum import Enum

from pydantic import Field
from pydantic_settings import BaseSettings

from baldur.settings.base import make_settings_config
from baldur.settings.field_types import (
    Percentage,
    Probability,
    ShortInterval,
)


class BackpressureLevel(str, Enum):
    """
    Backpressure level.

    Represents the system load state derived from queue size.
    Ordering: NONE < LOW < MEDIUM < HIGH < CRITICAL (severity-based).
    """

    NONE = "none"  # Normal
    LOW = "low"  # Slightly overloaded
    MEDIUM = "medium"  # Moderately overloaded
    HIGH = "high"  # Heavily overloaded
    CRITICAL = "critical"  # Critical (emergency action required)

    @property
    def severity(self) -> int:
        """Numeric severity for ordering comparisons."""
        return _BP_SEVERITY_ORDER[self]

    def __ge__(self, other: object) -> bool:
        if isinstance(other, BackpressureLevel):
            return _BP_SEVERITY_ORDER[self] >= _BP_SEVERITY_ORDER[other]
        return NotImplemented

    def __gt__(self, other: object) -> bool:
        if isinstance(other, BackpressureLevel):
            return _BP_SEVERITY_ORDER[self] > _BP_SEVERITY_ORDER[other]
        return NotImplemented

    def __le__(self, other: object) -> bool:
        if isinstance(other, BackpressureLevel):
            return _BP_SEVERITY_ORDER[self] <= _BP_SEVERITY_ORDER[other]
        return NotImplemented

    def __lt__(self, other: object) -> bool:
        if isinstance(other, BackpressureLevel):
            return _BP_SEVERITY_ORDER[self] < _BP_SEVERITY_ORDER[other]
        return NotImplemented


_BP_SEVERITY_ORDER: dict[BackpressureLevel, int] = {
    BackpressureLevel.NONE: 0,
    BackpressureLevel.LOW: 1,
    BackpressureLevel.MEDIUM: 2,
    BackpressureLevel.HIGH: 3,
    BackpressureLevel.CRITICAL: 4,
}


class BackpressureStrategy(str, Enum):
    """
    Backpressure strategy.

    Decides how to respond when the system is overloaded.
    """

    DROP_OLDEST = "drop_oldest"  # Drop the oldest items
    DROP_NEWEST = "drop_newest"  # Drop the newest items
    REJECT = "reject"  # Reject (HTTP 503)
    THROTTLE = "throttle"  # Apply rate limiting
    QUEUE = "queue"  # Enqueue for later


# =============================================================================
# AIMD (Additive Increase, Multiplicative Decrease) pattern
# Per-level rate reduction multipliers
# =============================================================================

LEVEL_RATE_MULTIPLIERS: dict[BackpressureLevel, float] = {
    BackpressureLevel.NONE: 1.0,  # Normal: 100% throughput
    BackpressureLevel.LOW: 1.0,  # Slight: unchanged
    BackpressureLevel.MEDIUM: 0.9,  # Moderate: reduce to 90%
    BackpressureLevel.HIGH: 0.8,  # High: reduce to 80%
    BackpressureLevel.CRITICAL: 0.5,  # Critical: drop to 50% (the MD in AIMD)
}


class BackpressureSettings(BaseSettings):
    """
    Auto-Scaling & Backpressure configuration.

    Environment variables:
        BALDUR_BACKPRESSURE_ENABLED=true
        BALDUR_BACKPRESSURE_DEFAULT_STRATEGY=throttle
        BALDUR_BACKPRESSURE_MAX_RATE_PER_SECOND=1000
        ...
    """

    model_config = make_settings_config("BALDUR_BACKPRESSURE_")

    # Backpressure activation
    backpressure_enabled: bool = Field(
        default=False,
        description="Enable/disable backpressure",
    )

    # Default strategy
    default_strategy: BackpressureStrategy = Field(
        default=BackpressureStrategy.THROTTLE,
        description="Default backpressure strategy",
    )

    # Queue thresholds (level determined by queue size)
    queue_low_threshold: int = Field(
        default=100,
        ge=1,
        description="LOW level queue size threshold",
    )
    queue_medium_threshold: int = Field(
        default=500,
        ge=1,
        description="MEDIUM level queue size threshold",
    )
    queue_high_threshold: int = Field(
        default=1000,
        ge=1,
        description="HIGH level queue size threshold",
    )
    queue_critical_threshold: int = Field(
        default=5000,
        ge=1,
        description="CRITICAL level queue size threshold",
    )

    # Rate limit (items processed per second)
    max_rate_per_second: float = Field(
        default=1000.0,
        ge=1.0,
        description="Maximum processing rate (items/second)",
    )
    min_rate_per_second: float = Field(
        default=10.0,
        ge=1.0,
        description="Minimum processing rate (items/second)",
    )

    # Rate adjustment parameters
    rate_increase_factor: float = Field(
        default=1.1,
        ge=1.0,
        le=2.0,
        description="Rate increase factor (on recovery)",
    )
    rate_adjust_interval_seconds: float = Field(
        default=5.0,
        ge=1.0,
        description="Rate adjustment interval (seconds)",
    )

    # Queue size caching (avoids Redis network latency)
    queue_size_cache_ttl_seconds: float = Field(
        default=2.0,
        ge=0.5,
        le=10.0,
        description="Queue size cache TTL (seconds)",
    )

    # Prometheus metrics settings
    metrics_enabled: bool = Field(
        default=False,
        description="Enable Prometheus metrics",
    )
    metrics_prefix: str = Field(
        default="baldur_",
        description="Metrics name prefix",
    )

    # HPA settings
    hpa_enabled: bool = Field(
        default=False,
        description="Enable HPA custom metrics",
    )
    hpa_target_queue_depth: int = Field(
        default=100,
        ge=1,
        description="HPA target queue depth",
    )

    # Multi-process Redis sync for LS stats (ENT)
    redis_sync_enabled: bool = Field(
        default=False,
        description="Enable periodic Redis sync for multi-process LS stats",
    )

    # Graceful Degradation
    graceful_degradation_enabled: bool = Field(
        default=False,
        description="Enable graceful degradation",
    )

    # CPU-usage-based rate decay thresholds
    resource_cpu_high_threshold: Percentage = Field(
        default=80.0,
        description="Reduce rate to 50% when CPU usage exceeds this threshold",
    )
    resource_cpu_critical_threshold: Percentage = Field(
        default=90.0,
        description="Reduce rate to 10% when CPU usage exceeds this threshold",
    )

    # 503 response customization
    reject_message: str = Field(
        default="Service temporarily unavailable due to high load",
        description="Response message for 503 rejection",
    )
    reject_retry_after_seconds: ShortInterval = Field(
        default=5,
        description="Retry-After header value (seconds)",
    )

    # =========================================================================
    # Priority Watermark — remaining-token ratio thresholds
    # Requests of a tier are rejected when the current token ratio falls below
    # its value. Example env var: BALDUR_BACKPRESSURE_WATERMARK_STANDARD=0.4
    # =========================================================================
    watermark_critical: Probability = Field(
        default=0.0,
        description="Critical tier watermark threshold. Reject when token ratio falls below this.",
    )
    watermark_standard: Probability = Field(
        default=0.3,
        description="Standard tier watermark threshold. Reject when token ratio falls below this.",
    )
    watermark_non_essential: Probability = Field(
        default=0.6,
        description="Non-essential tier watermark threshold. Reject when token ratio falls below this.",
    )

    # =========================================================================
    # External Level TTL — Throttle SLA → RateController bridge
    # =========================================================================
    external_level_ttl_seconds: float = Field(
        default=30.0,
        ge=5.0,
        le=120.0,
        description=(
            "TTL for external backpressure level from Throttle SLA events. "
            "Acts as a lease — each event reception renews the TTL."
        ),
    )

    def get_level_for_queue_size(self, queue_size: int) -> BackpressureLevel:
        """
        Return the backpressure level for a given queue size.

        Args:
            queue_size: Current queue size

        Returns:
            BackpressureLevel: The matching level
        """
        if queue_size >= self.queue_critical_threshold:
            return BackpressureLevel.CRITICAL
        if queue_size >= self.queue_high_threshold:
            return BackpressureLevel.HIGH
        if queue_size >= self.queue_medium_threshold:
            return BackpressureLevel.MEDIUM
        if queue_size >= self.queue_low_threshold:
            return BackpressureLevel.LOW
        return BackpressureLevel.NONE

    def get_rate_multiplier(self, level: BackpressureLevel) -> float:
        """
        Return the per-level rate reduction multiplier (AIMD pattern).

        Args:
            level: Backpressure level

        Returns:
            float: Rate multiplier (0.0 ~ 1.0)
        """
        return LEVEL_RATE_MULTIPLIERS.get(level, 1.0)

    def get_priority_watermarks(self) -> dict[str, float]:
        """Return the per-tier watermark threshold dictionary."""
        return {
            "critical": self.watermark_critical,
            "standard": self.watermark_standard,
            "non_essential": self.watermark_non_essential,
        }

    def get_retry_after_for_level(self, level: BackpressureLevel) -> int:
        """Return the Retry-After value for a BackpressureLevel.

        The higher the load, the longer the client retry interval, which
        prevents a retry storm.
        Computed as base * multiplier, so a single settings knob scales the
        whole range.

        Args:
            level: Current backpressure level

        Returns:
            Retry-After value (seconds)
        """
        base = self.reject_retry_after_seconds
        multiplier = {
            BackpressureLevel.NONE: 1,
            BackpressureLevel.LOW: 1,
            BackpressureLevel.MEDIUM: 2,
            BackpressureLevel.HIGH: 4,
            BackpressureLevel.CRITICAL: 8,
        }
        return base * multiplier.get(level, 1)


def get_backpressure_settings() -> BackpressureSettings:
    from baldur.settings.root import get_config

    return get_config().scaling.backpressure


def reset_backpressure_settings() -> None:
    from baldur.settings.root import get_config

    try:
        del get_config().scaling.__dict__["backpressure"]
    except KeyError:
        pass
