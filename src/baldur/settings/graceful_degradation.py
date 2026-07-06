"""
Graceful Degradation Settings - Pydantic v2.

Hash chain 저하 수준 및 Fallback 체인 설정.
Redis 장애 시 단계적 저하 및 복구 설정을 정의합니다.

Source:
- audit/graceful_degradation/enums.py (FallbackConfig, CircuitBreakerConfig)

Environment Variables:
    BALDUR_GRACEFUL_DEGRADATION_REDIS_TIMEOUT_SECONDS=5.0
    BALDUR_GRACEFUL_DEGRADATION_REPLICA_TIMEOUT_SECONDS=3.0
    BALDUR_GRACEFUL_DEGRADATION_MEMORY_MAX_ENTRIES=10000
    BALDUR_GRACEFUL_DEGRADATION_CB_FAILURE_THRESHOLD=5
    BALDUR_GRACEFUL_DEGRADATION_CB_RECOVERY_TIMEOUT_SECONDS=30.0
    BALDUR_GRACEFUL_DEGRADATION_CB_HALF_OPEN_REQUESTS=3
    BALDUR_GRACEFUL_DEGRADATION_CB_SUCCESS_THRESHOLD=2
"""

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings

from baldur.settings.base import make_settings_config
from baldur.settings.field_types import (
    STANDARD_TIMEOUT_SECONDS,
    SmallCount,
)
from baldur.settings.validators import warn_above


class GracefulDegradationSettings(BaseSettings):
    """
    Graceful Degradation 설정.

    Redis 장애 시 Fallback 체인 및 Circuit Breaker 설정을 정의합니다.
    저하 수준:
    - NORMAL: Redis 사용
    - DEGRADED: 로컬 Fallback 사용
    - EMERGENCY: 메모리 전용
    - READONLY: 읽기 전용
    """

    model_config = make_settings_config("BALDUR_GRACEFUL_DEGRADATION_")

    # ==========================================================================
    # Fallback Config (from enums.py FallbackConfig lines 45-48)
    # ==========================================================================
    redis_timeout_seconds: float = Field(
        default=5.0,
        ge=0.5,
        le=30.0,
        description="Redis connection timeout (seconds)",
    )
    replica_timeout_seconds: float = Field(
        default=3.0,
        ge=0.5,
        le=30.0,
        description="Replica connection timeout (seconds)",
    )
    memory_max_entries: int = Field(
        default=10000,
        ge=100,
        le=1000000,
        description="Maximum entries for memory fallback",
    )
    key_prefix: str = Field(
        default="baldur:",
        min_length=1,
        max_length=50,
        description="Redis key prefix",
    )

    # ==========================================================================
    # Circuit Breaker Config (from enums.py CircuitBreakerConfig lines 55-58)
    # ==========================================================================
    cb_failure_threshold: int = Field(
        default=5,
        ge=1,
        le=50,
        description="Circuit Breaker OPEN threshold",
    )
    cb_recovery_timeout_seconds: float = Field(
        default=STANDARD_TIMEOUT_SECONDS,
        ge=5.0,
        le=300.0,
        description="Circuit Breaker recovery wait time (seconds)",
    )
    cb_half_open_requests: SmallCount = Field(
        default=3,
        description="Number of requests allowed in HALF_OPEN state",
    )
    cb_success_threshold: SmallCount = Field(
        default=2,
        description="Success count to transition from HALF_OPEN to CLOSED",
    )

    @field_validator("redis_timeout_seconds")
    @classmethod
    def _warn_redis_timeout(cls, v: float) -> float:
        """redis_timeout이 너무 길면 경고."""
        return warn_above(
            10.0, "graceful_degradation_settings.high_consider_using_responsiveness"
        )(v)


def get_graceful_degradation_settings() -> "GracefulDegradationSettings":
    from baldur.settings.root import get_config

    return get_config().scaling.graceful_degradation


def reset_graceful_degradation_settings() -> None:
    from baldur.settings.root import get_config

    try:
        del get_config().scaling.__dict__["graceful_degradation"]
    except KeyError:
        pass
