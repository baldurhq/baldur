"""
Recovery Tasks Settings - Pydantic v2.

Per-recovery-task Celery retry strategy settings.

Lets each recovery task (check_recovery_trigger, execute_recovery_step,
monitor_active_recovery, cleanup_stale_sessions, run_health_checks) configure
its max_retries and default_retry_delay independently.

Environment Variables:
    BALDUR_RECOVERY_TASKS_CHECK_TRIGGER_MAX_RETRIES=3
    BALDUR_RECOVERY_TASKS_CHECK_TRIGGER_RETRY_DELAY=60
    BALDUR_RECOVERY_TASKS_EXECUTE_STEP_MAX_RETRIES=3
    BALDUR_RECOVERY_TASKS_EXECUTE_STEP_RETRY_DELAY=30
    BALDUR_RECOVERY_TASKS_MONITOR_RECOVERY_MAX_RETRIES=3
    BALDUR_RECOVERY_TASKS_MONITOR_RECOVERY_RETRY_DELAY=30
    BALDUR_RECOVERY_TASKS_CLEANUP_STALE_MAX_RETRIES=2
    BALDUR_RECOVERY_TASKS_CLEANUP_STALE_RETRY_DELAY=15
    BALDUR_RECOVERY_TASKS_HEALTH_CHECK_MAX_RETRIES=1
    BALDUR_RECOVERY_TASKS_HEALTH_CHECK_RETRY_DELAY=60
    BALDUR_RECOVERY_TASKS_RESUME_BACKOFF_BASE_SECONDS=30
    BALDUR_RECOVERY_TASKS_RESUME_BACKOFF_MAX_SECONDS=300
"""

import structlog
from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings

from baldur.settings.base import make_settings_config
from baldur.settings.validators import warn_below

logger = structlog.get_logger()


class RecoveryTasksSettings(BaseSettings):
    """
    Per-recovery-task Celery retry settings.

    Supports an independent max_retries / default_retry_delay per task, applied
    dynamically from the Celery decorator or at runtime on a ``self.retry()``
    call.

    Tasks:
    - check_recovery_trigger: check the recovery trigger conditions
    - execute_recovery_step: run a recovery step
    - monitor_active_recovery: monitor active recovery sessions
    - cleanup_stale_sessions: clean up abandoned recovery sessions
    - run_health_checks: run health checks
    """

    model_config = make_settings_config("BALDUR_RECOVERY_TASKS_")

    # ==========================================================================
    # check_recovery_trigger task settings
    # ==========================================================================
    check_trigger_max_retries: int = Field(
        default=3,
        ge=0,
        le=10,
        description="check_recovery_trigger maximum retry count",
    )
    check_trigger_retry_delay: int = Field(
        default=60,
        ge=5,
        le=600,
        description="check_recovery_trigger retry delay (seconds)",
    )

    # ==========================================================================
    # execute_recovery_step task settings
    # ==========================================================================
    execute_step_max_retries: int = Field(
        default=3,
        ge=0,
        le=10,
        description="execute_recovery_step maximum retry count",
    )
    execute_step_retry_delay: int = Field(
        default=30,
        ge=5,
        le=600,
        description="execute_recovery_step retry delay (seconds)",
    )

    # ==========================================================================
    # monitor_active_recovery task settings
    # ==========================================================================
    monitor_recovery_max_retries: int = Field(
        default=3,
        ge=0,
        le=10,
        description="monitor_active_recovery maximum retry count",
    )
    monitor_recovery_retry_delay: int = Field(
        default=30,
        ge=5,
        le=600,
        description="monitor_active_recovery retry delay (seconds)",
    )

    # ==========================================================================
    # cleanup_stale_sessions task settings
    # ==========================================================================
    cleanup_stale_max_retries: int = Field(
        default=2,
        ge=0,
        le=10,
        description="cleanup_stale_sessions maximum retry count",
    )
    cleanup_stale_retry_delay: int = Field(
        default=15,
        ge=5,
        le=300,
        description="cleanup_stale_sessions retry delay (seconds)",
    )

    # ==========================================================================
    # run_health_checks task settings
    # ==========================================================================
    health_check_max_retries: int = Field(
        default=1,
        ge=0,
        le=5,
        description="run_health_checks maximum retry count (requires fast feedback)",
    )
    health_check_retry_delay: int = Field(
        default=60,
        ge=10,
        le=300,
        description="run_health_checks retry delay (seconds)",
    )

    # ==========================================================================
    # Task execution interval settings (overlaps CeleryTaskSettings but kept
    # separate for the recovery domain)
    # ==========================================================================
    trigger_check_interval: int = Field(
        default=60,
        ge=10,
        le=300,
        description="Trigger check interval (seconds)",
    )
    health_monitor_interval: int = Field(
        default=30,
        ge=10,
        le=120,
        description="Health monitor interval (seconds)",
    )
    stale_check_interval: int = Field(
        default=10,
        ge=1,
        le=60,
        description="Stale session check interval (minutes)",
    )

    # ==========================================================================
    # Recovery-resume exponential backoff (execute_recovery_step resume path)
    # ==========================================================================
    resume_backoff_base_seconds: int = Field(
        default=30,
        ge=1,
        le=600,
        description="Recovery-resume exponential backoff base delay (seconds)",
    )
    resume_backoff_max_seconds: int = Field(
        default=300,
        ge=1,
        le=3600,
        description="Recovery-resume exponential backoff maximum delay cap (seconds)",
    )

    @field_validator("check_trigger_max_retries", "execute_step_max_retries")
    @classmethod
    def _warn_critical_task_retries(cls, v: int) -> int:
        """Ensure critical tasks retry at least once."""
        return warn_below(1, "recovery_tasks_settings.critical_task_low_consider")(v)

    @model_validator(mode="after")
    def validate_retry_delays(self) -> "RecoveryTasksSettings":
        """Warn on very short retry delays and enforce base <= max backoff."""
        delays = [
            ("check_trigger", self.check_trigger_retry_delay),
            ("execute_step", self.execute_step_retry_delay),
            ("monitor_recovery", self.monitor_recovery_retry_delay),
        ]
        for name, delay in delays:
            if delay < 10:
                logger.warning(
                    "recovery_tasks_settings.very_short",
                    task_name=name,
                    delay=delay,
                )
        if self.resume_backoff_base_seconds > self.resume_backoff_max_seconds:
            raise ValueError(
                "resume_backoff_base_seconds must not exceed resume_backoff_max_seconds"
            )
        return self


# =============================================================================
# Singleton Pattern
# =============================================================================


def get_recovery_tasks_settings() -> "RecoveryTasksSettings":
    """Return the cached RecoveryTasksSettings instance."""
    from baldur.settings.root import get_config

    return get_config().services_group.recovery_tasks


def reset_recovery_tasks_settings() -> None:
    """Reset the cache (for tests)."""
    from baldur.settings.root import get_config

    try:
        del get_config().services_group.__dict__["recovery_tasks"]
    except KeyError:
        pass
