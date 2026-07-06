"""
Metrics-aware cache adapter decorator.

Wraps any CacheProviderInterface to add uniform drift metrics
collection, decoupling observability from cache business logic.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from baldur.interfaces.cache_provider import (
    CacheProviderInterface,
    DistributedLock,
)

try:
    from baldur.metrics.drift_metrics import (
        record_cache_get,
        record_cache_set,
    )

    HAS_DRIFT_METRICS = True
except ImportError:
    HAS_DRIFT_METRICS = False


class MetricsAwareCacheAdapter(CacheProviderInterface):
    """Decorator that adds drift metrics to any CacheProviderInterface."""

    def __init__(self, delegate: CacheProviderInterface) -> None:
        self._delegate = delegate
        self._backend_name = type(delegate).__name__

    @property
    def provider_name(self) -> str:
        return self._delegate.provider_name

    # =========================================================================
    # Basic Operations (with metrics)
    # =========================================================================

    def get(self, key: str) -> Any | None:
        result = self._delegate.get(key)
        if HAS_DRIFT_METRICS:
            record_cache_get(
                self._backend_name, "hit" if result is not None else "miss"
            )
        return result

    def set(self, key: str, value: Any, ttl: timedelta | None = None) -> bool:
        result = self._delegate.set(key, value, ttl)
        if HAS_DRIFT_METRICS:
            record_cache_set(self._backend_name)
        return result

    def delete(self, key: str) -> bool:
        return self._delegate.delete(key)

    def exists(self, key: str) -> bool:
        return self._delegate.exists(key)

    # =========================================================================
    # Atomic Operations (delegate only)
    # =========================================================================

    def incr(self, key: str, amount: int = 1) -> int:
        return self._delegate.incr(key, amount)

    def decr(self, key: str, amount: int = 1) -> int:
        return self._delegate.decr(key, amount)

    def expire(self, key: str, ttl: timedelta) -> bool:
        return self._delegate.expire(key, ttl)

    def ttl(self, key: str) -> int | None:
        return self._delegate.ttl(key)

    def setnx(self, key: str, value: Any, ttl: timedelta | None = None) -> bool:
        return self._delegate.setnx(key, value, ttl)

    def cas_dict_field(
        self,
        key: str,
        field: str,
        expected: Any,
        new_value: dict[str, Any],
        ttl: timedelta | None = None,
    ) -> bool:
        return self._delegate.cas_dict_field(key, field, expected, new_value, ttl)

    def cas_takeover(
        self,
        key: str,
        new_record: dict[str, Any],
        *,
        stale_before: float,
        ttl: timedelta | None = None,
    ) -> bool:
        # Must delegate (not inherit the base non-atomic default): the gate holds
        # the wrapped adapter, so without this it would resolve to the base
        # get→check→set at call time — silently re-opening the takeover race —
        # even though the validator unwraps to the atomic concrete adapter.
        return self._delegate.cas_takeover(
            key, new_record, stale_before=stale_before, ttl=ttl
        )

    # =========================================================================
    # Distributed Locking (delegate only)
    # =========================================================================

    def get_lock(
        self,
        name: str,
        timeout: timedelta = timedelta(seconds=10),
        blocking_timeout: float | None = None,
    ) -> DistributedLock:
        return self._delegate.get_lock(name, timeout, blocking_timeout)

    # =========================================================================
    # Bulk Operations (delegate only)
    # =========================================================================

    def mget(self, keys: list[str]) -> dict[str, Any]:
        return self._delegate.mget(keys)

    def mset(self, mapping: dict[str, Any], ttl: timedelta | None = None) -> bool:
        return self._delegate.mset(mapping, ttl)

    def mdelete(self, keys: list[str]) -> int:
        return self._delegate.mdelete(keys)

    # =========================================================================
    # Hash Operations (delegate only)
    # =========================================================================

    def hget(self, name: str, key: str) -> Any | None:
        return self._delegate.hget(name, key)

    def hset(self, name: str, key: str, value: Any) -> bool:
        return self._delegate.hset(name, key, value)

    def hgetall(self, name: str) -> dict[str, Any]:
        return self._delegate.hgetall(name)

    # =========================================================================
    # List Operations (delegate only)
    # =========================================================================

    def push_limit(
        self, key: str, value: Any, max_len: int, ttl: timedelta | None = None
    ) -> int:
        return self._delegate.push_limit(key, value, max_len, ttl)

    def list_range(self, key: str, start: int, end: int) -> list[Any]:
        return self._delegate.list_range(key, start, end)

    # =========================================================================
    # Health Check & Recovery (delegate only)
    # =========================================================================

    def health_check(self) -> bool:
        return self._delegate.health_check()

    def reconnect(self) -> bool:
        if hasattr(self._delegate, "reconnect"):
            return self._delegate.reconnect()
        return False

    def flush_all(self) -> bool:
        return self._delegate.flush_all()

    # =========================================================================
    # Key Pattern Operations (delegate only)
    # =========================================================================

    def keys(self, pattern: str = "*") -> list[str]:
        return self._delegate.keys(pattern)

    def scan(self, pattern: str = "*", count: int = 100) -> tuple[int, list[str]]:
        return self._delegate.scan(pattern, count)
