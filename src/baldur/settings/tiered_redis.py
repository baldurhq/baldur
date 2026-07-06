"""
Tiered Redis Settings - Multi-Cluster Redis Topology.

Manages Redis connection settings for LOCAL and GLOBAL scopes.

Usage:
    # Single Redis (dev/test)
    BALDUR_TIERED_REDIS_LOCAL_URL=redis://localhost:6379/0

    # Multi Redis (production)
    BALDUR_TIERED_REDIS_LOCAL_URL=redis://local-redis:6379/0
    BALDUR_TIERED_REDIS_LOCAL_PASSWORD=local_password
    BALDUR_TIERED_REDIS_GLOBAL_URL=redis://global-redis:6379/0
    BALDUR_TIERED_REDIS_GLOBAL_PASSWORD=global_password
"""

from __future__ import annotations

import structlog
from pydantic import Field, model_validator
from pydantic_settings import BaseSettings

from baldur.settings.base import make_settings_config

logger = structlog.get_logger()

__all__ = [
    "TieredRedisSettings",
    "get_tiered_redis_settings",
    "reset_tiered_redis_settings",
]


class TieredRedisSettings(BaseSettings):
    """
    Tiered Redis connection settings.

    LOCAL: Per-cluster Redis (CB, metrics, DLQ) - high speed
    GLOBAL: Cross-region replicated Redis (config, anchors, Error Budget) - consistency
    """

    model_config = make_settings_config("BALDUR_TIERED_REDIS_")

    local_url: str = Field(
        default="redis://localhost:6379/0",
        description=(
            "Local cluster Redis URL. When unset, falls back to "
            "BALDUR_REDIS_URL (RedisSettings.url); a per-class override "
            "(BALDUR_TIERED_REDIS_LOCAL_URL) wins."
        ),
    )
    local_password: str | None = Field(
        default=None,
        description="Local Redis password (overrides RedisSettings.password)",
    )
    global_url: str | None = Field(
        default=None,
        description="Global Redis URL (defaults to local_url if not set)",
    )
    global_password: str | None = Field(
        default=None,
        description="Global Redis password (overrides RedisSettings.password)",
    )

    @model_validator(mode="after")
    def _validate_tiered_config(self) -> TieredRedisSettings:
        # Resolve local_url to BALDUR_REDIS_URL before the global_url default
        # logic below, so global_url transitively inherits the resolved value.
        from baldur.settings.redis import apply_redis_url_fallback

        apply_redis_url_fallback(self, "local_url")

        # global_url defaults to local_url if not set
        if self.global_url is None:
            object.__setattr__(self, "global_url", self.local_url)

        # Log single-redis mode (normal for dev/test, verify intent in prod)
        if self.local_url == self.global_url:
            logger.info(
                "tiered_redis_settings.single_redis_mode",
                hint="LOCAL and GLOBAL Redis are identical",
            )

        return self


def get_tiered_redis_settings() -> TieredRedisSettings:
    """TieredRedisSettings singleton via RootConfig."""
    from baldur.settings.root import get_config

    return get_config().multi_region.tiered_redis


def reset_tiered_redis_settings() -> None:
    """Reset singleton (for testing)."""
    from baldur.settings.root import get_config

    try:
        del get_config().multi_region.__dict__["tiered_redis"]
    except KeyError:
        pass
