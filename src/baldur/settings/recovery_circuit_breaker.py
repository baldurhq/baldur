"""
Recovery Circuit Breaker Settings - Pydantic v2.

Single Source of Truth for recovery circuit breaker configuration.

Replaces:
- services/coordination/recovery_circuit_breaker.py:RecoveryCircuitBreakerConfig

Environment Variables:
    BALDUR_RECOVERY_CB_ERROR_RATE_THRESHOLD=0.15
    BALDUR_RECOVERY_CB_SAMPLING_WINDOW_SECONDS=60
    BALDUR_RECOVERY_CB_MIN_SAMPLES=10
    ... etc

Reference:
- docs/baldur/middleware_system/92_CONFIG_IMPLEMENTATION_GUIDE.md
- docs/baldur/middleware_system/77_RECOVERY_COORDINATOR.md#8.1
"""

import structlog
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings

from baldur.settings.base import make_settings_config
from baldur.settings.field_types import (
    MediumCount,
    StrictProbability,
    TinyCount,
)
from baldur.settings.validators import warn_above

logger = structlog.get_logger()


class RecoveryCircuitBreakerSettings(BaseSettings):
    """
    Recovery Circuit Breaker configuration with validation.

    복구 진행 중 지표가 다시 악화되면 즉시 복구를 중단하고
    Emergency 상태로 재-에스컬레이션합니다.

    All defaults match:
    - services/coordination/recovery_circuit_breaker.py:RecoveryCircuitBreakerConfig
    """

    model_config = make_settings_config("BALDUR_RECOVERY_CB_")

    # ==========================================================================
    # Error Rate Settings
    # ==========================================================================
    error_rate_threshold: StrictProbability = Field(
        default=0.15,
        description="Error rate threshold (trips when exceeded). 15% = 0.15",
    )

    # ==========================================================================
    # Sampling Settings
    # ==========================================================================
    sampling_window_seconds: int = Field(
        default=60,
        ge=10,
        le=600,
        description="Sampling window (seconds). Evaluates based on the last N seconds of data",
    )
    min_samples: int = Field(
        default=10,
        ge=1,
        le=1000,
        description="Minimum sample count. Evaluation begins after collecting at least this many samples",
    )

    # ==========================================================================
    # Circuit State Settings
    # ==========================================================================
    open_duration_seconds: int = Field(
        default=300,
        ge=30,
        le=3600,
        description="Open state duration (seconds). Remains open for N seconds after tripping",
    )
    half_open_max_requests: MediumCount = Field(
        default=5,
        description="Maximum allowed requests in half-open state",
    )
    max_consecutive_trips: TinyCount = Field(
        default=3,
        description="Consecutive trip count. Permanently halts recovery when exceeded",
    )

    # ==========================================================================
    # Re-escalation Settings
    # ==========================================================================
    re_escalation_enabled: bool = Field(
        default=True,
        description="Enable re-escalation to Emergency level on trip",
    )
    re_escalation_level: str = Field(
        default="LEVEL_3",
        description="Target level for re-escalation",
    )

    @field_validator("error_rate_threshold")
    @classmethod
    def _warn_error_rate_threshold(cls, v: float) -> float:
        """Validate error rate threshold is reasonable."""
        return warn_above(0.5, "recovery_cb.error_rate_threshold_high")(v)

    @field_validator("re_escalation_level")
    @classmethod
    def validate_re_escalation_level(cls, v: str) -> str:
        """Validate re-escalation level is valid."""
        valid_levels = {"NORMAL", "LEVEL_1", "LEVEL_2", "LEVEL_3"}
        if v not in valid_levels:
            logger.warning(
                "unknown.valid_levels",
                setting_value=v,
                valid_levels=valid_levels,
            )
        return v


# =============================================================================
# Singleton Pattern (cached settings)
# =============================================================================


def get_recovery_circuit_breaker_settings() -> "RecoveryCircuitBreakerSettings":
    """
    Get cached RecoveryCircuitBreakerSettings instance.

    Returns:
        RecoveryCircuitBreakerSettings: Singleton instance
    """
    from baldur.settings.root import get_config

    return get_config().services_group.recovery_circuit_breaker


def reset_recovery_circuit_breaker_settings() -> None:
    """
    Reset cached settings (for testing).

    Call this after modifying environment variables to reload settings.
    """
    from baldur.settings.root import get_config

    try:
        del get_config().services_group.__dict__["recovery_circuit_breaker"]
    except KeyError:
        pass
