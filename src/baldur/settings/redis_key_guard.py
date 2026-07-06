"""
Redis Key Guard Settings - Pydantic v2.

Single Source of Truth for Redis key priority eviction configuration.

Replaces:
- services/coordination/redis_key_guard.py:RedisKeyPriorityEviction memory thresholds

Environment Variables:
    BALDUR_REDIS_KEY_GUARD_MEMORY_WARNING_THRESHOLD=80.0
    BALDUR_REDIS_KEY_GUARD_MEMORY_CRITICAL_THRESHOLD=90.0
    ... etc

Reference:
- docs/baldur/middleware_system/92_CONFIG_IMPLEMENTATION_GUIDE.md
- docs/baldur/middleware_system/77_RECOVERY_COORDINATOR.md#11.1
"""

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings

from baldur.settings.base import make_settings_config


class RedisKeyGuardSettings(BaseSettings):
    """
    Redis Key Priority Eviction configuration with validation.

    Redis maxmemory 상황에서 핵심 거버넌스 키(P0)를 보호합니다.

    All defaults match:
    - services/coordination/redis_key_guard.py:RedisKeyPriorityEviction
    """

    model_config = make_settings_config("BALDUR_REDIS_KEY_GUARD_")

    # ==========================================================================
    # Memory Threshold Settings
    # ==========================================================================
    memory_warning_threshold: float = Field(
        default=80.0,
        ge=50.0,
        le=95.0,
        description="Memory warning threshold (%). Triggers warning when reached",
    )
    memory_critical_threshold: float = Field(
        default=90.0,
        ge=60.0,
        le=99.0,
        description="Memory critical threshold (%). Triggers emergency cleanup when reached",
    )

    # ==========================================================================
    # Eviction Settings
    # ==========================================================================
    target_free_percent: float = Field(
        default=20.0,
        ge=5.0,
        le=50.0,
        description="Target free memory percentage during emergency cleanup (%)",
    )

    # ==========================================================================
    # TTL Settings for Volatile Keys
    # ==========================================================================
    cache_ttl_seconds: int = Field(
        default=3600,
        ge=60,
        le=86400,
        description="Default TTL for cache keys (seconds). 1 hour",
    )
    metrics_realtime_ttl_seconds: int = Field(
        default=600,
        ge=60,
        le=3600,
        description="TTL for real-time metrics keys (seconds). 10 minutes",
    )
    metrics_aggregate_ttl_seconds: int = Field(
        default=7200,
        ge=600,
        le=86400,
        description="TTL for aggregate metrics keys (seconds). 2 hours",
    )
    audit_event_ttl_seconds: int = Field(
        default=604800,
        ge=86400,
        le=2592000,
        description="TTL for audit event keys (seconds). 7 days",
    )
    temp_key_ttl_seconds: int = Field(
        default=300,
        ge=60,
        le=3600,
        description="TTL for temporary keys (seconds). 5 minutes",
    )

    @field_validator("memory_critical_threshold")
    @classmethod
    def validate_critical_gt_warning(cls, v: float, info) -> float:
        """Ensure critical threshold > warning threshold."""
        # info.data는 이미 검증된 필드들을 포함
        warning = info.data.get("memory_warning_threshold", 80.0)
        if v <= warning:
            raise ValueError(
                f"memory_critical_threshold ({v}) must be > "
                f"memory_warning_threshold ({warning})"
            )
        return v


def get_redis_key_guard_settings() -> "RedisKeyGuardSettings":
    from baldur.settings.root import get_config

    return get_config().coordination.redis_key_guard


def reset_redis_key_guard_settings() -> None:
    from baldur.settings.root import get_config

    try:
        del get_config().coordination.__dict__["redis_key_guard"]
    except KeyError:
        pass
