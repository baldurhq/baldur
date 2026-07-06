"""
Tiered Redis Provider - Multi-Cluster Redis Topology.

Provides Redis clients for LOCAL and GLOBAL scopes.

Problem:
- Single Redis creates single-AZ dependency for all clusters
- adapters/resilient/backend.py#L39: redis_url supports only single URL

Design:
- LOCAL: Per-cluster Redis (CB, metrics, DLQ) - high speed
- GLOBAL: Cross-region replicated Redis (config, anchors, Error Budget) - consistency

Reference: docs/baldur/middleware_system/70_MULTI_CLUSTER_ARCHITECTURE.md
"""

from __future__ import annotations

from enum import Enum
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from baldur.settings.tiered_redis import TieredRedisSettings

logger = structlog.get_logger()


class RedisScope(str, Enum):
    """Redis access scope."""

    LOCAL = "local"  # Per-cluster (high speed, real-time)
    GLOBAL = "global"  # Cross-region (config, anchors)


class TieredRedisProvider:
    """
    Tiered Redis provider.

    LOCAL: Per-cluster Redis (CB, metrics, DLQ)
    GLOBAL: Cross-region replicated Redis (config, anchors, Error Budget)

    Usage scenarios:
    - Single Redis (dev/test): LOCAL = GLOBAL = same URL
    - Multi Redis (production): LOCAL != GLOBAL

    Environment variables (via TieredRedisSettings):
    - BALDUR_TIERED_REDIS_LOCAL_URL: Local Redis
    - BALDUR_TIERED_REDIS_LOCAL_PASSWORD: Local password
    - BALDUR_TIERED_REDIS_GLOBAL_URL: Global Redis
    - BALDUR_TIERED_REDIS_GLOBAL_PASSWORD: Global password
    """

    def __init__(
        self,
        settings: TieredRedisSettings | None = None,
    ):
        """
        Initialize Tiered Redis Provider.

        Args:
            settings: TieredRedisSettings (optional, uses singleton if not provided)
        """
        if settings is None:
            from baldur.settings.tiered_redis import get_tiered_redis_settings

            settings = get_tiered_redis_settings()

        self._settings = settings
        self._local_client: Any | None = None
        self._global_client: Any | None = None

        logger.debug(
            "tiered_redis_provider.initialized",
            local_url=self._settings.local_url,
            global_url=self._settings.global_url,
        )

    def get_redis(self, scope: RedisScope = RedisScope.LOCAL) -> Any:
        """
        Return Redis client for the specified scope.

        Args:
            scope: Redis scope (LOCAL or GLOBAL)

        Returns:
            Redis client instance
        """
        if scope == RedisScope.LOCAL:
            return self._get_local_client()
        return self._get_global_client()

    def _get_local_client(self) -> Any:
        """Return local Redis client (lazy initialization)."""
        if self._local_client is None:
            from baldur.adapters.redis.connection_factory import (
                get_redis_connection_factory,
            )

            factory = get_redis_connection_factory()
            kwargs: dict[str, Any] = {}
            if self._settings.local_password:
                kwargs["password"] = self._settings.local_password

            self._local_client = factory.create(self._settings.local_url, **kwargs)
            logger.info(
                "tiered_redis_provider.local_redis_connected",
                local_url=self._settings.local_url,
            )
        return self._local_client

    def _get_global_client(self) -> Any:
        """Return global Redis client (lazy initialization)."""
        if self._global_client is None:
            # Reuse local client if same URL
            if self._settings.global_url == self._settings.local_url:
                self._global_client = self._get_local_client()
                logger.debug("tiered_redis_provider.global_redis_reusing_local")
            else:
                from baldur.adapters.redis.connection_factory import (
                    get_redis_connection_factory,
                )

                factory = get_redis_connection_factory()
                kwargs: dict[str, Any] = {}
                if self._settings.global_password:
                    kwargs["password"] = self._settings.global_password

                # global_url is guaranteed non-None after model_validator
                global_url = self.global_url
                self._global_client = factory.create(global_url, **kwargs)
                logger.info(
                    "tiered_redis_provider.global_redis_connected",
                    global_url=global_url,
                )
        return self._global_client

    @property
    def local_url(self) -> str:
        """Local Redis URL."""
        return self._settings.local_url

    @property
    def global_url(self) -> str:
        """Global Redis URL."""
        # global_url is guaranteed non-None after model_validator
        return self._settings.global_url  # type: ignore[return-value]

    @property
    def is_tiered(self) -> bool:
        """Whether LOCAL and GLOBAL use different Redis instances."""
        return self._settings.local_url != self._settings.global_url

    def close(self) -> None:
        """Close all Redis connections."""
        if self._local_client is not None:
            try:
                self._local_client.close()
            except Exception as e:
                logger.warning(
                    "tiered_redis_provider.error_closing_local_client",
                    error=e,
                )
            self._local_client = None

        # If global is same object as local, already closed — skip close(), only set None
        if self._global_client is not None:
            if self._settings.global_url != self._settings.local_url:
                try:
                    self._global_client.close()
                except Exception as e:
                    logger.warning(
                        "tiered_redis_provider.error_closing_global_client",
                        error=e,
                    )
            self._global_client = None

    def health_check(self, scope: RedisScope | None = None) -> dict:
        """
        Check Redis health status.

        Args:
            scope: Scope to check (None checks all)

        Returns:
            Status dictionary
        """
        result = {}

        if scope is None or scope == RedisScope.LOCAL:
            try:
                self._get_local_client().ping()
                result["local"] = {"status": "healthy", "url": self._settings.local_url}
            except Exception as e:
                result["local"] = {"status": "unhealthy", "error": str(e)}

        if scope is None or scope == RedisScope.GLOBAL:
            try:
                self._get_global_client().ping()
                result["global"] = {
                    "status": "healthy",
                    "url": self.global_url,
                }
            except Exception as e:
                result["global"] = {"status": "unhealthy", "error": str(e)}

        return result


# =============================================================================
# Singleton
# =============================================================================

from baldur.utils.singleton import CLEANUP_CLOSE, make_singleton_factory

(
    get_tiered_redis_provider,
    configure_tiered_redis_provider,
    reset_tiered_redis_provider,
) = make_singleton_factory(
    "tiered_redis_provider", TieredRedisProvider, cleanup_fn=CLEANUP_CLOSE
)
