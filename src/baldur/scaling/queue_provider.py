"""
Queue size provider (with caching).

Prevents the network latency of Redis queue lookups from becoming a
RateController bottleneck.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

import structlog

from baldur.scaling.config import (
    BackpressureSettings,
    get_backpressure_settings,
)

logger = structlog.get_logger()


class CachedQueueSizeProvider:
    """
    Queue size caching provider.

    Avoids frequent network calls when querying an external queue such as
    Redis. TTL-based caching bounds the lookup frequency.

    Usage:
        def get_redis_queue_size() -> int:
            return redis.llen("my_queue")

        provider = CachedQueueSizeProvider(get_redis_queue_size, cache_ttl=2.0)

        # A repeat call within 2 seconds returns the cached value
        size = provider()
    """

    def __init__(
        self,
        provider: Callable[[], int],
        cache_ttl: float | None = None,
        settings: BackpressureSettings | None = None,
    ):
        """
        Args:
            provider: Actual queue size lookup function
            cache_ttl: Cache TTL (seconds). Loaded from settings when None.
            settings: Backpressure settings
        """
        self._provider = provider
        self._settings = settings or get_backpressure_settings()
        self._cache_ttl = cache_ttl or self._settings.queue_size_cache_ttl_seconds

        self._cached_value = 0
        self._last_fetch_time = 0.0
        self._lock = threading.Lock()

    def __call__(self) -> int:
        """
        Return the queue size (cached).

        A repeat call within the TTL returns the cached value.
        Past the TTL, the real lookup runs and refreshes the cache.

        Returns:
            Queue size
        """
        now = time.time()

        with self._lock:
            if now - self._last_fetch_time > self._cache_ttl:
                try:
                    self._cached_value = self._provider()
                    self._last_fetch_time = now
                except Exception as e:
                    logger.warning(
                        "cached_queue_size_provider.fetch_failed_using_cached",
                        error=e,
                    )
                    # On failure, keep the existing cached value

            return self._cached_value

    def invalidate(self) -> None:
        """Invalidate the cache. The next call looks up immediately."""
        with self._lock:
            self._last_fetch_time = 0.0

    def get_cache_info(self) -> dict:
        """
        Return cache information.

        Returns:
            Cache state information
        """
        with self._lock:
            return {
                "cached_value": self._cached_value,
                "last_fetch_time": self._last_fetch_time,
                "cache_ttl": self._cache_ttl,
                "age_seconds": time.time() - self._last_fetch_time,
            }
