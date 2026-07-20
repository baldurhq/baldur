"""
Recovery Coordinator Settings - Pydantic v2.

Default per-stage settings for the RecoveryCoordinator.

Step parameters for each recovery level (LEVEL_1, LEVEL_2, LEVEL_3):
- wait_after_seconds: Wait time after the step completes
- duration_minutes: Health check duration
- success_threshold: Success rate threshold
- error_rate_threshold: Error rate threshold

Stability check defaults are included as well.

Environment Variables:
    BALDUR_RECOVERY_COORDINATOR_LEVEL3_HEALTH_CHECK_DURATION_MINUTES=5
    BALDUR_RECOVERY_COORDINATOR_LEVEL3_HEALTH_CHECK_SUCCESS_THRESHOLD=0.95
    BALDUR_RECOVERY_COORDINATOR_LEVEL3_HEALTH_CHECK_ERROR_RATE_THRESHOLD=0.1
    BALDUR_RECOVERY_COORDINATOR_LEVEL3_CANARY_RESUME_WAIT_AFTER=60
    BALDUR_RECOVERY_COORDINATOR_LEVEL3_GOVERNANCE_NORMAL_WAIT_AFTER=300
    BALDUR_RECOVERY_COORDINATOR_STABILITY_CHECK_DURATION_MINUTES=10
    BALDUR_RECOVERY_COORDINATOR_STABILITY_CHECK_ERROR_RATE_THRESHOLD=0.1
"""

import structlog
from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings

from baldur.settings.base import make_settings_config
from baldur.settings.field_types import TinyCount
from baldur.settings.validators import warn_below

logger = structlog.get_logger()


class RecoveryCoordinatorSettings(BaseSettings):
    """
    RecoveryCoordinator recovery stage configuration.

    Defines the per-LEVEL RecoveryStep parameters and the stability check
    defaults. Makes the defaults in RecoveryCoordinator.DEFAULT_RECOVERY_STEPS
    overridable through environment variables.
    """

    model_config = make_settings_config("BALDUR_RECOVERY_COORDINATOR_")

    # ==========================================================================
    # LEVEL_3 (most severe level) recovery stage settings
    # ==========================================================================
    level3_budget_reset_wait_after: int = Field(
        default=0,
        ge=0,
        le=300,
        description="Wait time after LEVEL_3 BUDGET_RESET step completion (seconds)",
    )
    level3_health_check_wait_after: int = Field(
        default=0,
        ge=0,
        le=300,
        description="Wait time after LEVEL_3 HEALTH_CHECK step completion (seconds)",
    )
    level3_health_check_duration_minutes: int = Field(
        default=5,
        ge=1,
        le=30,
        description="LEVEL_3 HEALTH_CHECK duration (minutes)",
    )
    level3_health_check_success_threshold: float = Field(
        default=0.95,
        ge=0.8,
        le=1.0,
        description="LEVEL_3 HEALTH_CHECK success rate threshold",
    )
    level3_health_check_error_rate_threshold: float = Field(
        default=0.1,
        ge=0.01,
        le=0.3,
        description="LEVEL_3 HEALTH_CHECK error rate threshold",
    )
    level3_canary_resume_wait_after: int = Field(
        default=60,
        ge=0,
        le=600,
        description="Wait time after LEVEL_3 CANARY_RESUME step completion (seconds)",
    )
    level3_governance_normal_wait_after: int = Field(
        default=300,
        ge=0,
        le=900,
        description="Wait time after LEVEL_3 GOVERNANCE_NORMAL step completion (seconds, 5-min stabilization)",
    )

    # ==========================================================================
    # LEVEL_2 (intermediate level) recovery stage settings
    # ==========================================================================
    level2_budget_reset_wait_after: int = Field(
        default=0,
        ge=0,
        le=300,
        description="Wait time after LEVEL_2 BUDGET_RESET step completion (seconds)",
    )
    level2_health_check_wait_after: int = Field(
        default=0,
        ge=0,
        le=300,
        description="Wait time after LEVEL_2 HEALTH_CHECK step completion (seconds)",
    )
    level2_health_check_duration_minutes: int = Field(
        default=3,
        ge=1,
        le=30,
        description="LEVEL_2 HEALTH_CHECK duration (minutes)",
    )
    level2_health_check_success_threshold: float = Field(
        default=0.95,
        ge=0.8,
        le=1.0,
        description="LEVEL_2 HEALTH_CHECK success rate threshold",
    )
    level2_health_check_error_rate_threshold: float = Field(
        default=0.15,
        ge=0.01,
        le=0.3,
        description="LEVEL_2 HEALTH_CHECK error rate threshold",
    )
    level2_canary_resume_wait_after: int = Field(
        default=30,
        ge=0,
        le=600,
        description="Wait time after LEVEL_2 CANARY_RESUME step completion (seconds)",
    )

    # ==========================================================================
    # LEVEL_1 (mild level) recovery stage settings
    # ==========================================================================
    level1_budget_reset_wait_after: int = Field(
        default=0,
        ge=0,
        le=300,
        description="Wait time after LEVEL_1 BUDGET_RESET step completion (seconds)",
    )
    level1_health_check_wait_after: int = Field(
        default=0,
        ge=0,
        le=300,
        description="Wait time after LEVEL_1 HEALTH_CHECK step completion (seconds)",
    )
    level1_health_check_duration_minutes: int = Field(
        default=2,
        ge=1,
        le=30,
        description="LEVEL_1 HEALTH_CHECK duration (minutes)",
    )
    level1_health_check_success_threshold: float = Field(
        default=0.90,
        ge=0.8,
        le=1.0,
        description="LEVEL_1 HEALTH_CHECK success rate threshold",
    )
    level1_health_check_error_rate_threshold: float = Field(
        default=0.2,
        ge=0.01,
        le=0.5,
        description="LEVEL_1 HEALTH_CHECK error rate threshold",
    )

    # ==========================================================================
    # Stability check defaults (whole recovery session level)
    # ==========================================================================
    stability_check_duration_minutes: int = Field(
        default=10,
        ge=1,
        le=60,
        description="Default duration for stability verification (minutes)",
    )
    stability_check_error_rate_threshold: float = Field(
        default=0.1,
        ge=0.01,
        le=0.3,
        description="Stability check error rate threshold",
    )
    stability_check_success_rate_threshold: float = Field(
        default=0.95,
        ge=0.8,
        le=1.0,
        description="Stability check success rate threshold",
    )

    # ==========================================================================
    # Recovery session global settings
    # ==========================================================================
    max_recovery_session_duration_minutes: int = Field(
        default=120,
        ge=30,
        le=480,
        description="Maximum recovery session duration (minutes). Auto-aborts when exceeded",
    )
    step_execution_timeout_seconds: int = Field(
        default=300,
        ge=30,
        le=1800,
        description="Single recovery step execution timeout (seconds). Global default when step has no timeout_seconds",
    )

    # ==========================================================================
    # Per-step-type timeout settings
    # ==========================================================================
    budget_reset_timeout_seconds: int = Field(
        default=60,
        ge=10,
        le=600,
        description="BUDGET_RESET step timeout (seconds)",
    )
    health_check_timeout_seconds: int = Field(
        default=600,
        ge=30,
        le=3600,
        description="HEALTH_CHECK step timeout (seconds). Set longer for stabilization verification",
    )
    canary_resume_timeout_seconds: int = Field(
        default=300,
        ge=30,
        le=1800,
        description="CANARY_RESUME step timeout (seconds)",
    )
    governance_normal_timeout_seconds: int = Field(
        default=120,
        ge=10,
        le=600,
        description="GOVERNANCE_NORMAL step timeout (seconds)",
    )
    compensation_step_timeout_seconds: int = Field(
        default=120,
        ge=10,
        le=600,
        description="Individual compensation handler execution timeout (seconds). Recommended shorter than forward step",
    )

    max_resume_count: TinyCount = Field(
        default=3,
        description="Maximum resume count for failed recovery sessions. Requires manual intervention when exceeded",
    )

    @field_validator(
        "level3_health_check_success_threshold",
        "level2_health_check_success_threshold",
        "level1_health_check_success_threshold",
    )
    @classmethod
    def _warn_success_threshold(cls, v: float) -> float:
        """Warn when the success rate threshold is too low."""
        return warn_below(
            0.9, "recovery_coordinator_settings.success_threshold_low_consider"
        )(v)

    @model_validator(mode="after")
    def validate_level_consistency(self) -> "RecoveryCoordinatorSettings":
        """Validate per-level consistency (LEVEL_3 > LEVEL_2 > LEVEL_1)."""
        # LEVEL_3 must be the strictest
        if (
            self.level3_health_check_success_threshold
            < self.level2_health_check_success_threshold
        ):
            logger.warning(
                "recovery_coordinator.success_threshold_inverted",
                level3=self.level3_health_check_success_threshold,
                level2=self.level2_health_check_success_threshold,
            )
        if (
            self.level3_health_check_error_rate_threshold
            > self.level2_health_check_error_rate_threshold
        ):
            logger.warning(
                "recovery_coordinator.error_rate_threshold_inverted",
                level3=self.level3_health_check_error_rate_threshold,
                level2=self.level2_health_check_error_rate_threshold,
            )
        return self


# =============================================================================
# Singleton Pattern
# =============================================================================


def get_recovery_coordinator_settings() -> "RecoveryCoordinatorSettings":
    """Return the cached RecoveryCoordinatorSettings instance."""
    from baldur.settings.root import get_config

    return get_config().services_group.recovery_coordinator


def reset_recovery_coordinator_settings() -> None:
    """Reset the cache (for testing)."""
    from baldur.settings.root import get_config

    try:
        del get_config().services_group.__dict__["recovery_coordinator"]
    except KeyError:
        pass
