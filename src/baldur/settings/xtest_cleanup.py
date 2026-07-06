"""
X-Test Artifact Cleanup Settings

X-Test 세션 종료 후 테스트 아티팩트(CB 상태, DLQ 항목, Idempotency 키 등) 자동 정리 설정.

Environment Variables:
    BALDUR_XTEST_CLEANUP_SESSION_TTL_HOURS=4
    BALDUR_XTEST_CLEANUP_INTERVAL_MINUTES=30
    BALDUR_XTEST_CLEANUP_CB_AUTO_RESTORE=true
    BALDUR_XTEST_CLEANUP_DLQ_AUTO_PURGE=true
    BALDUR_XTEST_CLEANUP_IDEMPOTENCY_AUTO_CLEAR=true
    BALDUR_XTEST_CLEANUP_RATE_LIMIT_AUTO_RESET=true
    BALDUR_XTEST_CLEANUP_MAX_RETRIES=2
    BALDUR_XTEST_CLEANUP_RETRY_DELAY=60
"""

from __future__ import annotations

import structlog
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings

from baldur.settings.base import make_settings_config

logger = structlog.get_logger()


class XTestCleanupSettings(BaseSettings):
    """
    X-Test 아티팩트 자동 정리 설정.

    X-Test 세션 만료 시간, 정리 주기, 컴포넌트별 자동 정리 활성화 여부를 정의합니다.
    """

    model_config = make_settings_config("BALDUR_XTEST_CLEANUP_")

    # ==========================================================================
    # 세션 TTL 설정
    # ==========================================================================
    session_ttl_hours: int = Field(
        default=4,
        ge=1,
        le=24,
        description="X-Test session expiration time (hours, accounts for long scenario tests)",
    )

    # ==========================================================================
    # 정리 주기 설정
    # ==========================================================================
    cleanup_interval_minutes: int = Field(
        default=30,
        ge=5,
        le=120,
        description="Auto-cleanup task execution interval (minutes)",
    )

    # ==========================================================================
    # 컴포넌트별 자동 정리 활성화
    # ==========================================================================
    cb_auto_restore: bool = Field(
        default=True,
        description="Enable automatic Circuit Breaker state restoration",
    )

    dlq_auto_purge: bool = Field(
        default=True,
        description="Enable automatic DLQ X-Test entry purging",
    )

    idempotency_auto_clear: bool = Field(
        default=True,
        description="Enable automatic Idempotency key clearing",
    )

    rate_limit_auto_reset: bool = Field(
        default=True,
        description="Enable automatic Rate Limit counter reset",
    )

    # ==========================================================================
    # Celery Task 재시도 설정
    # ==========================================================================
    max_retries: int = Field(
        default=2,
        ge=0,
        le=5,
        description="Maximum retry count for cleanup tasks",
    )

    retry_delay: int = Field(
        default=60,
        ge=10,
        le=600,
        description="Cleanup task retry delay (seconds)",
    )

    # ==========================================================================
    # Redis 키 접두사
    # ==========================================================================
    redis_session_prefix: str = Field(
        default="xtest:session:",
        description="Redis key prefix for X-Test session metadata",
    )

    redis_active_sessions_key: str = Field(
        default="xtest:session:active",
        description="Redis key for active X-Test session ID list",
    )

    @field_validator("session_ttl_hours")
    @classmethod
    def validate_session_ttl(cls, v: int) -> int:
        """세션 TTL 검증."""
        if v < 1:
            logger.warning(
                "x_test_cleanup.too_low_using",
                setting_value=v,
            )
            return 1
        return v

    @field_validator("cleanup_interval_minutes")
    @classmethod
    def validate_cleanup_interval(cls, v: int) -> int:
        """정리 주기 검증."""
        if v < 5:
            logger.warning(
                "x_test_cleanup.too_low_using",
                setting_value=v,
            )
            return 5
        return v


# =============================================================================
# Settings Instance Factory
# =============================================================================


def get_xtest_cleanup_settings() -> XTestCleanupSettings:
    from baldur.settings.root import get_config

    return get_config().testing.xtest_cleanup


__all__ = [
    "XTestCleanupSettings",
    "get_xtest_cleanup_settings",
    "reset_xtest_cleanup_settings",
]


def reset_xtest_cleanup_settings() -> None:
    from baldur.settings.root import get_config

    try:
        del get_config().testing.__dict__["xtest_cleanup"]
    except KeyError:
        pass
