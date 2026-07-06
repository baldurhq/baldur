"""
Rate Limit Throttle Integration Settings - Pydantic v2.

429 응답과 AdaptiveThrottle 간의 연동 설정을 정의합니다.

Features:
    - 429 발생 시 throttle limit 자동 감소
    - 연속 429 횟수별 감소 비율 설정
    - Key-Service 매핑 (인접 간섭 방지)
    - Recovery 전략 설정
    - 에스컬레이션 설정

Environment Variables:
    BALDUR_RATE_LIMIT_THROTTLE_INTEGRATION_ENABLED=true
    BALDUR_RATE_LIMIT_THROTTLE_INTEGRATION_DEBOUNCE_WINDOW_SECONDS=5.0
    BALDUR_RATE_LIMIT_THROTTLE_INTEGRATION_ESCALATION_ENABLED=true
    ... etc
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings

from baldur.settings.base import make_settings_config
from baldur.settings.field_types import (
    TinyCount,
)


class RateLimitThrottleIntegrationSettings(BaseSettings):
    """
    429-Throttle 연동 설정.

    외부 API의 429 응답 수신 시 AdaptiveThrottle의 limit을
    자동으로 조정하기 위한 설정입니다.
    """

    model_config = make_settings_config("BALDUR_RATE_LIMIT_THROTTLE_INTEGRATION_")

    # =========================================================================
    # 기본 활성화 설정
    # =========================================================================
    enabled: bool = Field(
        default=True,
        description="Enable throttle limit reduction on 429 responses",
    )

    # =========================================================================
    # 연속 429 횟수별 limit 감소 비율
    # =========================================================================
    reduction_ratio_1: float = Field(
        default=0.8,
        ge=0.1,
        le=1.0,
        description="Retention ratio after 1st 429 (0.8 = 20% reduction)",
    )
    reduction_ratio_2: float = Field(
        default=0.6,
        ge=0.1,
        le=1.0,
        description="Retention ratio after 2 consecutive 429s (0.6 = 40% reduction)",
    )
    reduction_ratio_3: float = Field(
        default=0.5,
        ge=0.1,
        le=1.0,
        description="Retention ratio after 3+ consecutive 429s (0.5 = 50% reduction)",
    )

    # =========================================================================
    # Recovery strategy settings
    # =========================================================================
    recovery_strategy: Literal["immediate", "gradual"] = Field(
        default="gradual",
        description="Limit recovery strategy after cooldown expires",
    )
    recovery_dampening_steps: TinyCount = Field(
        default=3,
        description="Number of steps for gradual recovery",
    )

    # =========================================================================
    # EventBus 디바운싱 설정
    # =========================================================================
    debounce_window_seconds: float = Field(
        default=5.0,
        ge=0.0,
        le=60.0,
        description="Event deduplication window for the same key (seconds)",
    )

    # =========================================================================
    # Key-Service 매핑 (인접 간섭 방지)
    # =========================================================================
    default_service: str = Field(
        default="default",
        description="Default service for unmapped keys",
    )

    # Note: key_to_service_mapping은 환경변수로 설정하기 어려우므로
    # 코드에서 직접 설정하거나 별도 설정 파일 사용

    def get_reduction_ratio(self, consecutive_429s: int) -> float:
        """
        연속 429 횟수에 따른 감소 비율 반환.

        Args:
            consecutive_429s: 연속 429 횟수

        Returns:
            유지 비율 (예: 0.8 = 20% 감소)
        """
        if consecutive_429s >= 3:
            return self.reduction_ratio_3
        if consecutive_429s == 2:
            return self.reduction_ratio_2
        return self.reduction_ratio_1


def get_rate_limit_throttle_integration_settings() -> (
    RateLimitThrottleIntegrationSettings
):
    from baldur.settings.root import get_config

    return get_config().scaling.rate_limit_throttle_integration


# Backward-compatible alias
get_rate_limit_throttle_settings = get_rate_limit_throttle_integration_settings


def reset_rate_limit_throttle_integration_settings() -> None:
    from baldur.settings.root import get_config

    try:
        del get_config().scaling.__dict__["rate_limit_throttle_integration"]
    except KeyError:
        pass


# Backward-compatible alias
clear_settings_cache = reset_rate_limit_throttle_integration_settings
