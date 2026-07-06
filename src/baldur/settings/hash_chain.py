"""
Hash Chain Settings - Pydantic v2.

해시 체인 무결성 관련 분산 락 및 감사 추적 설정입니다.

Source:
- audit/hash_chain_safety.py:AtomicMergeSwap.DEFAULT_TIMEOUT_SECONDS (300초)
- audit/hash_chain_safety.py:ShardedDateLock.DEFAULT_TIMEOUT_SECONDS (120초)
- audit/hash_chain_safety.py:IntegrityAuditTrail.MAX_REDIS_ENTRIES (1000개)

Environment Variables:
    BALDUR_HASH_CHAIN_MERGE_SWAP_TIMEOUT_SECONDS=300
    BALDUR_HASH_CHAIN_DATE_LOCK_TIMEOUT_SECONDS=120
    BALDUR_HASH_CHAIN_INTEGRITY_TRAIL_MAX_REDIS_ENTRIES=1000
    BALDUR_HASH_CHAIN_DATE_LOCK_BLOCKING_TIMEOUT_SECONDS=5.0
"""

import structlog
from pydantic import Field, model_validator
from pydantic_settings import BaseSettings

from baldur.settings.base import make_settings_config

logger = structlog.get_logger()


class HashChainSettings(BaseSettings):
    """
    해시 체인 무결성 관련 설정.

    분산 락:
    - merge_swap_timeout_seconds: 전역 조정 락 타임아웃 (5분)
    - date_lock_timeout_seconds: 날짜별 락 타임아웃 (2분)

    감사 추적:
    - integrity_trail_max_redis_entries: Redis 저장 무결성 이벤트 최대 수
    """

    model_config = make_settings_config("BALDUR_HASH_CHAIN_")

    # ==========================================================================
    # AtomicMergeSwap - 전역 조정 락 (from hash_chain_safety.py)
    # ==========================================================================
    merge_swap_timeout_seconds: int = Field(
        default=300,
        ge=60,
        le=600,
        description="AtomicMergeSwap global lock auto-expiry time (seconds). Default 5 minutes.",
    )

    merge_swap_blocking_timeout_seconds: float = Field(
        default=10.0,
        ge=1.0,
        le=60.0,
        description="AtomicMergeSwap maximum lock acquisition wait time (seconds).",
    )

    # ==========================================================================
    # ShardedDateLock - 날짜별 분산 락 (from hash_chain_safety.py)
    # ==========================================================================
    date_lock_timeout_seconds: int = Field(
        default=120,
        ge=30,
        le=300,
        description="ShardedDateLock per-date lock auto-expiry time (seconds). Default 2 minutes.",
    )

    date_lock_blocking_timeout_seconds: float = Field(
        default=5.0,
        ge=0.5,
        le=30.0,
        description="ShardedDateLock maximum lock acquisition wait time (seconds). Keep short for fast failover to other dates.",
    )

    # ==========================================================================
    # IntegrityAuditTrail - 무결성 감사 추적 (from hash_chain_safety.py)
    # ==========================================================================
    integrity_trail_max_redis_entries: int = Field(
        default=1000,
        ge=100,
        le=10000,
        description="Maximum integrity audit events stored in Redis. Limited via FIFO eviction.",
    )

    @model_validator(mode="after")
    def validate_lock_timeouts(self) -> "HashChainSettings":
        """락 타임아웃이 블로킹 타임아웃보다 커야 함."""
        if self.merge_swap_timeout_seconds <= self.merge_swap_blocking_timeout_seconds:
            logger.warning(
                "hash_chain_settings.greater_than",
                merge_swap_timeout_seconds=self.merge_swap_timeout_seconds,
                merge_swap_blocking_timeout=self.merge_swap_blocking_timeout_seconds,
            )
        if self.date_lock_timeout_seconds <= self.date_lock_blocking_timeout_seconds:
            logger.warning(
                "hash_chain_settings.greater_than",
                date_lock_timeout_seconds=self.date_lock_timeout_seconds,
                date_lock_blocking_timeout=self.date_lock_blocking_timeout_seconds,
            )
        return self


def get_hash_chain_settings() -> "HashChainSettings":
    from baldur.settings.root import get_config

    return get_config().audit_group.hash_chain


def reset_hash_chain_settings() -> None:
    from baldur.settings.root import get_config

    try:
        del get_config().audit_group.__dict__["hash_chain"]
    except KeyError:
        pass
