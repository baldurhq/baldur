"""
RuntimeFeedbackLoop Settings - Pydantic v2.

실시간 피드백 루프 자율 조정 설정.
연속 실패, 롤백 쿨다운, 조정 후 대기 시간 등을 환경변수로 설정 가능.

Environment Variables:
    BALDUR_RUNTIME_FEEDBACK_MAX_CONSECUTIVE_FAILURES=3
    BALDUR_RUNTIME_FEEDBACK_ROLLBACK_COOLDOWN=120
    BALDUR_RUNTIME_FEEDBACK_ADJUSTMENT_WAIT=30
"""

from pydantic import Field
from pydantic_settings import BaseSettings

from baldur.settings.base import make_settings_config
from baldur.settings.field_types import SmallCount


class RuntimeFeedbackSettings(BaseSettings):
    """
    RuntimeFeedbackLoop 설정.

    자율 조정 실패 시 자동 롤백 및 피드백 루프 일시 정지를 위한 설정.
    """

    model_config = make_settings_config("BALDUR_RUNTIME_FEEDBACK_")

    # ==========================================================================
    # 연속 실패 관련 설정
    # ==========================================================================
    max_consecutive_failures: SmallCount = Field(
        default=3,
        description="Maximum consecutive failure count. Auto-pauses feedback loop when exceeded.",
    )

    # ==========================================================================
    # 롤백 관련 설정
    # ==========================================================================
    rollback_cooldown: int = Field(
        default=120,
        ge=10,
        le=3600,
        description="Stabilization wait time after rollback (seconds). Blocks further adjustments during this period.",
    )

    # ==========================================================================
    # 조정 후 대기 설정
    # ==========================================================================
    adjustment_wait: int = Field(
        default=30,
        ge=5,
        le=600,
        description="Wait time after adjustment to verify effect (seconds). For metrics collection.",
    )

    # ==========================================================================
    # 저하 감지 임계값 (338: Settings Gap Phase 2)
    # ==========================================================================
    error_increase_threshold: float = Field(
        default=0.2,
        ge=0.01,
        le=1.0,
        description="Error rate increase ratio to detect degradation (20% default).",
    )
    latency_increase_threshold: float = Field(
        default=0.5,
        ge=0.05,
        le=5.0,
        description="Latency increase ratio to detect degradation (50% default).",
    )
    zero_to_error_threshold: float = Field(
        default=0.05,
        ge=0.001,
        le=0.5,
        description="Error rate threshold for zero-to-error spike detection (5% default).",
    )


def get_runtime_feedback_settings() -> "RuntimeFeedbackSettings":
    from baldur.settings.root import get_config

    return get_config().meta.runtime_feedback


def reset_runtime_feedback_settings() -> None:
    from baldur.settings.root import get_config

    try:
        del get_config().meta.__dict__["runtime_feedback"]
    except KeyError:
        pass
