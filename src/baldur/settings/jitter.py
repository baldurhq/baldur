"""
Jitter Settings - Pydantic v2.

Thundering Herd 방지를 위한 Jitter 설정.
분산 환경에서 동시 시작되는 인스턴스들의 DB 쿼리를 시간적으로 분산시킵니다.

AdaptiveJitter 임계값도 포함:
- 에러 버짓 기반 위험/안전 판단
- 시스템 부하 기반 고부하/저부하 판단

Environment Variables:
    BALDUR_JITTER_MAX_DELAY_SECONDS=60.0
    BALDUR_JITTER_MIN_DELAY_SECONDS=0.0
    BALDUR_JITTER_ERROR_BUDGET_DANGER_THRESHOLD=0.2
    BALDUR_JITTER_ERROR_BUDGET_SAFE_THRESHOLD=0.5
    BALDUR_JITTER_LOAD_HIGH_THRESHOLD=0.8
    BALDUR_JITTER_LOAD_LOW_THRESHOLD=0.3
"""

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings

from baldur.settings.base import make_settings_config


class JitterSettings(BaseSettings):
    """
    Jitter 설정.

    Thundering Herd 방지를 위한 무작위 지연 설정을 정의합니다.
    환경별 권장 설정:
    - 단일 서버: 0초 (비활성화)
    - K8s 10 Pods: 30초
    - K8s 100+ Pods: 60초
    """

    model_config = make_settings_config("BALDUR_JITTER_")

    # ==========================================================================
    # Delay Settings (from utils/jitter.py lines 27-28, 75-76, 99-100, 122-123)
    # ==========================================================================
    max_delay_seconds: float = Field(
        default=60.0,
        ge=0.0,
        le=300.0,
        description="Maximum delay time (seconds)",
    )
    min_delay_seconds: float = Field(
        default=0.0,
        ge=0.0,
        le=60.0,
        description="Minimum delay time (seconds)",
    )

    # ==========================================================================
    # Startup Jitter (for AppConfig.ready())
    # ==========================================================================
    startup_max_delay_seconds: float = Field(
        default=30.0,
        ge=0.0,
        le=120.0,
        description="Maximum startup delay time (seconds)",
    )

    # ==========================================================================
    # Feature Toggle
    # ==========================================================================
    enabled: bool = Field(
        default=True,
        description="Enable jitter. If False, no delay is applied.",
    )

    # ==========================================================================
    # AdaptiveJitter 임계값 (에러 버짓 기반)
    # ==========================================================================
    error_budget_danger_threshold: float = Field(
        default=0.2,
        ge=0.01,
        le=0.5,
        description="Error budget danger threshold. At or below this level, maximum jitter is applied.",
    )
    error_budget_safe_threshold: float = Field(
        default=0.5,
        ge=0.3,
        le=0.9,
        description="Error budget safe threshold. At or above this level, minimum jitter is applied.",
    )

    # ==========================================================================
    # AdaptiveJitter 임계값 (부하 기반)
    # ==========================================================================
    load_high_threshold: float = Field(
        default=0.8,
        ge=0.5,
        le=0.99,
        description="High load threshold. At or above this level, the system is in danger state.",
    )
    load_low_threshold: float = Field(
        default=0.3,
        ge=0.01,
        le=0.5,
        description="Low load threshold. At or below this level, the system is in relaxed state.",
    )

    @model_validator(mode="after")
    def validate_delay_range(self) -> "JitterSettings":
        """min_delay가 max_delay보다 작은지 확인, 임계값 순서 검증."""
        if self.min_delay_seconds > self.max_delay_seconds:
            raise ValueError(
                f"min_delay_seconds ({self.min_delay_seconds}) cannot be greater than "
                f"max_delay_seconds ({self.max_delay_seconds})"
            )
        if self.error_budget_danger_threshold >= self.error_budget_safe_threshold:
            raise ValueError(
                f"error_budget_danger_threshold ({self.error_budget_danger_threshold}) "
                f"must be less than error_budget_safe_threshold ({self.error_budget_safe_threshold})"
            )
        if self.load_low_threshold >= self.load_high_threshold:
            raise ValueError(
                f"load_low_threshold ({self.load_low_threshold}) "
                f"must be less than load_high_threshold ({self.load_high_threshold})"
            )
        return self


def get_jitter_settings() -> "JitterSettings":
    from baldur.settings.root import get_config

    return get_config().testing.jitter


def reset_jitter_settings() -> None:
    from baldur.settings.root import get_config

    try:
        del get_config().testing.__dict__["jitter"]
    except KeyError:
        pass
