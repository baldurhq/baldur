"""
Regional Recovery Policy Settings - Pydantic v2.

지역별 복구 정책 설정입니다.

Replaces:
- services/coordination/regional_recovery_policy.py 내 하드코딩된 값들
- models.py:approval_timeout_minutes, escalation_intervals

Environment Variables:
    BALDUR_REGIONAL_RECOVERY_POLICY_ERROR_RATE_THRESHOLD=0.10
    BALDUR_REGIONAL_RECOVERY_POLICY_SUCCESS_RATE_THRESHOLD=0.95
    BALDUR_REGIONAL_RECOVERY_POLICY_STABILITY_CHECK_DURATION_MINUTES=10

Reference:
- docs/baldur/middleware_system/92_CONFIG_IMPLEMENTATION_GUIDE.md (Week 4 [26])
- docs/baldur/middleware_system/91_CONFIG_INVENTORY.md §6.4, §8.6
"""

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings

from baldur.settings.base import make_settings_config
from baldur.settings.field_types import TinyCount


class RegionalRecoveryPolicySettings(BaseSettings):
    """
    지역별 복구 정책 설정.

    임계치:
    - error_rate_threshold: 복구 시작 에러율 임계치 (10%)
    - success_rate_threshold: 복구 완료 성공률 임계치 (95%)

    시간 설정:
    - stability_check_duration_minutes: 안정성 확인 기간 (10분)
    - max_recovery_duration_minutes: 최대 복구 시간 (60분)
    - cooldown_minutes: 복구 후 쿨다운 (15분)

    승인:
    - approval_timeout_minutes: 승인 타임아웃 (60분)
    - escalation_intervals: 에스컬레이션 간격 ([15, 30, 60]분)

    동시성:
    - max_concurrent_recoveries: 최대 동시 복구 수 (3)
    """

    model_config = make_settings_config("BALDUR_REGIONAL_RECOVERY_POLICY_")

    # ==========================================================================
    # Thresholds - from regional_recovery_policy.py
    # ==========================================================================
    error_rate_threshold: float = Field(
        default=0.10,
        ge=0.01,
        le=0.5,
        description="Error rate threshold to initiate recovery (10%)",
    )

    success_rate_threshold: float = Field(
        default=0.95,
        ge=0.8,
        le=1.0,
        description="Success rate threshold for recovery completion (95%)",
    )

    auto_approve_threshold: float = Field(
        default=0.1,
        ge=0.01,
        le=0.3,
        description="Auto-approval threshold (impact ratio)",
    )

    # ==========================================================================
    # Time Settings - from regional_recovery_policy.py
    # ==========================================================================
    stability_check_duration_minutes: int = Field(
        default=10,
        ge=5,
        le=60,
        description="Stability check duration (minutes)",
    )

    max_recovery_duration_minutes: int = Field(
        default=60,
        ge=15,
        le=480,
        description="Maximum recovery duration (minutes)",
    )

    cooldown_minutes: int = Field(
        default=15,
        ge=5,
        le=120,
        description="Post-recovery cooldown period (minutes)",
    )

    # ==========================================================================
    # Approval - from regional_recovery_policy.py, models.py
    # ==========================================================================
    approval_timeout_minutes: int = Field(
        default=60,
        ge=15,
        le=240,
        description="Approval timeout (minutes)",
    )

    escalation_interval_1: int = Field(
        default=15,
        ge=5,
        le=60,
        description="First escalation interval (minutes)",
    )

    escalation_interval_2: int = Field(
        default=30,
        ge=10,
        le=120,
        description="Second escalation interval (minutes)",
    )

    escalation_interval_3: int = Field(
        default=60,
        ge=15,
        le=240,
        description="Third escalation interval (minutes)",
    )

    # ==========================================================================
    # Concurrency - from regional_recovery_policy.py
    # ==========================================================================
    max_concurrent_recoveries: TinyCount = Field(
        default=3,
        description="Maximum concurrent recoveries",
    )

    # ==========================================================================
    # Accountability - from models.py
    # ==========================================================================
    ready_to_restore_timeout_hours: float = Field(
        default=4.0,
        ge=1.0,
        le=24.0,
        description="Ready-to-restore timeout (hours)",
    )

    auto_restore_after_hours: float = Field(
        default=8.0,
        ge=2.0,
        le=48.0,
        description="Auto-restore time (hours)",
    )

    def get_escalation_intervals(self) -> list[int]:
        """에스컬레이션 간격 리스트 반환."""
        return [
            self.escalation_interval_1,
            self.escalation_interval_2,
            self.escalation_interval_3,
        ]

    @model_validator(mode="after")
    def validate_thresholds(self) -> "RegionalRecoveryPolicySettings":
        """임계치 검증."""
        if self.error_rate_threshold >= self.success_rate_threshold:
            raise ValueError(
                f"error_rate_threshold ({self.error_rate_threshold}) must be less than "
                f"success_rate_threshold ({self.success_rate_threshold})"
            )
        # 에스컬레이션 간격이 오름차순인지 확인
        intervals = self.get_escalation_intervals()
        for i in range(1, len(intervals)):
            if intervals[i] <= intervals[i - 1]:
                raise ValueError(
                    f"Escalation intervals must be in ascending order: {intervals}"
                )
        return self


def get_regional_recovery_policy_settings() -> "RegionalRecoveryPolicySettings":
    from baldur.settings.root import get_config

    return get_config().multi_region.regional_recovery_policy


def reset_regional_recovery_policy_settings() -> None:
    from baldur.settings.root import get_config

    try:
        del get_config().multi_region.__dict__["regional_recovery_policy"]
    except KeyError:
        pass
