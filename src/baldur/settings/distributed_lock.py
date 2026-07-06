"""
Distributed Lock Settings - Pydantic v2.

분산 환경에서의 락(Lock) 관련 설정입니다.

Replaces:
- services/coordination/distributed_recovery_lock.py:DEFAULT_LOCK_TIMEOUT
- adapters/cache/redis_adapter.py:RedisDistributedLock 설정

Environment Variables:
    BALDUR_DISTRIBUTED_LOCK_TIMEOUT_MINUTES=30
    BALDUR_DISTRIBUTED_LOCK_RETRY_INTERVAL_SECONDS=0.1

Reference:
- docs/baldur/middleware_system/92_CONFIG_IMPLEMENTATION_GUIDE.md (Week 3 [17])
- docs/baldur/middleware_system/91_CONFIG_INVENTORY.md §6.31, §13.4
- docs/baldur/middleware_system/77_RECOVERY_COORDINATOR.md#8.3
"""

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings

from baldur.settings.base import make_settings_config
from baldur.settings.field_types import LargeCount
from baldur.settings.validators import warn_above, warn_below


class DistributedLockSettings(BaseSettings):
    """
    분산 락 설정.

    Redis 기반 분산 락의 타임아웃 및 재시도 정책을 관리합니다.

    Features:
    - 락 자동 만료로 좀비 락 방지
    - 재시도 간격 및 최대 횟수 설정
    - 연장(Extend) 설정
    """

    model_config = make_settings_config("BALDUR_DISTRIBUTED_LOCK_")

    # ==========================================================================
    # Lock Timeout (from distributed_recovery_lock.py#L92)
    # ==========================================================================
    timeout_minutes: int = Field(
        default=30,
        ge=1,
        le=120,
        description="Lock auto-expiry time (minutes). Based on maximum expected recovery time.",
    )

    # ==========================================================================
    # Retry Settings
    # ==========================================================================
    retry_interval_seconds: float = Field(
        default=0.1,
        ge=0.01,
        le=5.0,
        description="Lock acquisition retry interval (seconds)",
    )

    max_retry_attempts: LargeCount = Field(
        default=100,
        description="Maximum lock acquisition retry attempts",
    )

    # ==========================================================================
    # Extend Settings
    # ==========================================================================
    extend_interval_seconds: int = Field(
        default=60,
        ge=10,
        le=300,
        description="Lock extension check interval (seconds)",
    )

    # ==========================================================================
    # Tier 3 Local Lock Fallback (D-3)
    # ==========================================================================
    local_fallback_enabled: bool = Field(
        default=False,
        description=(
            "Enable Tier 3 Local Lock fallback when Redis and K8s are both unavailable. "
            "Only safe for single-instance deployments. "
            "Multi-pod environments risk split-brain if enabled."
        ),
    )

    # ==========================================================================
    # Key Prefix (IMMUTABLE - 참조용으로만 포함)
    # ==========================================================================
    key_prefix: str = Field(
        default="baldur:",
        description="Redis lock key prefix (not recommended to change)",
    )

    @field_validator("timeout_minutes")
    @classmethod
    def _warn_timeout_minutes(cls, v: int) -> int:
        """타임아웃이 너무 길면 경고."""
        return warn_above(60, "distributed_lock.timeout_too_long")(v)

    @field_validator("retry_interval_seconds")
    @classmethod
    def _warn_retry_interval_seconds(cls, v: float) -> float:
        """재시도 간격이 너무 짧으면 경고."""
        return warn_below(0.05, "distributed_lock.retry_interval_too_short")(v)

    def get_timeout_seconds(self) -> int:
        """타임아웃을 초 단위로 반환."""
        return self.timeout_minutes * 60

    def get_timeout_ms(self) -> int:
        """타임아웃을 밀리초 단위로 반환."""
        return self.timeout_minutes * 60 * 1000


def get_distributed_lock_settings() -> "DistributedLockSettings":
    from baldur.settings.root import get_config

    return get_config().coordination.distributed_lock


def reset_distributed_lock_settings() -> None:
    from baldur.settings.root import get_config

    try:
        del get_config().coordination.__dict__["distributed_lock"]
    except KeyError:
        pass
