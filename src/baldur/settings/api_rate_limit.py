"""
API Rate Limit Settings - Pydantic v2.

Settings for the Django API rate-limiting middleware's non-limit fields:
Control-API path prefix, Redis health checker, and local-limiter cleanup.

The per-minute limit / window / emergency values are NOT here. They live on the
single canonical ``RateLimitSettings`` surface (``BALDUR_RATE_LIMIT_*``), read
via ``api/django/rate_limit/config.py``, so the limit behaves identically with
and without the PRO RuntimeConfigManager registered.

Environment Variables:
    BALDUR_API_RATE_LIMIT_CONTROL_API_PATH_PREFIX=/api/baldur/
    BALDUR_API_RATE_LIMIT_REDIS_PING_INTERVAL=5
    BALDUR_API_RATE_LIMIT_REDIS_FAILURE_THRESHOLD=3
    BALDUR_API_RATE_LIMIT_REDIS_RECOVERY_JITTER_MAX=10
    BALDUR_API_RATE_LIMIT_REDIS_PING_TIMEOUT_MS=100
    BALDUR_API_RATE_LIMIT_LOCAL_CLEANUP_INTERVAL=60
"""

from pydantic import Field
from pydantic_settings import BaseSettings

from baldur.settings.base import make_settings_config
from baldur.settings.field_types import (
    ShortInterval,
    SmallCount,
)


class ApiRateLimitSettings(BaseSettings):
    """
    API rate-limiting settings for the Django middleware (non-limit fields).

    The limit / window / emergency values are owned by ``RateLimitSettings``
    (``BALDUR_RATE_LIMIT_*``). This class owns the surrounding middleware
    configuration:

    Control API Path:
        - control_api_path_prefix: path prefix where rate limiting applies

    Redis Health Checker:
        - redis_ping_interval: health-check interval (seconds)
        - redis_failure_threshold: consecutive failures to mark UNHEALTHY
        - redis_recovery_jitter_max: max jitter to prevent thundering herd (s)
        - redis_ping_timeout_ms: health-check ping timeout (milliseconds)

    Local Memory Limiter:
        - local_cleanup_interval: L1 limiter cleanup interval (seconds)
    """

    model_config = make_settings_config("BALDUR_API_RATE_LIMIT_")

    # =========================================================================
    # Control API Path Configuration
    # =========================================================================
    control_api_path_prefix: str = Field(
        default="/api/baldur/",
        description="API path prefix where rate limiting is applied",
    )

    # =========================================================================
    # Redis Health Checker Settings
    # =========================================================================
    redis_ping_interval: ShortInterval = Field(
        default=5,
        description="Redis health check interval (seconds)",
    )
    redis_failure_threshold: SmallCount = Field(
        default=3,
        description="Consecutive failure count to transition to UNHEALTHY state",
    )
    redis_recovery_jitter_max: ShortInterval = Field(
        default=10,
        description="Maximum jitter to prevent thundering herd on recovery (seconds)",
    )
    redis_ping_timeout_ms: int = Field(
        default=100,
        ge=10,
        le=1000,
        description="Health check ping timeout in milliseconds (dedicated low-timeout client)",
    )

    # =========================================================================
    # Local Memory Limiter Settings
    # =========================================================================
    local_cleanup_interval: int = Field(
        default=60,
        ge=10,
        le=300,
        description="Local memory rate limiter cleanup interval (seconds)",
    )


def get_api_rate_limit_settings() -> "ApiRateLimitSettings":
    from baldur.settings.root import get_config

    return get_config().services_group.api_rate_limit


def reset_api_rate_limit_settings() -> None:
    from baldur.settings.root import get_config

    try:
        del get_config().services_group.__dict__["api_rate_limit"]
    except KeyError:
        pass
