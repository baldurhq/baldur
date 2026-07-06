"""
Throttle Settings - Pydantic v2.

Netflix Gradient-based adaptive throttle settings.

Replaces:
- services/throttle/config.py:ThrottleConfig

Environment Variables:
    BALDUR_THROTTLE_INITIAL_LIMIT=100
    BALDUR_THROTTLE_MIN_LIMIT=10
    BALDUR_THROTTLE_MAX_LIMIT=500

Reference:
- docs/baldur/middleware_system/92_CONFIG_IMPLEMENTATION_GUIDE.md (Week 2 [10])
- docs/baldur/middleware_system/91_CONFIG_INVENTORY.md §7.1
"""

import structlog
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings

from baldur.settings.base import make_settings_config
from baldur.settings.field_types import (
    HugeCount,
    IntervalDuration,
    LargeCount,
    MediumCount,
    MediumDuration,
    Percentage,
    Probability,
)

logger = structlog.get_logger()


class ThrottleSettings(BaseSettings):
    """
    Netflix Gradient-based adaptive throttle settings.

    Dynamically adjusts request limits based on RTT gradient.
    """

    model_config = make_settings_config("BALDUR_THROTTLE_")

    # ==========================================================================
    # Basic Rate Limiting (from throttle/config.py)
    # ==========================================================================
    initial_limit: HugeCount = Field(
        default=100,
        description="Initial request limit (requests per window)",
    )

    window_seconds: IntervalDuration = Field(
        default=60,
        description="Window size (seconds)",
    )

    # ==========================================================================
    # Adaptive Throttling Limits
    # ==========================================================================
    min_limit: MediumCount = Field(
        default=10,
        description="Minimum limit (never drops below this value)",
    )

    max_limit: int = Field(
        default=500,
        ge=100,
        le=100000,
        description="Maximum limit (never exceeds this value)",
    )

    # ==========================================================================
    # Gradient Calculation Settings
    # ==========================================================================
    sample_interval_ms: int = Field(
        default=500,
        ge=50,
        le=5000,
        description="RTT sampling interval (ms)",
    )

    smoothing_factor: Probability = Field(
        default=0.5,
        description="Exponential smoothing factor (0-1, higher is more responsive)",
    )

    # ==========================================================================
    # Adjustment Rates
    # ==========================================================================
    decrease_ratio: float = Field(
        default=0.9,
        ge=0.5,
        le=0.99,
        description="Multiplier applied when RTT increases (decrease ratio)",
    )

    increase_step: MediumCount = Field(
        default=1,
        description="Additive step when RTT decreases (increase step)",
    )

    # ==========================================================================
    # SLA Thresholds (ms)
    # ==========================================================================
    sla_warning_ms: int = Field(
        default=200,
        ge=10,
        le=5000,
        description="RTT threshold to start throttling (ms)",
    )

    sla_critical_ms: int = Field(
        default=500,
        ge=50,
        le=10000,
        description="RTT threshold to start aggressive throttling (ms)",
    )

    # ==========================================================================
    # Circuit Breaker integration settings
    # ==========================================================================
    cb_open_limit_percent: Probability = Field(
        default=0.0,
        description="Limit ratio when CB is OPEN (0.0 = use min_limit)",
    )

    cb_half_open_limit_percent: Probability = Field(
        default=0.5,
        description="Limit ratio when CB is HALF_OPEN (50%)",
    )

    # ==========================================================================
    # Recovery Dampening settings (gradual recovery)
    # ==========================================================================
    recovery_dampening_enabled: bool = Field(
        default=True,
        description="Enable Recovery Dampening (prevents Thundering Herd)",
    )

    recovery_step_1_percent: Probability = Field(
        default=0.8,
        description="Recovery step 1 ratio (80%)",
    )

    recovery_step_2_percent: Probability = Field(
        default=0.9,
        description="Recovery step 2 ratio (90%)",
    )

    recovery_step_3_percent: Probability = Field(
        default=1.0,
        description="Recovery step 3 ratio (100%)",
    )

    recovery_step_interval_seconds: MediumDuration = Field(
        default=30.0,
        description="Interval between recovery steps (seconds)",
    )

    # ==========================================================================
    # Load Shedding integration settings
    # ==========================================================================
    shedding_compensation_factor: float = Field(
        default=1.5,
        ge=1.0,
        le=3.0,
        description="Load Shedding double-blocking prevention compensation factor. "
        "Softens Throttle limit reduction accounting for requests already blocked by Middleware.",
    )

    # ==========================================================================
    # Redis Key Prefix
    # ==========================================================================
    key_prefix: str = Field(
        default="baldur:throttle",
        description="Redis key prefix",
    )

    # ==========================================================================
    # Prometheus Metrics Label
    # ==========================================================================
    service_name: str = Field(
        default="default",
        description="Service label value for Prometheus metrics. "
        "Injected via BALDUR_THROTTLE_SERVICE_NAME env var.",
    )

    # ==========================================================================
    # DLQ integration settings (rejected request DLQ storage + auto-replay on recovery)
    # ==========================================================================
    dlq_on_rejection: bool = Field(
        default=True,
        description="Whether to store rejected requests in DLQ on throttle rejection",
    )

    auto_replay_on_recovery: bool = Field(
        default=True,
        description="Whether to auto-replay DLQ on recovery",
    )

    replay_batch_size: LargeCount = Field(
        default=10,
        description="Replay batch size",
    )

    replay_interval_ms: int = Field(
        default=100,
        ge=10,
        le=10000,
        description="Replay batch interval (ms)",
    )

    replay_min_recovery_percent: Percentage = Field(
        default=50.0,
        description="Minimum recovery percentage required to start replay (%)",
    )

    dlq_store_sampling_rate: Probability = Field(
        default=1.0,
        description="DLQ storage sampling rate (1.0 = store all)",
    )

    # ==========================================================================
    # Async audit worker backpressure (services/throttle/audit.py)
    # ==========================================================================
    audit_queue_maxsize: int = Field(
        default=10000,
        ge=100,
        le=100000,
        description=(
            "Backpressure cap for the async throttle-audit queue. Applied when "
            "the queue is first created; changing it requires a process restart."
        ),
    )

    @field_validator("max_limit")
    @classmethod
    def validate_max_limit(cls, v: int, info) -> int:
        """Ensure max_limit is greater than min_limit."""
        if v < 10:  # min_limit default
            logger.warning(
                "safe_default.very_low_cause_issues",
                setting_value=v,
            )
        return v

    @field_validator("sla_critical_ms")
    @classmethod
    def validate_sla_critical(cls, v: int, info) -> int:
        """Ensure sla_critical_ms is greater than sla_warning_ms."""
        if v < 200:
            logger.warning(
                "safe_default.lower_than_typical_warning",
                setting_value=v,
            )
        return v

    # =========================================================================
    # Backward-compatible methods (existing ThrottleConfig interface)
    # =========================================================================
    @classmethod
    def from_dict(cls, data: dict) -> "ThrottleSettings":
        """Create settings from dictionary (backward compat with ThrottleConfig)."""
        return cls(**{k: v for k, v in data.items() if k in cls.model_fields})

    @classmethod
    def from_settings(cls) -> "ThrottleSettings":
        """Create from current settings (backward compat with ThrottleConfig)."""
        return get_throttle_settings()


# =============================================================================
# Singleton Pattern
# =============================================================================


def get_throttle_settings() -> "ThrottleSettings":
    """Get cached ThrottleSettings instance."""
    from baldur.settings.root import get_config

    return get_config().scaling.throttle


def reset_throttle_settings() -> None:
    """Reset cached settings (for testing)."""
    from baldur.settings.root import get_config

    try:
        del get_config().scaling.__dict__["throttle"]
    except KeyError:
        pass
