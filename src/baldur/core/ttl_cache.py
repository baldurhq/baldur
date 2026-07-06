"""Generic TTL Cache Base — thread-safe, jitter-optional, stats-capable."""

from __future__ import annotations

import random
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Generic, TypeVar

from baldur.core.singleflight import Singleflight

K = TypeVar("K")
V = TypeVar("V")

__all__ = ["TTLCacheBase", "CacheStats"]


@dataclass
class CacheStats:
    """Cache statistics for TTL cache operations."""

    hits: int = 0
    misses: int = 0
    invalidations: int = 0
    expirations: int = 0

    @property
    def hit_rate(self) -> float:
        """Cache hit rate (0.0-1.0)."""
        total = self.hits + self.misses
        return self.hits / total if total > 0 else 0.0


class _CacheEntry(Generic[V]):
    """Internal cache entry with expiration timestamp."""

    __slots__ = ("value", "expires_at")

    def __init__(self, value: V, expires_at: float) -> None:
        self.value = value
        self.expires_at = expires_at


class TTLCacheBase(Generic[K, V]):
    """
    Reusable TTL cache with optional jitter and statistics.

    Features:
    - Thread-safe (RLock)
    - Optional jitter for thundering herd prevention
    - Configurable max size with LRU eviction
    - Cache statistics (hits, misses, expirations)
    - Bulk invalidation
    """

    def __init__(
        self,
        ttl_seconds: float,
        jitter_range: float = 0.0,
        max_size: int = 0,  # 0 = unlimited
    ) -> None:
        self._ttl_seconds = ttl_seconds
        self._jitter_range = jitter_range
        self._max_size = max_size
        self._cache: OrderedDict[K, _CacheEntry[V]] = OrderedDict()
        self._lock = threading.RLock()
        self._stats = CacheStats()
        self._singleflight: Singleflight[V | None] = Singleflight()

    def get(self, key: K) -> V | None:
        """Get value from cache. Returns None on miss or expiration."""
        return self._lookup(key)

    def _lookup(self, key: K) -> V | None:
        """Core lookup shared by get() and get_or_compute().

        Kept separate from the public get() because subclasses may
        override get() with a different return shape (e.g.,
        IPCStateCache returns a (value, hit) tuple); get_or_compute()
        must observe raw hit/miss semantics regardless.
        """
        with self._lock:
            entry = self._cache.get(key)

            if entry is None:
                self._stats.misses += 1
                return None

            if time.time() >= entry.expires_at:
                del self._cache[key]
                self._stats.misses += 1
                self._stats.expirations += 1
                return None

            # LRU: move to end on access
            if self._max_size > 0:
                self._cache.move_to_end(key)

            self._stats.hits += 1
            return entry.value

    def get_or_compute(
        self,
        key: K,
        fn: Callable[[], V | None],
        ttl_override: float | None = None,
    ) -> V | None:
        """Get a cached value, computing it on miss with in-flight dedup.

        Concurrent same-key misses execute ``fn`` exactly once per
        process (Singleflight); the other callers block and share the
        winner's value - or its exception.

        Semantics:
        - Cache hit: returned immediately (no Singleflight cost).
        - ``fn`` returning None: returned but NOT cached (None is this
          cache's miss sentinel). False IS a legitimate cached value.
        - ``fn`` raising: nothing cached; the exception propagates to
          the winner and all current waiters.
        - ``fn`` runs with no instance locks held.

        Args:
            key: Cache key.
            fn: Zero-arg compute callable, executed only by the winner.
            ttl_override: Optional TTL for the stored value (seconds).

        Returns:
            Cached or freshly computed value, or None if ``fn``
            returned None.
        """
        value = self._lookup(key)
        if value is not None:
            return value

        def _compute_and_store() -> V | None:
            # Double-check: a previous winner may have filled the key
            # between this caller's miss and its winner registration.
            cached = self._lookup(key)
            if cached is not None:
                return cached
            result = fn()
            if result is not None:
                self.set(key, result, ttl_override)
            return result

        return self._singleflight.run(key, _compute_and_store)

    def set(self, key: K, value: V, ttl_override: float | None = None) -> None:
        """Set value in cache with optional TTL override."""
        with self._lock:
            ttl = ttl_override if ttl_override is not None else self._calculate_ttl()
            expires_at = time.time() + ttl

            # Evict LRU if at capacity and key is new
            if (
                self._max_size > 0
                and key not in self._cache
                and len(self._cache) >= self._max_size
            ):
                self._evict_lru()

            self._cache[key] = _CacheEntry(value=value, expires_at=expires_at)

            # Move to end for LRU tracking
            if self._max_size > 0:
                self._cache.move_to_end(key)

    def invalidate(self, key: K) -> bool:
        """Invalidate a single cache entry. Returns True if entry existed."""
        with self._lock:
            if key in self._cache:
                del self._cache[key]
                self._stats.invalidations += 1
                return True
            return False

    def invalidate_all(self) -> int:
        """Invalidate all cache entries. Returns count of invalidated entries."""
        with self._lock:
            count = len(self._cache)
            self._cache.clear()
            self._stats.invalidations += count
            return count

    def get_stats(self) -> CacheStats:
        """Get cache statistics."""
        return self._stats

    @property
    def size(self) -> int:
        """Current number of entries in cache."""
        with self._lock:
            return len(self._cache)

    def _calculate_ttl(self) -> float:
        """Calculate TTL with optional jitter."""
        if self._jitter_range > 0:
            jitter = random.uniform(-self._jitter_range, self._jitter_range)
            return self._ttl_seconds + jitter
        return self._ttl_seconds

    def _evict_lru(self) -> None:
        """Evict the least recently used entry."""
        if self._cache:
            self._cache.popitem(last=False)

    def _cleanup_expired(self) -> int:
        """Remove all expired entries. Returns count of removed entries."""
        now = time.time()
        expired = [key for key, entry in self._cache.items() if now >= entry.expires_at]
        for key in expired:
            del self._cache[key]
        self._stats.expirations += len(expired)
        return len(expired)
