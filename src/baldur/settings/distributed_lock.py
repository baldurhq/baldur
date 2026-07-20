"""
Distributed Lock Settings - Pydantic v2.

Lock settings for distributed deployments.

Replaces:
- services/coordination/distributed_recovery_lock.py:DEFAULT_LOCK_TIMEOUT
- adapters/cache/redis_adapter.py:RedisDistributedLock settings

Environment Variables:
    BALDUR_DISTRIBUTED_LOCK_TIMEOUT_MINUTES=30
    BALDUR_DISTRIBUTED_LOCK_RETRY_INTERVAL_SECONDS=0.1
"""

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings

from baldur.settings.base import make_settings_config
from baldur.settings.field_types import LargeCount
from baldur.settings.validators import warn_above, warn_below


class DistributedLockSettings(BaseSettings):
    """
    Distributed lock settings.

    Manages the timeout and retry policy of the Redis-based distributed lock.

    Features:
    - Automatic lock expiry to prevent zombie locks
    - Retry interval and maximum attempt settings
    - Extend settings
    """

    model_config = make_settings_config("BALDUR_DISTRIBUTED_LOCK_")

    # ==========================================================================
    # Lock Timeout (from distributed_recovery_lock.py)
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
    # Key Prefix (IMMUTABLE - included for reference only)
    # ==========================================================================
    key_prefix: str = Field(
        default="baldur:",
        description="Redis lock key prefix (not recommended to change)",
    )

    @field_validator("timeout_minutes")
    @classmethod
    def _warn_timeout_minutes(cls, v: int) -> int:
        """Warn when the timeout is too long."""
        return warn_above(60, "distributed_lock.timeout_too_long")(v)

    @field_validator("retry_interval_seconds")
    @classmethod
    def _warn_retry_interval_seconds(cls, v: float) -> float:
        """Warn when the retry interval is too short."""
        return warn_below(0.05, "distributed_lock.retry_interval_too_short")(v)

    def get_timeout_seconds(self) -> int:
        """Return the timeout in seconds."""
        return self.timeout_minutes * 60

    def get_timeout_ms(self) -> int:
        """Return the timeout in milliseconds."""
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
