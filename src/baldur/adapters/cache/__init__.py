"""
Cache provider adapters for the baldur system.

This module contains concrete implementations of CacheProviderInterface
for different cache backends.

Available Adapters:
    - RedisCacheAdapter: Redis-based caching with distributed locks
    - InMemoryCacheAdapter: In-memory caching for testing
    - AsyncRedisCacheAdapter: awaitable dedup ops over redis.asyncio
    - AsyncInMemoryCacheAdapter: awaitable dedup ops / async test double

Status: Public
"""

from baldur.adapters.cache.async_memory_adapter import (
    AsyncInMemoryCacheAdapter,
)
from baldur.adapters.cache.async_redis_adapter import (
    AsyncRedisCacheAdapter,
)
from baldur.adapters.cache.memory_adapter import (
    InMemoryCacheAdapter,
)
from baldur.adapters.cache.metrics_decorator import (
    MetricsAwareCacheAdapter,
)
from baldur.adapters.cache.redis_adapter import (
    RedisCacheAdapter,
)

__all__ = [
    "RedisCacheAdapter",
    "InMemoryCacheAdapter",
    "MetricsAwareCacheAdapter",
    "AsyncRedisCacheAdapter",
    "AsyncInMemoryCacheAdapter",
]
