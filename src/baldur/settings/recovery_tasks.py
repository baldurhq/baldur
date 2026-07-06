"""
Recovery Tasks Settings - Pydantic v2.

Celery 복구 태스크별 재시도 전략 설정입니다.

각 복구 태스크(check_recovery_trigger, execute_recovery_step,
monitor_active_recovery, cleanup_stale_sessions, run_health_checks)의
max_retries, default_retry_delay를 개별적으로 설정할 수 있습니다.

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
"""

import structlog
from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings

from baldur.settings.base import make_settings_config
from baldur.settings.validators import warn_below

logger = structlog.get_logger()


class RecoveryTasksSettings(BaseSettings):
    """
    복구 태스크별 Celery 재시도 설정.

    각 태스크마다 독립적인 max_retries, default_retry_delay 설정을 지원합니다.
    Celery 데코레이터에서 동적으로 적용하거나, 런타임 self.retry() 호출 시 사용합니다.

    태스크 목록:
    - check_recovery_trigger: 복구 트리거 조건 확인
    - execute_recovery_step: 복구 단계 실행
    - monitor_active_recovery: 활성 복구 세션 모니터링
    - cleanup_stale_sessions: 방치된 복구 세션 정리
    - run_health_checks: 헬스 체크 실행
    """

    model_config = make_settings_config("BALDUR_RECOVERY_TASKS_")

    # ==========================================================================
    # check_recovery_trigger 태스크 설정
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
    # execute_recovery_step 태스크 설정
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
    # monitor_active_recovery 태스크 설정
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
    # cleanup_stale_sessions 태스크 설정
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
    # run_health_checks 태스크 설정
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
    # 태스크 실행 간격 설정 (CeleryTaskSettings와 중복이나 복구 전용으로 분리)
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

    @field_validator("check_trigger_max_retries", "execute_step_max_retries")
    @classmethod
    def _warn_critical_task_retries(cls, v: int) -> int:
        """중요 태스크 재시도 최소 1회 보장."""
        return warn_below(1, "recovery_tasks_settings.critical_task_low_consider")(v)

    @model_validator(mode="after")
    def validate_retry_delays(self) -> "RecoveryTasksSettings":
        """재시도 지연이 너무 짧으면 경고."""
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
        return self


# =============================================================================
# Singleton Pattern
# =============================================================================


def get_recovery_tasks_settings() -> "RecoveryTasksSettings":
    """캐시된 RecoveryTasksSettings 인스턴스 반환."""
    from baldur.settings.root import get_config

    return get_config().services_group.recovery_tasks


def reset_recovery_tasks_settings() -> None:
    """캐시 초기화 (테스트용)."""
    from baldur.settings.root import get_config

    try:
        del get_config().services_group.__dict__["recovery_tasks"]
    except KeyError:
        pass
