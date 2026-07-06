"""
CBStateCache Settings - Pydantic v2.

Circuit Breaker 상태 캐시 설정.
TTL 및 Jitter 범위를 환경변수로 설정 가능.

Environment Variables:
    BALDUR_STATE_CACHE_BASE_TTL=5.0
    BALDUR_STATE_CACHE_JITTER_RANGE=0.5
"""

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings

from baldur.settings.base import make_settings_config


class StateCacheSettings(BaseSettings):
    """
    CBStateCache 설정.

    TTL 기반 로컬 캐싱으로 네트워크 호출 최소화.
    Polling Jitter로 Thundering Herd 방지.
    """

    model_config = make_settings_config("BALDUR_STATE_CACHE_")

    # ==========================================================================
    # TTL 설정
    # ==========================================================================
    base_ttl: float = Field(
        default=5.0,
        ge=0.1,
        le=300.0,
        description="Base cache TTL (seconds). Cache invalidates after this duration.",
    )

    # ==========================================================================
    # Jitter 설정
    # ==========================================================================
    jitter_range: float = Field(
        default=0.5,
        ge=0.0,
        le=10.0,
        description="Random jitter range (seconds). Applies +/-jitter_range randomly to TTL.",
    )

    @model_validator(mode="after")
    def validate_jitter(self) -> "StateCacheSettings":
        """jitter_range가 base_ttl보다 크면 안됨."""
        if self.jitter_range > self.base_ttl:
            raise ValueError(
                f"jitter_range ({self.jitter_range}) should not exceed "
                f"base_ttl ({self.base_ttl})"
            )
        return self


# =============================================================================
# Singleton Pattern
# =============================================================================


def get_state_cache_settings() -> "StateCacheSettings":
    """
    캐시된 StateCacheSettings 인스턴스 반환.

    Returns:
        StateCacheSettings: 싱글톤 인스턴스
    """
    from baldur.settings.root import get_config

    return get_config().scaling.state_cache


def reset_state_cache_settings() -> None:
    """
    캐시된 설정 초기화 (테스트용).
    """
    from baldur.settings.root import get_config

    try:
        del get_config().scaling.__dict__["state_cache"]
    except KeyError:
        pass
