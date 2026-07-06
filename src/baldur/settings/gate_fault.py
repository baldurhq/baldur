"""
Gate Fault Settings - Pydantic v2.

Error Budget Gate 내부 장애 감지기 설정.
Gate가 Error Budget 서비스(Redis/DB)에 반복 접근 실패 시,
매번 타임아웃을 기다리지 않고 즉시 Fail-Open으로 응답합니다.

Source:
- services/error_budget_gate/fault_detector.py (GateFaultDetector)

Environment Variables:
    BALDUR_GATE_FAULT_FAILURE_THRESHOLD=5
    BALDUR_GATE_FAULT_RECOVERY_TIMEOUT_SECONDS=30
"""

import structlog
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings

from baldur.settings.base import make_settings_config

logger = structlog.get_logger()


class GateFaultSettings(BaseSettings):
    """
    Gate Fault Detector 설정.

    Error Budget Gate 내부의 장애 감지 및 복구 설정을 정의합니다.
    ⚠️ 주의: 이것은 메인 CircuitBreakerService와 다릅니다!
    - GateFaultDetector: Gate 내부용, 메모리 전용 (외부 의존성 없음)
    - CircuitBreakerService: 외부 API 호출용, 분산 환경 지원
    """

    model_config = make_settings_config("BALDUR_GATE_FAULT_")

    # ==========================================================================
    # Failure Detection (from fault_detector.py line 46)
    # ==========================================================================
    failure_threshold: int = Field(
        default=5,
        ge=1,
        le=50,
        description="Threshold to transition to DEGRADED state (consecutive failure count)",
    )

    # ==========================================================================
    # Recovery Settings (from fault_detector.py line 46)
    # ==========================================================================
    recovery_timeout_seconds: int = Field(
        default=30,
        ge=5,
        le=300,
        description="Wait time before recovery attempt from DEGRADED state (seconds)",
    )

    @field_validator("failure_threshold")
    @classmethod
    def validate_failure_threshold(cls, v: int) -> int:
        """failure_threshold가 너무 작으면 경고."""
        if v < 3:
            logger.warning(
                "gate_fault_settings.low_consider_using_avoid",
                setting_value=v,
            )
        return v


def get_gate_fault_settings() -> "GateFaultSettings":
    from baldur.settings.root import get_config

    return get_config().meta.gate_fault


def reset_gate_fault_settings() -> None:
    from baldur.settings.root import get_config

    try:
        del get_config().meta.__dict__["gate_fault"]
    except KeyError:
        pass
