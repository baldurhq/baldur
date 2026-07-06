"""
Reusable Annotated field types and shared default constants for settings.

Provides standardized Field constraint patterns that appear across 10+ settings files.
Individual settings files import these types instead of repeating ge/le constraints.

Usage:
    from baldur.settings.field_types import Probability, SmallCount

    class MySettings(BaseSettings):
        error_rate: Probability = 0.5
        max_retries: SmallCount = STANDARD_RETRY_COUNT
"""

from __future__ import annotations

from typing import Annotated

from pydantic import Field

# =========================================================================
# Annotated Field Types — Ratio / Probability
# =========================================================================
Probability = Annotated[float, Field(ge=0.0, le=1.0)]
"""0.0-1.0 ratio/probability field. Used for error rates, weights, factors."""

StrictProbability = Annotated[float, Field(ge=0.01, le=1.0)]
"""0.01-1.0 ratio field excluding zero. Used for non-zero error thresholds."""

Percentage = Annotated[float, Field(ge=0.0, le=100.0)]
"""0-100 percentage field."""


# =========================================================================
# Annotated Field Types — Count / Size
# =========================================================================
TinyCount = Annotated[int, Field(ge=1, le=10)]
"""1-10 integer. Used for small multipliers, retry bases."""

SmallCount = Annotated[int, Field(ge=1, le=20)]
"""1-20 integer. Used for retry attempts, small limits."""

MediumCount = Annotated[int, Field(ge=1, le=100)]
"""1-100 integer. Used for thresholds, moderate limits."""

LargeCount = Annotated[int, Field(ge=1, le=1000)]
"""1-1000 integer. Used for batch sizes, buffer sizes."""

HugeCount = Annotated[int, Field(ge=1, le=10000)]
"""1-10000 integer. Used for rate limits, large buffers."""

ZeroableSmallCount = Annotated[int, Field(ge=0, le=10)]
"""0-10 integer. Used for optional counts (0 = disabled)."""


# =========================================================================
# Annotated Field Types — Duration (seconds)
# =========================================================================
ShortDuration = Annotated[float, Field(ge=0.1, le=60.0)]
"""0.1-60s duration. Used for base delays, short timeouts."""

MediumDuration = Annotated[float, Field(ge=1.0, le=600.0)]
"""1-600s (10min) duration. Used for recovery timeouts, intervals."""

LongDuration = Annotated[float, Field(ge=1.0, le=3600.0)]
"""1-3600s (1hr) duration. Used for max delays, long TTLs."""

IntervalDuration = Annotated[int, Field(ge=1, le=3600)]
"""1-3600s integer interval. Used for window sizes, integer timeouts."""

ShortInterval = Annotated[int, Field(ge=1, le=60)]
"""1-60s integer interval. Used for short integer timeouts."""


# =========================================================================
# Annotated Field Types — Multiplier
# =========================================================================
BackoffMultiplier = Annotated[float, Field(ge=1.0, le=10.0)]
"""1.0-10.0 multiplier. Used for backoff, DDoS, scaling multipliers."""

JitterFactor = Annotated[float, Field(ge=0.0, le=1.0)]
"""0.0-1.0 jitter factor. Alias for Probability with jitter semantics."""


# =========================================================================
# Shared Default Constants — Retry / Backoff
# =========================================================================
STANDARD_RETRY_COUNT: int = 3
"""Standard retry attempt count across the framework."""

STANDARD_BASE_DELAY: float = 1.0
"""Standard base delay in seconds for backoff strategies."""

STANDARD_MAX_DELAY: float = 60.0
"""Standard maximum delay cap in seconds (1 minute). Aligned with CB recovery_timeout default."""

STANDARD_BACKOFF_MULTIPLIER: float = 2.0
"""Standard backoff multiplier for exponential strategies."""

STANDARD_JITTER_FACTOR: float = 0.2
"""Standard jitter factor for backoff randomization."""


# =========================================================================
# Shared Default Constants — Timeout / Interval
# =========================================================================
STANDARD_TIMEOUT_SECONDS: float = 30.0
"""Standard operation timeout in seconds."""

STANDARD_CHECK_INTERVAL: float = 5.0
"""Standard health check / monitoring interval in seconds."""


# =========================================================================
# Shared Default Constants — Size / Capacity
# =========================================================================
STANDARD_BATCH_SIZE: int = 100
"""Standard batch/buffer size for processing."""

STANDARD_POOL_SIZE: int = 10
"""Standard connection/thread pool size."""


__all__ = [
    # Annotated types
    "Probability",
    "StrictProbability",
    "Percentage",
    "TinyCount",
    "SmallCount",
    "MediumCount",
    "LargeCount",
    "HugeCount",
    "ZeroableSmallCount",
    "ShortDuration",
    "MediumDuration",
    "LongDuration",
    "IntervalDuration",
    "ShortInterval",
    "BackoffMultiplier",
    "JitterFactor",
    # Constants
    "STANDARD_RETRY_COUNT",
    "STANDARD_BASE_DELAY",
    "STANDARD_MAX_DELAY",
    "STANDARD_BACKOFF_MULTIPLIER",
    "STANDARD_JITTER_FACTOR",
    "STANDARD_TIMEOUT_SECONDS",
    "STANDARD_CHECK_INTERVAL",
    "STANDARD_BATCH_SIZE",
    "STANDARD_POOL_SIZE",
]
