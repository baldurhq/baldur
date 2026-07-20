"""
X-Test Artifact Cleanup Settings

Settings for automatic cleanup of test artifacts (CB state, DLQ entries,
Idempotency keys, etc.) after an X-Test session ends.

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
    X-Test artifact auto-cleanup settings.

    Defines the X-Test session expiration time, the cleanup interval, and
    whether auto-cleanup is enabled per component.
    """

    model_config = make_settings_config("BALDUR_XTEST_CLEANUP_")

    # ==========================================================================
    # Session TTL settings
    # ==========================================================================
    session_ttl_hours: int = Field(
        default=4,
        ge=1,
        le=24,
        description="X-Test session expiration time (hours, accounts for long scenario tests)",
    )

    # ==========================================================================
    # Cleanup interval settings
    # ==========================================================================
    cleanup_interval_minutes: int = Field(
        default=30,
        ge=5,
        le=120,
        description="Auto-cleanup task execution interval (minutes)",
    )

    # ==========================================================================
    # Per-component auto-cleanup toggles
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
    # Celery task retry settings
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
    # Redis key prefixes
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
        """Validate the session TTL."""
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
        """Validate the cleanup interval."""
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
