"""
AutoRollbackGuard Settings - Pydantic v2.

Independent safety net for when autonomous tuning fails.
Error rate / latency thresholds and consecutive failure counts are configurable
via environment variables.

Environment Variables:
    BALDUR_AUTO_ROLLBACK_ERROR_RATE_MAJOR=0.1
    BALDUR_AUTO_ROLLBACK_ERROR_RATE_CRITICAL=0.3
    BALDUR_AUTO_ROLLBACK_LATENCY_MAJOR_MS=5000
    BALDUR_AUTO_ROLLBACK_LATENCY_CRITICAL_MS=10000
    BALDUR_AUTO_ROLLBACK_MAX_HEALTH_HISTORY=10000
    BALDUR_AUTO_ROLLBACK_FAILURES_ALERT=3
    BALDUR_AUTO_ROLLBACK_FAILURES_EMERGENCY=5
"""

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings

from baldur.settings.base import make_settings_config
from baldur.settings.field_types import SmallCount


class AutoRollbackSettings(BaseSettings):
    """
    AutoRollbackGuard settings.

    Thresholds for classifying system degradation level and triggering
    emergency recovery.
    """

    model_config = make_settings_config("BALDUR_AUTO_ROLLBACK_")

    # ==========================================================================
    # Error rate thresholds (0.0 ~ 1.0)
    # ==========================================================================
    error_rate_major: float = Field(
        default=0.1,
        ge=0.01,
        le=0.5,
        description="Major-level error rate threshold. Rollback consideration starts above this.",
    )
    error_rate_critical: float = Field(
        default=0.3,
        ge=0.1,
        le=0.9,
        description="Critical-level error rate threshold. Immediate rollback above this.",
    )

    # ==========================================================================
    # Latency thresholds (milliseconds)
    # ==========================================================================
    latency_major_ms: int = Field(
        default=5000,
        ge=500,
        le=30000,
        description="Major-level P99 latency threshold (ms). Default 5 seconds.",
    )
    latency_critical_ms: int = Field(
        default=10000,
        ge=1000,
        le=60000,
        description="Critical-level P99 latency threshold (ms). Default 10 seconds.",
    )

    # ==========================================================================
    # Health check history size (Phase 2: predictive anomaly forecaster)
    # ==========================================================================
    max_health_history: int = Field(
        default=10000,
        ge=100,
        le=10000,
        description="Maximum health check history count. Covers ~83 hours at 30s intervals.",
    )

    # ==========================================================================
    # Consecutive failure thresholds
    # ==========================================================================
    failures_alert: SmallCount = Field(
        default=3,
        description="Consecutive failures to trigger alert. Enters ALERT state.",
    )
    failures_emergency: int = Field(
        default=5,
        ge=2,
        le=30,
        description="Consecutive failures to enter emergency. Enters EMERGENCY state.",
    )

    # ==========================================================================
    # Minor thresholds + cooldown (338: Settings Gap Phase 2)
    # ==========================================================================
    error_rate_minor: float = Field(
        default=0.05,
        ge=0.001,
        le=0.3,
        description="Minor degradation error rate threshold (5% default).",
    )
    latency_minor_ms: int = Field(
        default=3000,
        ge=100,
        le=30000,
        description="Minor degradation P99 latency threshold in ms (3s default).",
    )
    cooldown_minutes: int = Field(
        default=5,
        ge=1,
        le=60,
        description="Rollback cooldown period in minutes (5min default).",
    )

    @model_validator(mode="after")
    def validate_thresholds(self) -> "AutoRollbackSettings":
        """Validate threshold ordering: major < critical."""
        if self.error_rate_major >= self.error_rate_critical:
            raise ValueError(
                f"error_rate_major ({self.error_rate_major}) must be less than "
                f"error_rate_critical ({self.error_rate_critical})"
            )
        if self.latency_major_ms >= self.latency_critical_ms:
            raise ValueError(
                f"latency_major_ms ({self.latency_major_ms}) must be less than "
                f"latency_critical_ms ({self.latency_critical_ms})"
            )
        if self.failures_alert >= self.failures_emergency:
            raise ValueError(
                f"failures_alert ({self.failures_alert}) must be less than "
                f"failures_emergency ({self.failures_emergency})"
            )
        return self


# =============================================================================
# Singleton Pattern
# =============================================================================


def get_auto_rollback_settings() -> "AutoRollbackSettings":
    """
    Return the cached AutoRollbackSettings instance.

    Returns:
        AutoRollbackSettings: singleton instance
    """
    from baldur.settings.root import get_config

    return get_config().services_group.auto_rollback


def reset_auto_rollback_settings() -> None:
    """
    Reset cached settings (for testing).
    """
    from baldur.settings.root import get_config

    try:
        del get_config().services_group.__dict__["auto_rollback"]
    except KeyError:
        pass
