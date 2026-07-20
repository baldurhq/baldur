"""
Chaos Experiment Settings - Pydantic v2.

TTL, SLA, and result retention settings for chaos experiments.

Replaces:
- services/chaos/base/experiment.py:default_ttl_seconds
- services/chaos/base/models.py:grace_period_seconds
- services/chaos/base/models.py:sla_breach_threshold_percent

Environment Variables:
    BALDUR_CHAOS_EXPERIMENT_MAX_DURATION_SECONDS=3600
    BALDUR_CHAOS_EXPERIMENT_GRACE_PERIOD_SECONDS=300
    BALDUR_CHAOS_EXPERIMENT_RESULT_TTL_SECONDS=86400
"""

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings

from baldur.settings.base import make_settings_config
from baldur.settings.validators import warn_above


class ChaosExperimentSettings(BaseSettings):
    """
    Chaos experiment settings.

    Manages the lifecycle and SLA settings of a chaos engineering experiment.

    Features:
    - Maximum experiment duration limit
    - Grace period: stabilization wait after the experiment starts
    - SLA breach threshold: auto-stop trigger
    - Result retention period (TTL)
    """

    model_config = make_settings_config("BALDUR_CHAOS_EXPERIMENT_")

    # ==========================================================================
    # Duration Settings (from chaos/base/experiment.py)
    # ==========================================================================
    max_duration_seconds: int = Field(
        default=3600,
        ge=60,
        le=86400,
        description="Maximum experiment duration (seconds). Default 1 hour, max 24 hours",
    )

    default_duration_seconds: int = Field(
        default=300,
        ge=30,
        le=3600,
        description="Default experiment duration (seconds). Default 5 minutes",
    )

    default_ttl_seconds: int = Field(
        default=600,
        ge=60,
        le=7200,
        description="Experiment auto-expiry TTL (seconds). Default 10 minutes",
    )

    # ==========================================================================
    # Grace Period Settings (from chaos/base/models.py)
    # ==========================================================================
    grace_period_seconds: int = Field(
        default=300,
        ge=30,
        le=1800,
        description="Stabilization wait time after experiment start (seconds). Default 5 minutes",
    )

    # ==========================================================================
    # SLA Settings (from chaos/base/models.py)
    # ==========================================================================
    sla_breach_threshold_percent: float = Field(
        default=1.0,
        ge=0.1,
        le=10.0,
        description="SLA breach threshold (%). Auto-stop when exceeded",
    )

    # ==========================================================================
    # Result Storage Settings
    # ==========================================================================
    result_ttl_seconds: int = Field(
        default=86400,
        ge=3600,
        le=604800,
        description="Experiment result retention period (seconds). Default 24 hours, max 7 days",
    )

    # ==========================================================================
    # Health Check Settings (from chaos/base/models.py)
    # ==========================================================================
    health_check_interval_seconds: float = Field(
        default=30.0,
        ge=5.0,
        le=300.0,
        description="Health check interval during experiment (seconds)",
    )

    health_check_timeout_ms: int = Field(
        default=100,
        ge=10,
        le=5000,
        description="Health check timeout (milliseconds)",
    )

    @field_validator("grace_period_seconds")
    @classmethod
    def _warn_grace_period(cls, v: int, info) -> int:
        """Grace period must be smaller than max_duration."""
        # Note: cross-field validation is handled in model_validator
        return warn_above(1800, "chaos_experiment.grace_period_too_long")(v)


# =============================================================================
# Singleton Pattern
# =============================================================================


def get_chaos_experiment_settings() -> "ChaosExperimentSettings":
    """Get cached ChaosExperimentSettings instance."""
    from baldur.settings.root import get_config

    return get_config().services_group.chaos_experiment


def reset_chaos_experiment_settings() -> None:
    """Reset cached settings (for testing)."""
    from baldur.settings.root import get_config

    try:
        del get_config().services_group.__dict__["chaos_experiment"]
    except KeyError:
        pass
