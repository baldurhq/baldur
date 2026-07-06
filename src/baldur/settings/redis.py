"""
Redis Connection Settings - Pydantic v2.

Settings used by RedisConnectionFactory for Standalone/Sentinel/Cluster routing.

Environment Variables:
    BALDUR_REDIS_URL=redis://localhost:6379/0
    BALDUR_REDIS_PASSWORD=<secret>
    BALDUR_REDIS_SENTINEL_PASSWORD=<secret>
    BALDUR_REDIS_USERNAME=<acl_user>
    BALDUR_REDIS_SOCKET_TIMEOUT=5.0
    BALDUR_REDIS_SOCKET_CONNECT_TIMEOUT=5.0
    BALDUR_REDIS_RETRY_ON_TIMEOUT=true
    BALDUR_REDIS_MAX_CONNECTIONS=100
    BALDUR_REDIS_HEALTH_CHECK_INTERVAL=30

Related:
    - adapters/redis/connection_factory.py: RedisConnectionFactory
    - settings/pool_monitor.py: PoolMonitorSettings (runtime pool monitoring)
    - core/pool_watchdog.py: PoolWatchdog (automatic pool recovery)
"""

from __future__ import annotations

import os

import structlog
from pydantic import Field
from pydantic_settings import BaseSettings

from baldur.settings.base import make_settings_config
from baldur.settings.field_types import HugeCount, ShortDuration

logger = structlog.get_logger()

__all__ = ["RedisSettings", "get_redis_settings", "reset_redis_settings"]


class RedisSettings(BaseSettings):
    """
    Redis connection settings.

    URL carries routing info only (scheme, host, port, master name, db).
    Auth credentials are separated into dedicated fields for security,
    Sentinel dual-auth, and Redis 6.0+ ACL support.

    URL scheme conventions:
        - redis:// / rediss:// → Standalone (existing behavior)
        - redis+sentinel://master@host1:port,host2:port/db → Sentinel
        - redis+cluster://host1:port,host2:port → Cluster
    """

    model_config = make_settings_config("BALDUR_REDIS_")

    # ==========================================================================
    # Connection URL (routing info only, no password)
    # ==========================================================================
    url: str = Field(
        default="redis://localhost:6379/0",
        description="Redis connection URL (routing info only, no password in URL)",
    )

    # ==========================================================================
    # Authentication (separated from URL for security)
    # ==========================================================================
    password: str | None = Field(
        default=None,
        description="Redis instance password (Master password for Sentinel)",
    )
    sentinel_password: str | None = Field(
        default=None,
        description="Sentinel node password (Sentinel-only, separate from Master)",
    )
    username: str | None = Field(
        default=None,
        description="Redis ACL username (Redis 6.0+)",
    )

    # ==========================================================================
    # Connection Parameters
    # ==========================================================================
    socket_timeout: ShortDuration = Field(
        default=5.0,
        description="Socket timeout in seconds",
    )
    socket_connect_timeout: ShortDuration = Field(
        default=5.0,
        description="Socket connection timeout in seconds",
    )
    probe_connect_timeout: ShortDuration = Field(
        default=0.5,
        description=(
            "Fast-fail connect timeout (seconds) for lazy Redis liveness probes. "
            "Deliberately shorter than socket_connect_timeout so an unreachable "
            "Redis degrades the probe path quickly instead of blocking."
        ),
    )
    retry_on_timeout: bool = Field(
        default=True,
        description="Retry on timeout errors",
    )

    # ==========================================================================
    # Connection Pool
    # ==========================================================================
    max_connections: HugeCount = Field(
        default=100,
        description="Connection pool max connections per client",
    )

    # ==========================================================================
    # Health Check
    # ==========================================================================
    health_check_interval: int = Field(
        default=30,
        ge=5,
        le=300,
        description="Sentinel/Cluster health check interval in seconds",
    )

    @property
    def is_tls_enabled(self) -> bool:
        """Check if TLS is enabled based on URL scheme.

        Covers both ``rediss://`` (standalone) and ``rediss+sentinel://`` variants.
        """
        return self.url.startswith("rediss")


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------


def get_redis_settings() -> RedisSettings:
    """Return singleton RedisSettings instance."""
    from baldur.settings.root import get_config

    return get_config().adapters.redis


def reset_redis_settings() -> None:
    """Reset singleton (for testing)."""
    from baldur.settings.root import get_config

    try:
        del get_config().adapters.__dict__["redis"]
    except KeyError:
        pass


def apply_redis_url_fallback(model: BaseSettings, field_name: str) -> None:
    """Resolve ``field_name`` to BALDUR_REDIS_URL when not explicitly set.

    Shared by Redis-backed settings classes that want the project-wide
    ``BALDUR_REDIS_URL`` (``RedisSettings.url``) as the fallback for a
    feature-local URL field. ``model_fields_set`` membership means a
    per-feature override (env var or kwarg) was supplied — that wins and
    the helper no-ops. Intentionally kept out of ``__all__``: this is a
    settings-internal building block, not part of the public API.

    Fail-safe: if ``get_redis_settings()`` raises, the field keeps its
    prior value and a WARNING is emitted (no exception propagates out of
    the calling validator). An empty resolved URL (``BALDUR_REDIS_URL=""``)
    is also left as the field default — ``object.__setattr__`` bypasses
    Pydantic validation, so an empty string would otherwise slip past a
    consumer's ``min_length=1``.

    Callers use a ``model_validator(mode="after")`` and MUST still
    ``return self`` (this helper returns ``None``).
    """
    # Pattern source: settings/leader_election.py::_fallback_redis_url.
    if field_name in model.model_fields_set:
        return
    try:
        resolved = get_redis_settings().url
    except Exception as e:
        logger.warning(
            "settings.redis_url_fallback_failed",
            field=field_name,
            error=str(e),
        )
        return
    if not resolved:
        return
    object.__setattr__(model, field_name, resolved)
    logger.debug(
        "settings.redis_url_resolved",
        field=field_name,
        redis_url=resolved,
        source="BALDUR_REDIS_URL" if os.environ.get("BALDUR_REDIS_URL") else "default",
    )
