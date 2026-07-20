"""
SafetyBounds Settings - Pydantic v2.

Safety limits for autonomous tuning.
Per-parameter min/max ranges and the maximum change ratio per cycle are
configurable through environment variables.

Relationship with Field constraints (field_types.py):
    - Field constraints (ge/le via Annotated types) define the **user-configurable range**
      for each settings field. These are static validation boundaries that Pydantic
      enforces at settings load time.
    - SafetyBounds define the **auto-tuning range** — the narrower bounds within which
      the baldur engine may autonomously adjust parameters at runtime.
    - SafetyBounds are always a subset of Field constraints. If a Field allows ge=1, le=100,
      SafetyBounds might restrict auto-tuning to min=5, max=50.
    - Example: retry_count Field allows 0-20 (SmallCount), but SafetyBounds restricts
      auto-tuning to 0-10 with max 50% change per cycle.

Environment Variables (per parameter):
    BALDUR_SAFETY_BOUNDS_TIMEOUT_MS_MIN=100
    BALDUR_SAFETY_BOUNDS_TIMEOUT_MS_MAX=30000
    BALDUR_SAFETY_BOUNDS_TIMEOUT_MS_MAX_CHANGE=0.3
    ... (other parameters follow the same pattern)
"""

from pydantic import BaseModel, Field, model_validator
from pydantic_settings import BaseSettings

from baldur.settings.base import make_settings_config


class ParameterBoundConfig(BaseModel):
    """Bound configuration for a single parameter."""

    min_value: float = Field(description="Minimum allowed value")
    max_value: float = Field(description="Maximum allowed value")
    max_change_per_cycle: float = Field(
        ge=0.01,
        le=1.0,
        description="Maximum change ratio per cycle (0.3 = 30%)",
    )

    @model_validator(mode="after")
    def validate_bounds(self) -> "ParameterBoundConfig":
        """Validate that min < max."""
        if self.min_value > self.max_value:
            raise ValueError(
                f"min_value ({self.min_value}) cannot be greater than "
                f"max_value ({self.max_value})"
            )
        return self


class SafetyBoundsSettings(BaseSettings):
    """
    Full SafetyBounds configuration.

    Limits that keep autonomous tuning from drifting into a dangerous range.
    """

    model_config = make_settings_config("BALDUR_SAFETY_BOUNDS_")

    # ==========================================================================
    # timeout_ms bounds
    # ==========================================================================
    timeout_ms_min: float = Field(
        default=100,
        ge=10,
        description="Timeout minimum value (ms)",
    )
    timeout_ms_max: float = Field(
        default=30000,
        le=120000,
        description="Timeout maximum value (ms)",
    )
    timeout_ms_max_change: float = Field(
        default=0.3,
        ge=0.01,
        le=1.0,
        description="Timeout maximum change ratio per cycle",
    )

    # ==========================================================================
    # retry_count bounds
    # ==========================================================================
    retry_count_min: float = Field(
        default=0,
        ge=0,
        description="Retry count minimum value",
    )
    retry_count_max: float = Field(
        default=10,
        le=20,
        description="Retry count maximum value",
    )
    retry_count_max_change: float = Field(
        default=0.5,
        ge=0.01,
        le=1.0,
        description="Retry count maximum change ratio per cycle",
    )

    # ==========================================================================
    # circuit_breaker_threshold bounds
    # ==========================================================================
    circuit_breaker_threshold_min: float = Field(
        default=0.1,
        ge=0.01,
        le=0.5,
        description="Circuit breaker threshold minimum value",
    )
    circuit_breaker_threshold_max: float = Field(
        default=0.9,
        ge=0.5,
        le=0.99,
        description="Circuit breaker threshold maximum value",
    )
    circuit_breaker_threshold_max_change: float = Field(
        default=0.2,
        ge=0.01,
        le=1.0,
        description="Circuit breaker threshold maximum change ratio per cycle",
    )

    # ==========================================================================
    # jitter_range bounds
    # ==========================================================================
    jitter_range_min: float = Field(
        default=0.01,
        ge=0.001,
        description="Jitter range minimum value (seconds)",
    )
    jitter_range_max: float = Field(
        default=1.0,
        le=5.0,
        description="Jitter range maximum value (seconds)",
    )
    jitter_range_max_change: float = Field(
        default=0.5,
        ge=0.01,
        le=1.0,
        description="Jitter range maximum change ratio per cycle",
    )

    # ==========================================================================
    # rate_limit_rps bounds
    # ==========================================================================
    rate_limit_rps_min: float = Field(
        default=10,
        ge=1,
        description="Rate limit minimum value (rps)",
    )
    rate_limit_rps_max: float = Field(
        default=10000,
        le=100000,
        description="Rate limit maximum value (rps)",
    )
    rate_limit_rps_max_change: float = Field(
        default=0.2,
        ge=0.01,
        le=1.0,
        description="Rate limit maximum change ratio per cycle",
    )

    # ==========================================================================
    # throttle_sla_warning_ms bounds
    # ==========================================================================
    throttle_sla_warning_ms_min: float = Field(
        default=50,
        ge=10,
        description="SLA warning threshold minimum value (ms)",
    )
    throttle_sla_warning_ms_max: float = Field(
        default=2000,
        le=5000,
        description="SLA warning threshold maximum value (ms)",
    )
    throttle_sla_warning_ms_max_change: float = Field(
        default=0.3,
        ge=0.01,
        le=1.0,
        description="SLA warning maximum change ratio per cycle",
    )

    # ==========================================================================
    # throttle_sla_critical_ms bounds
    # ==========================================================================
    throttle_sla_critical_ms_min: float = Field(
        default=100,
        ge=50,
        description="SLA critical threshold minimum value (ms)",
    )
    throttle_sla_critical_ms_max: float = Field(
        default=5000,
        le=10000,
        description="SLA critical threshold maximum value (ms)",
    )
    throttle_sla_critical_ms_max_change: float = Field(
        default=0.3,
        ge=0.01,
        le=1.0,
        description="SLA critical maximum change ratio per cycle",
    )

    # ==========================================================================
    # backoff_base_ms bounds
    # ==========================================================================
    backoff_base_ms_min: float = Field(
        default=10,
        ge=1,
        description="Backoff base minimum value (ms)",
    )
    backoff_base_ms_max: float = Field(
        default=5000,
        le=30000,
        description="Backoff base maximum value (ms)",
    )
    backoff_base_ms_max_change: float = Field(
        default=0.3,
        ge=0.01,
        le=1.0,
        description="Backoff base maximum change ratio per cycle",
    )

    # ==========================================================================
    # backoff_max_ms bounds
    # ==========================================================================
    backoff_max_ms_min: float = Field(
        default=1000,
        ge=100,
        description="Backoff max minimum value (ms)",
    )
    backoff_max_ms_max: float = Field(
        default=60000,
        le=300000,
        description="Backoff max maximum value (ms)",
    )
    backoff_max_ms_max_change: float = Field(
        default=0.3,
        ge=0.01,
        le=1.0,
        description="Backoff max maximum change ratio per cycle",
    )

    # ==========================================================================
    # connection_pool_size bounds
    # ==========================================================================
    connection_pool_size_min: float = Field(
        default=1,
        ge=1,
        description="Connection pool size minimum value",
    )
    connection_pool_size_max: float = Field(
        default=100,
        le=500,
        description="Connection pool size maximum value",
    )
    connection_pool_size_max_change: float = Field(
        default=0.2,
        ge=0.01,
        le=1.0,
        description="Connection pool size maximum change ratio per cycle",
    )

    def get_bounds(self, parameter: str) -> ParameterBoundConfig | None:
        """
        Look up the bound configuration by parameter name.

        Args:
            parameter: Parameter name (e.g., "timeout_ms", "retry_count")

        Returns:
            ParameterBoundConfig, or None for an unknown parameter
        """
        # Normalize the parameter name (hyphen → underscore)
        normalized = parameter.replace("-", "_")

        min_attr = f"{normalized}_min"
        max_attr = f"{normalized}_max"
        change_attr = f"{normalized}_max_change"

        if not hasattr(self, min_attr):
            return None

        return ParameterBoundConfig(
            min_value=getattr(self, min_attr),
            max_value=getattr(self, max_attr),
            max_change_per_cycle=getattr(self, change_attr),
        )


def get_safety_bounds_settings() -> "SafetyBoundsSettings":
    from baldur.settings.root import get_config

    return get_config().meta.safety_bounds


def reset_safety_bounds_settings() -> None:
    from baldur.settings.root import get_config

    try:
        del get_config().meta.__dict__["safety_bounds"]
    except KeyError:
        pass
