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

__all__ = [
    "DEFAULT_REDIS_URL",
    "REDIS_INTENT_ENV_VARS",
    "REDIS_URL_ENV_VARS",
    "RedisSettings",
    "get_redis_settings",
    "redis_explicitly_configured",
    "reset_redis_settings",
]

# The address the framework dials when nobody named one. Referenced by every
# settings class that carries a Redis URL, so "the default" has one spelling.
DEFAULT_REDIS_URL = "redis://localhost:6379/0"

# Client-acquisition priority order: the documented canonical variable first,
# the bare backward-compat one second. ``_acquire_from_env`` iterates this
# tuple, which is what keeps the acquisition path and
# :func:`redis_explicitly_configured` from disagreeing about what counts as a
# Redis URL — a new source is added here once, not in two places.
REDIS_URL_ENV_VARS: tuple[str, ...] = ("BALDUR_REDIS_URL", "REDIS_URL")

# Everything that expresses Redis intent. Adds the feature-local override,
# which is not a client-acquisition source but is unmistakably an operator
# naming a Redis.
REDIS_INTENT_ENV_VARS: tuple[str, ...] = (
    *REDIS_URL_ENV_VARS,
    "BALDUR_RESILIENT_STORAGE_REDIS_URL",
)


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
        default=DEFAULT_REDIS_URL,
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


def redis_explicitly_configured() -> bool:
    """Report whether anyone named a Redis for this process.

    True when Redis intent was expressed through any documented channel;
    False when the framework would be dialing its own default address that
    nobody asked for. Components use it to tell "optional dependency never
    configured" (an expected posture) apart from "configured and broken" (an
    operational fault that must stay loud).

    The channels mirror the live client-acquisition strategies rather than
    restating them: the environment variables come from
    :data:`REDIS_INTENT_ENV_VARS`, and the two Django-shaped probes are the
    same two strategies ``baldur.adapters.redis`` runs.

    Side-effect-free: neither Django probe imports Django into a process that
    has not already loaded it. Each probe is wrapped independently, and the
    function never raises — a failed probe returns False, which is accurate
    rather than merely safe, since a framework that cannot be imported cannot
    serve that acquisition strategy either.
    """
    for name in REDIS_INTENT_ENV_VARS:
        if os.environ.get(name, "").strip():
            return True

    if _django_settings_name_a_redis():
        return True

    return _django_redis_cache_configured()


def _django_settings_name_a_redis() -> bool:
    """Django settings carry a truthy ``BALDUR_REDIS_URL`` attribute.

    Reached when Django is plausibly in play — an environment variable names
    a settings module, or ``django.conf`` is already imported, which covers
    ``settings.configure(...)`` called programmatically with no env var.
    """
    import sys

    if "DJANGO_SETTINGS_MODULE" not in os.environ and "django.conf" not in sys.modules:
        return False
    try:
        from django.conf import settings as django_settings

        return bool(getattr(django_settings, "BALDUR_REDIS_URL", None))
    except Exception:
        return False


def _django_redis_cache_configured() -> bool:
    """A ``CACHES`` entry names a django_redis backend."""
    try:
        import django_redis  # noqa: F401
        from django.conf import settings as django_settings

        caches = getattr(django_settings, "CACHES", None) or {}
        return any(
            "django_redis" in str(config.get("BACKEND", ""))
            for config in caches.values()
        )
    except Exception:
        return False


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
