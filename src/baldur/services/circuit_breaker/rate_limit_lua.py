"""
Redis-backed Rate Limit Backend using Lua Scripts.

Provides cluster-wide visibility for rate limit cascade detection
and self-DDoS protection via Redis ZSET with atomic Lua operations.

Reuses LuaScriptRegistry for evalsha + NOSCRIPT recovery.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any

import structlog

logger = structlog.get_logger()

_counter_lock = threading.Lock()
_counter = 0


def _unique_member() -> str:
    """Generate a unique ZSET member to prevent deduplication at high RPS."""
    global _counter
    with _counter_lock:
        _counter += 1
        seq = _counter
    return f"{time.time()}:{os.getpid()}:{seq}"


__all__ = [
    "RedisRateLimitBackend",
]

LUA_RECORD_AND_COUNT = """
local key = KEYS[1]
local timestamp = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local ttl = tonumber(ARGV[3])
local member = ARGV[4]

redis.call('ZADD', key, timestamp, member)
redis.call('ZREMRANGEBYSCORE', key, '-inf', timestamp - window)
local count = redis.call('ZCARD', key)
redis.call('EXPIRE', key, ttl)
return count
"""

LUA_COUNT_IN_WINDOW = """
local key = KEYS[1]
local cutoff = tonumber(ARGV[1])

redis.call('ZREMRANGEBYSCORE', key, '-inf', cutoff)
return redis.call('ZCARD', key)
"""


class RedisRateLimitBackend:
    """Redis ZSET-based rate limit data store.

    All record operations use a single Lua script (1 RTT) that atomically
    adds a member, prunes expired entries, and returns the current count.
    """

    _KEY_PREFIX = "baldur:rl:"

    _BACKOFF_TTL = 3600

    def __init__(
        self, redis_client: Any, retention_seconds: int = 120, ttl_seconds: int = 180
    ):
        from baldur.audit.performance.lua_registry import LuaScriptRegistry

        self._redis = redis_client
        self._retention = retention_seconds
        self._ttl = ttl_seconds
        self._registry = LuaScriptRegistry(redis_client)
        self._registry.register("rl_record_and_count", LUA_RECORD_AND_COUNT)
        self._registry.register("rl_count_in_window", LUA_COUNT_IN_WINDOW)

    def _key(self, category: str, service_name: str) -> str:
        return f"{self._KEY_PREFIX}{category}:{service_name}"

    def record_rate_limit(self, service_name: str) -> int:
        now = time.time()
        key = self._key("429", service_name)
        return int(
            self._registry.execute(
                "rl_record_and_count",
                keys=[key],
                args=[now, self._retention, self._ttl, _unique_member()],
            )
        )

    def record_request(self, service_name: str) -> int:
        now = time.time()
        key = self._key("req", service_name)
        return int(
            self._registry.execute(
                "rl_record_and_count",
                keys=[key],
                args=[now, self._retention, self._ttl, _unique_member()],
            )
        )

    def get_rate_limit_count(self, service_name: str, window_seconds: int) -> int:
        cutoff = time.time() - window_seconds
        key = self._key("429", service_name)
        return int(
            self._registry.execute(
                "rl_count_in_window",
                keys=[key],
                args=[cutoff],
            )
        )

    def get_request_count(self, service_name: str, window_seconds: int) -> int:
        cutoff = time.time() - window_seconds
        key = self._key("req", service_name)
        return int(
            self._registry.execute(
                "rl_count_in_window",
                keys=[key],
                args=[cutoff],
            )
        )

    def get_backoff_level(self, service_name: str) -> int:
        key = self._key("backoff", service_name)
        val = self._redis.get(key)
        if val is None:
            return 0
        return int(val)

    def increment_backoff(self, service_name: str) -> int:
        key = self._key("backoff", service_name)
        level = int(self._redis.incr(key))
        self._redis.expire(key, self._BACKOFF_TTL)
        return level

    def reset_backoff(self, service_name: str) -> None:
        key = self._key("backoff", service_name)
        self._redis.delete(key)

    def clear_service(self, service_name: str) -> None:
        keys = [
            self._key("429", service_name),
            self._key("req", service_name),
            self._key("backoff", service_name),
        ]
        for key in keys:
            try:
                self._redis.delete(key)
            except Exception:
                pass
