"""
Pre-computed Cache Service - L2 Redis Cache.

Pre-computed JSON storage in Redis for low-latency access.
"""

from __future__ import annotations

import structlog

from .constants import _get_l2_ttl_seconds

logger = structlog.get_logger()


# =============================================================================
# L2 Redis Cache
# =============================================================================


class L2RedisCache:
    """
    L2 Redis Cache for pre-computed JSON.

    Stores pre-serialized JSON strings to eliminate serialization overhead
    on read path.
    """

    def __init__(self):
        self._redis = None
        self._initialized = False

    def _get_redis(self):
        """Lazy load cache provider."""
        if self._redis is None:
            try:
                from baldur.factory import ProviderRegistry

                self._redis = ProviderRegistry.get_cache()
                self._initialized = True
            except Exception as e:
                logger.warning(
                    "precomputed_cache.cache_unavailable",
                    error=e,
                )
                self._redis = None
        return self._redis

    def get(self, key: str) -> str | None:
        """Get pre-computed JSON string from Redis."""
        try:
            redis = self._get_redis()
            if redis:
                value = redis.get(key)
                if isinstance(value, bytes):
                    return value.decode("utf-8")
                return value
        except Exception as e:
            logger.debug(
                "precomputed_cache.redis_get_failed",
                error=e,
            )
        return None

    def set(self, key: str, value: str, ttl: float | None = None) -> bool:
        """Set pre-computed JSON string in Redis."""
        if ttl is None:
            ttl = _get_l2_ttl_seconds()
        try:
            redis = self._get_redis()
            if redis:
                from datetime import timedelta

                redis.set(key, value, ttl=timedelta(seconds=int(ttl)))
                return True
        except Exception as e:
            logger.debug(
                "precomputed_cache.redis_set_failed",
                error=e,
            )
        return False

    def is_available(self) -> bool:
        """Check if Redis is available."""
        try:
            redis = self._get_redis()
            return redis is not None
        except Exception:
            return False


# Global L2 cache instance
_l2_cache = L2RedisCache()


def get_l2_cache() -> L2RedisCache:
    """Get the global L2 Redis cache instance."""
    return _l2_cache


def reset_l2_cache() -> None:
    """Reset the global L2 Redis cache instance to a fresh L2RedisCache."""
    global _l2_cache

    _l2_cache = L2RedisCache()
