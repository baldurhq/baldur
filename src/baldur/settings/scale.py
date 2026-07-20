"""
Enterprise Scale Settings - Pydantic v2.

Unified configuration for large-scale audit event processing in enterprise
environments.
Selecting a profile adjusts the related settings as a group; individual
overrides are also supported.

Environment Variables:
    BALDUR_SCALE_PROFILE=enterprise
    BALDUR_SCALE_MAX_EVENTS_PER_REQUEST=50000
    BALDUR_SCALE_MAX_EVENTS_PER_SECOND=200000
    BALDUR_SCALE_RING_BUFFER_CAPACITY=1000000
    BALDUR_SCALE_BATCH_SIZE=1000
    BALDUR_SCALE_FLUSH_INTERVAL_SECONDS=1.0
"""

from enum import Enum

from pydantic import Field
from pydantic_settings import BaseSettings

from baldur.settings.base import make_settings_config


class ScaleProfile(str, Enum):
    """
    Predefined scale profiles.

    Provides sensible defaults for the size of the environment.
    """

    DEVELOPMENT = "development"  # Development/test environment
    SMALL_BUSINESS = "small"  # Small scale (1-10 pods)
    MEDIUM_BUSINESS = "medium"  # Medium scale (10-50 pods)
    ENTERPRISE = "enterprise"  # Enterprise (50+ pods)
    HIGH_THROUGHPUT = "high"  # Very high throughput (100,000+ RPS)


# Per-profile default values
PROFILE_DEFAULTS: dict[ScaleProfile, dict[str, int | float]] = {
    ScaleProfile.DEVELOPMENT: {
        "max_events_per_request": 100,
        "max_events_per_second": 1000,
        "ring_buffer_capacity": 10000,
        "batch_size": 10,
        "flush_interval": 5.0,
    },
    ScaleProfile.SMALL_BUSINESS: {
        "max_events_per_request": 1000,
        "max_events_per_second": 10000,
        "ring_buffer_capacity": 100000,
        "batch_size": 100,
        "flush_interval": 3.0,
    },
    ScaleProfile.MEDIUM_BUSINESS: {
        "max_events_per_request": 10000,
        "max_events_per_second": 50000,
        "ring_buffer_capacity": 500000,
        "batch_size": 500,
        "flush_interval": 2.0,
    },
    ScaleProfile.ENTERPRISE: {
        "max_events_per_request": 50000,
        "max_events_per_second": 200000,
        "ring_buffer_capacity": 1000000,
        "batch_size": 1000,
        "flush_interval": 1.0,
    },
    ScaleProfile.HIGH_THROUGHPUT: {
        "max_events_per_request": 100000,
        "max_events_per_second": 1000000,
        "ring_buffer_capacity": 5000000,
        "batch_size": 5000,
        "flush_interval": 0.5,
    },
}


class ScaleSettings(BaseSettings):
    """
    Unified Enterprise Scale configuration.

    Selecting a profile adjusts the related settings as a group.
    Overriding individual settings is also supported.
    """

    model_config = make_settings_config("BALDUR_SCALE_")

    # ==========================================================================
    # Scale Profile
    # ==========================================================================
    profile: ScaleProfile = Field(
        default=ScaleProfile.DEVELOPMENT,
        description="Scale profile. Defaults auto-adjust based on selected profile.",
    )

    # ==========================================================================
    # Per-Request Limits (individual override)
    # ==========================================================================
    max_events_per_request: int | None = Field(
        default=None,
        ge=10,
        le=1000000,
        description="Maximum events per request (None uses profile default)",
    )

    # ==========================================================================
    # Throughput Limits (individual override)
    # ==========================================================================
    max_events_per_second: int | None = Field(
        default=None,
        ge=100,
        le=10000000,
        description="Maximum events per second (None uses profile default)",
    )

    # ==========================================================================
    # Buffer Sizes (individual override)
    # ==========================================================================
    ring_buffer_capacity: int | None = Field(
        default=None,
        ge=1000,
        le=10000000,
        description="RingBuffer capacity (None uses profile default)",
    )

    # ==========================================================================
    # Batch Settings (individual override)
    # ==========================================================================
    batch_size: int | None = Field(
        default=None,
        ge=1,
        le=100000,
        description="Batch size (None uses profile default)",
    )

    flush_interval_seconds: float | None = Field(
        default=None,
        ge=0.1,
        le=60.0,
        description="Flush interval (None uses profile default)",
    )

    # ==========================================================================
    # Effective Value Properties (computed from the profile)
    # ==========================================================================
    @property
    def effective_max_events_per_request(self) -> int:
        """Profile-derived effective max_events_per_request."""
        if self.max_events_per_request is not None:
            return self.max_events_per_request
        return int(PROFILE_DEFAULTS[self.profile]["max_events_per_request"])

    @property
    def effective_max_events_per_second(self) -> int:
        """Profile-derived effective max_events_per_second."""
        if self.max_events_per_second is not None:
            return self.max_events_per_second
        return int(PROFILE_DEFAULTS[self.profile]["max_events_per_second"])

    @property
    def effective_ring_buffer_capacity(self) -> int:
        """Profile-derived effective ring_buffer_capacity."""
        if self.ring_buffer_capacity is not None:
            return self.ring_buffer_capacity
        return int(PROFILE_DEFAULTS[self.profile]["ring_buffer_capacity"])

    @property
    def effective_batch_size(self) -> int:
        """Profile-derived effective batch_size."""
        if self.batch_size is not None:
            return self.batch_size
        return int(PROFILE_DEFAULTS[self.profile]["batch_size"])

    @property
    def effective_flush_interval(self) -> float:
        """Profile-derived effective flush_interval."""
        if self.flush_interval_seconds is not None:
            return self.flush_interval_seconds
        return float(PROFILE_DEFAULTS[self.profile]["flush_interval"])


# ==========================================================================
# Singleton management
# ==========================================================================
def get_scale_settings() -> "ScaleSettings":
    """Get cached ScaleSettings instance."""
    from baldur.runtime import get_runtime

    return get_runtime().get_settings(ScaleSettings)


def reset_scale_settings() -> None:
    """Reset cached settings (for testing)."""
    from baldur.runtime import get_runtime

    get_runtime().reset_settings(ScaleSettings)
