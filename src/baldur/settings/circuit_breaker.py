"""
Circuit Breaker Settings - Pydantic v2.

Single Source of Truth for circuit breaker configuration.

Replaces:
- core/config.py:CircuitBreakerConfig (lines 13-33)
- core/safe_defaults.py:SAFE_DEFAULTS["circuit_breaker"]
- core/safe_defaults.py:VALIDATION_RULES["circuit_breaker"]

Environment Variables:
    BALDUR_CB_ENABLED=true
    BALDUR_CB_FAILURE_THRESHOLD=5
    BALDUR_CB_RECOVERY_TIMEOUT=60
    ... etc
"""

import structlog
from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings

from baldur.settings.base import make_settings_config
from baldur.settings.field_types import (
    STANDARD_BACKOFF_MULTIPLIER,
    BackoffMultiplier,
    HugeCount,
    IntervalDuration,
    JitterFactor,
    LargeCount,
    LongDuration,
    MediumCount,
    Percentage,
    ShortDuration,
)
from baldur.settings.validators import warn_above

logger = structlog.get_logger()


class CircuitBreakerSettings(BaseSettings):
    """
    Circuit Breaker configuration with validation.

    All defaults match core/config.py:CircuitBreakerConfig
    All validation rules match core/safe_defaults.py:VALIDATION_RULES["circuit_breaker"]
    """

    model_config = make_settings_config("BALDUR_CB_")

    # ==========================================================================
    # Core Settings (from core/config.py lines 17-23)
    # ==========================================================================
    enabled: bool = Field(
        default=True,
        description="Enable circuit breaker protection",
    )
    failure_threshold: MediumCount = Field(
        default=5,
        description="Number of failures before opening circuit",
    )
    recovery_timeout: IntervalDuration = Field(
        default=60,
        description="Seconds to wait before attempting recovery",
    )
    success_threshold: MediumCount = Field(
        default=2,
        description="Successes required to close circuit",
    )
    half_open_max_calls: MediumCount = Field(
        default=3,
        description="Max trial calls admitted while probing recovery in half-open state",
    )
    half_open_stuck_timeout_seconds: IntervalDuration = Field(
        default=60,
        description=(
            "Seconds after which a HALF_OPEN window with count==limit is "
            "considered stuck (worker died mid-trial) and auto-reset on the "
            "next try_acquire_half_open_slot call (476 D8)."
        ),
    )

    # ==========================================================================
    # Failure-Rate Trigger
    # ==========================================================================
    failure_rate_threshold: Percentage = Field(
        default=50.0,
        description=(
            "Failure percentage over the recent-call window that opens the "
            "circuit, OR'd with failure_threshold. Set to 0 to disable the "
            "rate trigger and leave only the consecutive-failure count trigger."
        ),
    )
    sliding_window_size: LargeCount = Field(
        default=100,
        description=(
            "How many recent CLOSED-state calls the failure rate is computed "
            "over. Counted per worker process, matching the circuit breaker's "
            "per-worker admission model."
        ),
    )
    minimum_calls: LargeCount = Field(
        default=10,
        description=(
            "Calls the window must hold before the failure-rate trigger is "
            "evaluated — a rate over too few calls is noise. Gates the rate "
            "trigger only: the consecutive-failure count trigger is "
            "traffic-independent and always applies. Setting this above "
            "sliding_window_size makes the rate trigger unreachable, because "
            "the window never holds that many calls. Distinct from "
            "rate_limit_cascade_minimum_calls, which governs 429 cascade "
            "detection rather than the failure-rate trigger."
        ),
    )

    # ==========================================================================
    # Rate Limit Cascade Detection (from core/config.py lines 25-27)
    # ==========================================================================
    rate_limit_cascade_threshold: LargeCount = Field(
        default=10,
        description=(
            "429 errors before cascade detection triggers. Counted per "
            "process unless rate_limit_distributed=True (shared L2 view)."
        ),
    )
    rate_limit_cascade_window_seconds: IntervalDuration = Field(
        default=60,
        description="Window for cascade detection",
    )
    rate_limit_cascade_rate: Percentage = Field(
        default=10.0,
        description="429 rate (%) to trigger cascade — hybrid condition with threshold",
    )
    rate_limit_cascade_minimum_calls: MediumCount = Field(
        default=20,
        description="Minimum requests before rate evaluation is meaningful",
    )

    # ==========================================================================
    # Self-DDoS Protection (from core/config.py lines 29-33)
    # Validation rules from core/safe_defaults.py lines 233-236
    # ==========================================================================
    self_ddos_protection_enabled: bool = Field(
        default=True,
        description="Enable self-DDoS protection",
    )
    self_ddos_rps_limit: HugeCount = Field(
        default=200,
        description=(
            "Per-service RPS cap for self-DDoS detection. Evaluated per "
            "process unless rate_limit_distributed=True; under N workers the "
            "aggregate downstream RPS can reach this value x N before every "
            "worker trips."
        ),
    )
    self_ddos_window_seconds: int = Field(
        default=10,
        ge=1,
        le=300,
        description="Window for self-DDoS detection",
    )
    self_ddos_backoff_multiplier: BackoffMultiplier = Field(
        default=STANDARD_BACKOFF_MULTIPLIER,
        description="Backoff multiplier for self-DDoS",
    )
    self_ddos_backoff_base_seconds: ShortDuration = Field(
        default=1.0,
        description="Base delay (seconds) for self-DDoS adaptive backoff",
    )
    self_ddos_backoff_max_seconds: LongDuration = Field(
        default=60.0,
        description="Maximum delay cap (seconds) for self-DDoS adaptive backoff",
    )
    self_ddos_backoff_jitter_factor: JitterFactor = Field(
        default=0.25,
        description="Jitter factor (0-1) for self-DDoS adaptive backoff randomization",
    )

    # ==========================================================================
    # Distributed Rate Limit Tracking (Redis L2)
    # ==========================================================================
    rate_limit_distributed: bool = Field(
        default=False,
        description="Enable Redis-backed distributed rate limit tracking",
    )

    # ==========================================================================
    # Cluster-wide CB state propagation (PRO-tier, default-off)
    # ==========================================================================
    cluster_state_propagation_enabled: bool = Field(
        default=False,
        description=(
            "Propagate a Circuit Breaker OPEN/CLOSED transition to already-"
            "running peer workers via the EventBus so the cluster protects a "
            "failing dependency without each worker re-tripping independently. "
            "Named for *state* (both OPEN and CLOSED) propagation. Requires the "
            "distributed EventBus (BALDUR_EVENT_BUS_BACKEND=redis); the active "
            "peer-side listener is PRO-tier. The OSS consumer is the cold-start "
            "L1-miss L2 read in the layered repository admission path, which "
            "also closes the #478 hydration-failure staleness window. "
            "Default-off keeps the admission read path L1-only (no Redis I/O)."
        ),
    )

    # ==========================================================================
    # Reconciler Settings (368: Django Settings Decoupling)
    # ==========================================================================
    monitored_services: list[str] = Field(
        default_factory=list,
        description="Service names to monitor for CB reconciliation",
    )

    @field_validator("failure_threshold")
    @classmethod
    def _warn_failure_threshold(cls, v: int) -> int:
        """Safe default fallback warning for extreme values."""
        return warn_above(50, "safe_default.high_consider_using_safety")(v)

    @model_validator(mode="after")
    def _warn_rate_trigger_unreachable(self) -> "CircuitBreakerSettings":
        """Warn when minimum_calls puts the rate trigger out of reach.

        The window never holds more than sliding_window_size calls, so a larger
        minimum_calls means the rate condition is never evaluated. This warns
        rather than raising: the combination is inert, not invalid — it disables
        one trigger while the consecutive-failure trigger keeps protecting — so
        raising would turn a suboptimal setting into a boot failure.
        """
        if self.minimum_calls > self.sliding_window_size:
            logger.warning(
                "settings.cb_rate_trigger_unreachable",
                minimum_calls=self.minimum_calls,
                sliding_window_size=self.sliding_window_size,
            )
        return self


# =============================================================================
# Singleton Pattern (cached settings)
# =============================================================================


def get_circuit_breaker_settings() -> "CircuitBreakerSettings":
    from baldur.settings.root import get_config

    return get_config().core.circuit_breaker


def reset_circuit_breaker_settings() -> None:
    from baldur.settings.root import get_config

    try:
        del get_config().core.__dict__["circuit_breaker"]
    except KeyError:
        pass
