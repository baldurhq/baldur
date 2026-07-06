"""
Recovery Coordinator Settings - Pydantic v2.

RecoveryCoordinator의 복구 단계별 기본 설정입니다.

각 복구 레벨(LEVEL_1, LEVEL_2, LEVEL_3)별 단계 파라미터:
- wait_after_seconds: 단계 완료 후 대기 시간
- duration_minutes: 헬스 체크 지속 시간
- success_threshold: 성공률 임계값
- error_rate_threshold: 에러율 임계값

또한 안정성 검사 기본값도 포함합니다.

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
    RecoveryCoordinator 복구 단계 설정.

    LEVEL별 RecoveryStep 파라미터와 안정성 검사 기본값을 정의합니다.
    RecoveryCoordinator.DEFAULT_RECOVERY_STEPS의 기본값을 환경변수로 오버라이드 가능하게 합니다.
    """

    model_config = make_settings_config("BALDUR_RECOVERY_COORDINATOR_")

    # ==========================================================================
    # LEVEL_3 (가장 심각한 레벨) 복구 단계 설정
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
    # LEVEL_2 (중간 레벨) 복구 단계 설정
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
    # LEVEL_1 (경미한 레벨) 복구 단계 설정
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
    # 안정성 검사 기본 설정 (전체 복구 세션 수준)
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
    # 복구 세션 전역 설정
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
    # Step 유형별 타임아웃 설정
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
        """성공률 임계값이 너무 낮으면 경고."""
        return warn_below(
            0.9, "recovery_coordinator_settings.success_threshold_low_consider"
        )(v)

    @model_validator(mode="after")
    def validate_level_consistency(self) -> "RecoveryCoordinatorSettings":
        """레벨별 설정 일관성 검증 (LEVEL_3 > LEVEL_2 > LEVEL_1)."""
        # LEVEL_3가 가장 엄격해야 함
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
    """캐시된 RecoveryCoordinatorSettings 인스턴스 반환."""
    from baldur.settings.root import get_config

    return get_config().services_group.recovery_coordinator


def reset_recovery_coordinator_settings() -> None:
    """캐시 초기화 (테스트용)."""
    from baldur.settings.root import get_config

    try:
        del get_config().services_group.__dict__["recovery_coordinator"]
    except KeyError:
        pass
