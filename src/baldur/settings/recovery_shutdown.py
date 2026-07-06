"""
Recovery Shutdown Settings - Pydantic v2.

Single Source of Truth for recovery-aware shutdown configuration.

Replaces:
- services/coordination/recovery_shutdown.py:RecoveryAwareShutdownConfig

Environment Variables:
    BALDUR_RECOVERY_SHUTDOWN_DRAIN_TIMEOUT=30.0
    BALDUR_RECOVERY_SHUTDOWN_RECOVERY_EXTENSION=300.0
    BALDUR_RECOVERY_SHUTDOWN_MAX_WAIT=600.0
    ... etc

Reference:
- docs/baldur/middleware_system/92_CONFIG_IMPLEMENTATION_GUIDE.md
- docs/baldur/middleware_system/77_RECOVERY_COORDINATOR.md#11.3
"""

from typing import Self

import structlog
from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings

from baldur.settings.base import make_settings_config
from baldur.settings.field_types import STANDARD_CHECK_INTERVAL

logger = structlog.get_logger()


class RecoveryShutdownSettings(BaseSettings):
    """
    Recovery-aware Shutdown configuration with validation.

    K8s preStop 훅에서 현재 진행 중인 Recovery Session이 있는지 확인하고,
    있다면 종료를 지연시켜 복구 프로세스를 물리적으로 보호합니다.

    All defaults match:
    - services/coordination/recovery_shutdown.py:RecoveryAwareShutdownConfig
    """

    model_config = make_settings_config("BALDUR_RECOVERY_SHUTDOWN_")

    # ==========================================================================
    # Timeout Settings
    # ==========================================================================
    default_drain_timeout_seconds: float = Field(
        default=30.0,
        ge=5.0,
        le=300.0,
        description="Default drain timeout (seconds)",
    )
    recovery_extension_seconds: float = Field(
        default=300.0,
        ge=60.0,
        le=1800.0,
        description="Additional wait time when a Recovery Session is in progress (seconds)",
    )
    max_shutdown_wait_seconds: float = Field(
        default=600.0,
        ge=60.0,
        le=1800.0,
        description="Maximum wait time (seconds). Should match K8s terminationGracePeriodSeconds",
    )

    # ==========================================================================
    # preStop Settings
    # ==========================================================================
    prestop_seconds: float = Field(
        default=10.0,
        ge=0.0,
        le=60.0,
        description="K8s preStop hook sleep duration (seconds). "
        "Used to validate drain_timeout fits within K8s termination window.",
    )

    # ==========================================================================
    # Check Interval Settings
    # ==========================================================================
    recovery_check_interval_seconds: float = Field(
        default=STANDARD_CHECK_INTERVAL,
        ge=1.0,
        le=30.0,
        description="Recovery Session check interval (seconds)",
    )
    log_interval_seconds: float = Field(
        default=15.0,
        ge=5.0,
        le=60.0,
        description="Log output interval (seconds)",
    )

    # ==========================================================================
    # Behavior Settings
    # ==========================================================================
    allow_force_shutdown: bool = Field(
        default=True,
        description="Whether to allow force shutdown when maximum wait time is exceeded",
    )

    # ==========================================================================
    # GracefulShutdownCoordinator Settings (339)
    # ==========================================================================
    check_interval_seconds: float = Field(
        default=0.5,
        ge=0.1,
        le=10.0,
        description="Drain loop check interval for GracefulShutdownCoordinator (seconds).",
    )
    max_request_age_seconds: float = Field(
        default=300.0,
        ge=30.0,
        le=3600.0,
        description="Maximum age for tracked in-flight requests before cleanup (seconds).",
    )

    # ==========================================================================
    # DrainAware / RequestTracking middleware (471)
    # ==========================================================================
    drain_liveness_paths: list[str] = Field(
        default_factory=list,
        description=(
            "Operator-defined paths exempted from the drain-503 response "
            "while the coordinator is in DRAINING phase. Unioned with the "
            "baldur-canonical defaults (/api/baldur/health/live/, "
            "/api/baldur/health/ping/). Use this when k8s livenessProbe "
            "targets a non-baldur path such as /livez or /healthz/live."
        ),
    )
    drain_default_retry_after_seconds: float = Field(
        default=5.0,
        ge=1.0,
        le=300.0,
        description=(
            "Fallback Retry-After value (seconds) for drain-503 responses "
            "when the coordinator phase is not DRAINING (e.g. TERMINATING) "
            "and remaining_drain_time is therefore unavailable."
        ),
    )
    hooks_check_delay_seconds: float = Field(
        default=2.0,
        ge=0.5,
        le=30.0,
        description=(
            "Delay (seconds) before the deferred check that emits "
            "'baldur.gunicorn_hooks_not_installed' WARNING when running "
            "under gunicorn but baldur.adapters.gunicorn.hooks was not "
            "imported. Window must outlast post_worker_init's hooks-side "
            "import path."
        ),
    )

    @field_validator("max_shutdown_wait_seconds")
    @classmethod
    def validate_max_wait_ge_drain(cls, v: float, info) -> float:
        """Ensure max_shutdown_wait >= drain_timeout + extension."""
        drain = info.data.get("default_drain_timeout_seconds", 30.0)
        extension = info.data.get("recovery_extension_seconds", 300.0)
        min_required = drain + extension

        if v < min_required:
            logger.warning(
                "recovery.interrupted",
                setting_value=v,
                drain=drain,
                extension=extension,
                min_required=min_required,
            )
        return v

    @model_validator(mode="after")
    def validate_drain_fits_termination_window(self) -> Self:
        """Warn if drain_timeout + preStop exceeds max_shutdown_wait."""
        effective_window = self.max_shutdown_wait_seconds - self.prestop_seconds
        if self.default_drain_timeout_seconds > effective_window:
            logger.warning(
                "shutdown_coordinator.drain_timeout_exceeds_window",
                drain_timeout=self.default_drain_timeout_seconds,
                effective_window=effective_window,
                prestop=self.prestop_seconds,
                max_wait=self.max_shutdown_wait_seconds,
            )
        return self


# =============================================================================
# Singleton Pattern (cached settings)
# =============================================================================


def get_recovery_shutdown_settings() -> "RecoveryShutdownSettings":
    """
    Get cached RecoveryShutdownSettings instance.

    Returns:
        RecoveryShutdownSettings: Singleton instance
    """
    from baldur.settings.root import get_config

    return get_config().services_group.recovery_shutdown


def reset_recovery_shutdown_settings() -> None:
    """
    Reset cached settings (for testing).

    Call this after modifying environment variables to reload settings.
    """
    from baldur.settings.root import get_config

    try:
        del get_config().services_group.__dict__["recovery_shutdown"]
    except KeyError:
        pass
