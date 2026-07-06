"""
Air-Gap Settings - Pydantic v2.

Air-Gap 저장소 관련 설정입니다.
비즈니스 DB와 Baldur 엔진 사이의 중간 저장소 역할을 합니다.

Replaces:
- adapters/airgap/redis_adapter.py:DEFAULT_TTL

Environment Variables:
    BALDUR_AIRGAP_REDIS_TTL=3600
    BALDUR_AIRGAP_KEY_PREFIX=sh:airgap:

Usage:
    from baldur.settings.airgap import get_airgap_settings
    settings = get_airgap_settings()
    ttl = settings.redis_ttl
"""

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings

from baldur.settings.base import make_settings_config


class AirGapSettings(BaseSettings):
    """
    Air-Gap 저장소 설정.

    Air-Gap은 비즈니스 레이어와 Baldur 엔진 사이의 분리 계층입니다.
    비즈니스 DB 변경 시 Redis에 요약 상태를 기록하고,
    Baldur 엔진은 Redis에서만 상태를 조회합니다.

    Attributes:
        redis_ttl: Redis에 저장되는 Air-Gap 상태의 TTL (초)
        key_prefix: Redis 키 접두사
    """

    model_config = make_settings_config("BALDUR_AIRGAP_")

    # ==========================================================================
    # Redis TTL - from adapters/airgap/redis_adapter.py
    # ==========================================================================
    redis_ttl: int = Field(
        default=3600,
        ge=60,
        le=86400,
        description="TTL for Air-Gap state stored in Redis (seconds). Default 1 hour.",
    )

    # ==========================================================================
    # Key Prefix
    # ==========================================================================
    key_prefix: str = Field(
        default="sh:airgap:",
        description="Redis key prefix",
    )

    @field_validator("key_prefix")
    @classmethod
    def validate_key_prefix(cls, v: str) -> str:
        """키 접두사가 콜론으로 끝나는지 확인."""
        if not v.endswith(":"):
            return f"{v}:"
        return v


def get_airgap_settings() -> "AirGapSettings":
    from baldur.settings.root import get_config

    return get_config().testing.airgap


def reset_airgap_settings() -> None:
    from baldur.settings.root import get_config

    try:
        del get_config().testing.__dict__["airgap"]
    except KeyError:
        pass
