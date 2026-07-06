"""
Chaos Settings - Pydantic v2.

Single Source of Truth for chaos engineering configuration.

Replaces:
- core/config.py:ChaosConfig (lines 575-605)
- core/safe_defaults.py:SAFE_DEFAULTS["chaos"]
- core/safe_defaults.py:VALIDATION_RULES["chaos"]

Environment Variables:
    BALDUR_CHAOS_MAX_BLAST_RADIUS=0.10
    BALDUR_CHAOS_DRY_RUN_DEFAULT=true

    # Chaos Scheduler Celery Task 재시도 설정
    BALDUR_CHAOS_SCHEDULER_EXPERIMENT_MAX_RETRIES=0
    BALDUR_CHAOS_SCHEDULER_EXPERIMENT_SOFT_TIME_LIMIT=300
    BALDUR_CHAOS_SCHEDULER_EXPERIMENT_TIME_LIMIT=360
    BALDUR_CHAOS_SCHEDULER_REPORT_MAX_RETRIES=3
    BALDUR_CHAOS_SCHEDULER_REPORT_RETRY_DELAY=300
    BALDUR_CHAOS_SCHEDULER_CLEANUP_MAX_RETRIES=1
    BALDUR_CHAOS_SCHEDULER_PENDING_CHECK_MAX_RETRIES=1
"""

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings

from baldur.settings.base import make_settings_config
from baldur.settings.validators import warn_above


class ChaosSettings(BaseSettings):
    """
    Chaos Engineering configuration with validation.

    Safety Guard, Blast Radius, and experiment controls.

    All defaults match core/config.py:ChaosConfig
    All validation rules match core/safe_defaults.py:VALIDATION_RULES["chaos"]
    """

    model_config = make_settings_config("BALDUR_CHAOS_")

    # ==========================================================================
    # Safety Guard (from core/config.py lines 582-586)
    # Validation rules from core/safe_defaults.py lines 301-304
    # ==========================================================================
    max_blast_radius: float = Field(
        default=0.10,
        ge=0.0,
        le=0.5,
        description="Maximum blast radius (10% default, 50% max)",
    )
    max_failure_rate: float = Field(
        default=0.20,
        ge=0.0,
        le=0.5,
        description="Maximum failure rate (20% default, 50% max)",
    )
    auto_rollback_enabled: bool = Field(
        default=True,
        description="Enable automatic rollback on threshold breach",
    )
    rollback_threshold: float = Field(
        default=0.05,
        ge=0.0,
        le=0.5,
        description="Error rate threshold for rollback (5%)",
    )

    # ==========================================================================
    # Experiment Controls (from core/config.py lines 588-592)
    # ==========================================================================
    dry_run_default: bool = Field(
        default=True,
        description="Default to dry run mode",
    )
    require_approval: bool = Field(
        default=False,
        description="Require approval before experiments",
    )
    experiment_timeout_seconds: int = Field(
        default=7200,
        ge=300,
        le=14400,
        description="Global zombie detection fallback threshold (seconds). "
        "Used by MetaWatchdog probe when experiment's own TTL is unavailable.",
    )

    # ==========================================================================
    # Stop Conditions (from core/config.py lines 594-596)
    # ==========================================================================
    stop_on_error_rate: float = Field(
        default=0.10,
        ge=0.0,
        le=0.5,
        description="Error rate threshold to stop experiment",
    )
    stop_on_latency_increase_pct: float = Field(
        default=50.0,
        ge=0.0,
        le=200.0,
        description="Latency increase percentage threshold to stop",
    )

    # ==========================================================================
    # Additional from safe_defaults
    # ==========================================================================
    enabled: bool = Field(
        default=False,
        description="Enable chaos engineering (default disabled for safety)",
    )
    failure_rate: float = Field(
        default=0.01,
        ge=0.0,
        le=0.5,
        description="Default failure injection rate (1%)",
    )
    latency_max_ms: int = Field(
        default=1000,
        ge=0,
        le=10000,
        description="Maximum latency injection in milliseconds",
    )

    # ==========================================================================
    # Chaos Scheduler Celery Task 재시도 설정 (run_scheduled_experiments_task)
    # Chaos 실험은 재시도하지 않음 (안전을 위해)
    # ==========================================================================
    scheduler_experiment_max_retries: int = Field(
        default=0,
        ge=0,
        le=3,
        description="Maximum retries for scheduled experiment task (0 recommended)",
    )
    scheduler_experiment_soft_time_limit: int = Field(
        default=300,
        ge=60,
        le=1800,
        description="Soft time limit for scheduled experiment task (seconds)",
    )
    scheduler_experiment_time_limit: int = Field(
        default=360,
        ge=120,
        le=2400,
        description="Hard time limit for scheduled experiment task (seconds)",
    )

    # ==========================================================================
    # Chaos Scheduler Celery Task 재시도 설정 (generate_daily_resilience_report_task)
    # ==========================================================================
    scheduler_report_max_retries: int = Field(
        default=3,
        ge=0,
        le=10,
        description="Maximum retries for daily resilience report task",
    )
    scheduler_report_retry_delay: int = Field(
        default=300,
        ge=30,
        le=1800,
        description="Retry delay for daily resilience report task (seconds)",
    )

    # ==========================================================================
    # Chaos Scheduler Celery Task 재시도 설정 (cleanup_expired_approvals_task)
    # ==========================================================================
    scheduler_cleanup_max_retries: int = Field(
        default=1,
        ge=0,
        le=5,
        description="Maximum retries for expired approval cleanup task",
    )

    # ==========================================================================
    # Chaos Scheduler Celery Task 재시도 설정 (check_pending_approvals_task)
    # ==========================================================================
    scheduler_pending_check_max_retries: int = Field(
        default=1,
        ge=0,
        le=5,
        description="Maximum retries for pending approval check task",
    )

    # ==========================================================================
    # Zombie Hunter Distributed Lock TTL (from chaos_scheduler.py line 441)
    # ==========================================================================
    experiment_lock_ttl: int = Field(
        default=120,
        ge=30,
        le=600,
        description="Distributed lock TTL for zombie experiment rollback (seconds). Prevents race conditions.",
    )

    # ==========================================================================
    # Cross-Process Zombie Detection (390)
    # ==========================================================================
    cross_process_detection_enabled: bool = Field(
        default=False,
        description=(
            "Enable cross-process zombie detection via StateBackend. "
            "Requires Redis StateBackend for cross-pod visibility."
        ),
    )
    worker_heartbeat_interval_seconds: int = Field(
        default=40,
        ge=10,
        le=300,
        description=(
            "Worker heartbeat write interval (seconds). "
            "Should be TTL / 3 ratio following RegionHeartbeat convention."
        ),
    )
    worker_heartbeat_ttl_seconds: int = Field(
        default=120,
        ge=30,
        le=600,
        description=(
            "Worker heartbeat TTL (seconds). "
            "Should be 2x zombie hunt interval for reliable liveness detection."
        ),
    )

    @field_validator("max_blast_radius", "max_failure_rate")
    @classmethod
    def _warn_safety_limits(cls, v: float) -> float:
        """Warn if safety limits are set high."""
        return warn_above(0.3, "safe_default.high_chaos_consider_using")(v)


# =============================================================================
# Singleton Pattern (cached settings)
# =============================================================================


def get_chaos_settings() -> "ChaosSettings":
    from baldur.settings.root import get_config

    return get_config().services_group.chaos


def reset_chaos_settings() -> None:
    from baldur.settings.root import get_config

    try:
        del get_config().services_group.__dict__["chaos"]
    except KeyError:
        pass
