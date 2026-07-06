"""
Ring Buffer Settings - Pydantic v2.

Shadow Logging을 위한 Ring Buffer 설정.
비침투 원칙에 따라 DROP_OLDEST가 기본값이며, 메인 애플리케이션 성능에 영향을 주지 않습니다.

Source:
- audit/ring_buffer.py

Environment Variables:
    BALDUR_RING_BUFFER_CAPACITY=10000
    BALDUR_RING_BUFFER_BATCH_MAX_SIZE=100
    BALDUR_RING_BUFFER_STRATEGY=drop_oldest
"""

from typing import Literal

import structlog
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings

from baldur.settings.base import make_settings_config

logger = structlog.get_logger()


class RingBufferSettings(BaseSettings):
    """
    Ring Buffer 설정.

    Shadow Logging을 위한 비침투 버퍼 설정을 정의합니다.
    메인 애플리케이션을 절대 블로킹하지 않습니다.
    """

    model_config = make_settings_config("BALDUR_RING_BUFFER_")

    # ==========================================================================
    # Buffer Settings (from ring_buffer.py line 67)
    # ==========================================================================
    capacity: int = Field(
        default=10000,
        ge=100,
        le=1000000,
        description="Ring Buffer maximum capacity",
    )

    # ==========================================================================
    # Batch Settings (from ring_buffer.py - get_batch default)
    # ==========================================================================
    batch_max_size: int = Field(
        default=100,
        ge=1,
        le=10000,
        description="Maximum items per batch processing",
    )

    # ==========================================================================
    # Strategy Settings (from ring_buffer.py BackpressureStrategy)
    # ==========================================================================
    strategy: Literal["drop_oldest", "drop_newest"] = Field(
        default="drop_oldest",
        description="Backpressure strategy. drop_oldest (recommended: non-intrusive) or drop_newest.",
    )

    @field_validator("capacity")
    @classmethod
    def validate_capacity(cls, v: int) -> int:
        """capacity가 너무 크면 경고."""
        if v > 100000:
            logger.warning(
                "ring_buffer_settings.high_consider_using_memory",
                setting_value=v,
            )
        return v


# =============================================================================
# Singleton Pattern
# =============================================================================


def get_ring_buffer_settings() -> "RingBufferSettings":
    """
    캐시된 RingBufferSettings 인스턴스 반환.

    Returns:
        RingBufferSettings: 싱글톤 인스턴스
    """
    from baldur.settings.root import get_config

    return get_config().scaling.ring_buffer


def reset_ring_buffer_settings() -> None:
    """
    캐시된 설정 초기화 (테스트용).

    환경 변수 변경 후 설정을 다시 로드하려면 이 함수를 호출하세요.
    """
    from baldur.settings.root import get_config

    try:
        del get_config().scaling.__dict__["ring_buffer"]
    except KeyError:
        pass
