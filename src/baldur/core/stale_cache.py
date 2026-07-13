"""
Serve-stale cache primitive.

A small in-process cache that can serve entries *past* their TTL, up to a
bounded max-stale age. This is the framework's only user-data serve-stale
surface: unlike ``baldur.core.ttl_cache`` (which evicts at TTL and never
returns an expired value), this store keeps an entry usable during the
``ttl_seconds .. ttl_seconds + max_stale_age`` window so a caller can fall back
to slightly stale data while a dependency is unavailable.

Scope: in-process, per-worker. Entries live in this process's memory only;
there is no cross-worker or cross-host sharing. Construct one per use site (like
``TTLCacheBase``) rather than sharing a module singleton.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Generic, TypeVar

from baldur.core.serializable import SerializableMixin
from baldur.utils.time import utc_now

T = TypeVar("T")


# =============================================================================
# Stale Cache Entry
# =============================================================================


@dataclass
class StaleCacheEntry(SerializableMixin, Generic[T]):
    """
    A single cached value with fresh / stale / expired age semantics.

    Attributes:
        key: Cache key
        value: Cached value
        cached_at: Cache time
        service_id: Service ID
        ttl_seconds: Original TTL
    """

    key: str
    value: T
    cached_at: datetime = field(default_factory=lambda: utc_now())
    service_id: str = ""
    ttl_seconds: int = 300  # default 5 minutes

    def age_seconds(self) -> float:
        """Cache age (seconds)."""
        return (utc_now() - self.cached_at).total_seconds()

    def is_stale(self) -> bool:
        """Whether TTL is exceeded (i.e., stale)."""
        return self.age_seconds() > self.ttl_seconds

    def is_expired(self, max_stale_age: int) -> bool:
        """Whether the max stale-allowance time is exceeded."""
        return self.age_seconds() > (self.ttl_seconds + max_stale_age)

    def to_dict(self) -> dict[str, Any]:
        """Convert to a dictionary."""
        return {
            "key": self.key,
            "value": str(self.value)[:100],  # value is summarized
            "cached_at": self.cached_at.isoformat(),
            "service_id": self.service_id,
            "ttl_seconds": self.ttl_seconds,
            "age_seconds": self.age_seconds(),
            "is_stale": self.is_stale(),
        }


# =============================================================================
# Stale Cache Store
# =============================================================================


class StaleCacheStore:
    """
    Bounded in-process store that can serve stale entries.

    Implemented as a simple in-memory cache. In real operation it can be
    replaced with Redis, etc.
    """

    def __init__(self, max_entries: int = 10000):
        """
        Initialize.

        Args:
            max_entries: Maximum number of cache entries
        """
        # Why OrderedDict: a plain dict preserves insertion order on
        # 3.7+ but lacks O(1) move_to_end and FIFO popitem(last=False).
        # Insertion/update order equals age order here because set()
        # always stores a fresh StaleCacheEntry with a new cached_at
        # (moving overwrites to the end) and get() only deletes expired
        # entries - so popitem(last=False) IS oldest-entry eviction.
        self._cache: OrderedDict[str, StaleCacheEntry] = OrderedDict()
        self._max_entries = max_entries
        self._lock = threading.RLock()
        self._stats = {
            "hits": 0,
            "misses": 0,
            "stale_hits": 0,
            "expired": 0,
            "sets": 0,
        }

    @staticmethod
    def build_stale_cache_key(domain: str, identifier: str) -> str:
        """
        Centralize the stale cache key generation rule.

        Ensures every producer and consumer of a stale entry agrees on the
        same key format.

        Args:
            domain: Service domain (e.g., "payment", "product")
            identifier: Resource identifier (e.g., "user123", "order456")

        Returns:
            Normalized cache key (e.g., "payment:user123")
        """
        return f"{domain}:{identifier}"

    def get(
        self,
        key: str,
        max_stale_age: int = 300,
    ) -> StaleCacheEntry | None:
        """
        Look up the cache.

        Args:
            key: Cache key
            max_stale_age: Max stale-allowance time (seconds)

        Returns:
            StaleCacheEntry or None
        """
        with self._lock:
            entry = self._cache.get(key)

            if entry is None:
                self._stats["misses"] += 1
                return None

            # Check expiry
            if entry.is_expired(max_stale_age):
                self._stats["expired"] += 1
                del self._cache[key]
                return None

            # Check whether stale
            if entry.is_stale():
                self._stats["stale_hits"] += 1
            else:
                self._stats["hits"] += 1

            return entry

    def set(
        self,
        key: str,
        value: Any,
        service_id: str = "",
        ttl_seconds: int = 300,
    ) -> StaleCacheEntry:
        """
        Store in the cache.

        Args:
            key: Cache key
            value: Value to cache
            service_id: Service ID
            ttl_seconds: TTL (seconds)

        Returns:
            The created StaleCacheEntry

        Warning:
            Since this is an in-memory cache, the reference to value is stored as-is.
            Modifying the original object after storing also pollutes the cached data.
            The caller must observe one of the following:
            1. Treat the stored object as immutable
            2. Pass a copy via copy.copy() or copy.deepcopy() before storing
        """
        with self._lock:
            # Evict the oldest entry only when inserting a NEW key at
            # capacity - overwriting an existing key replaces in place
            # (no unrelated entry is evicted).
            overwrite = key in self._cache
            if not overwrite and len(self._cache) >= self._max_entries:
                self._evict_oldest()

            entry = StaleCacheEntry(
                key=key,
                value=value,
                service_id=service_id,
                ttl_seconds=ttl_seconds,
            )
            self._cache[key] = entry
            if overwrite:
                # Fresh cached_at -> keep update order equal to age order.
                self._cache.move_to_end(key)
            self._stats["sets"] += 1

            return entry

    def delete(self, key: str) -> bool:
        """Delete from the cache."""
        with self._lock:
            if key in self._cache:
                del self._cache[key]
                return True
            return False

    def _evict_oldest(self) -> None:
        """Evict the oldest entry in O(1) (FIFO head = oldest cached_at)."""
        if not self._cache:
            return

        self._cache.popitem(last=False)

    def clear(self) -> int:
        """Delete the entire cache."""
        with self._lock:
            count = len(self._cache)
            self._cache.clear()
            return count

    def get_stats(self) -> dict[str, Any]:
        """Cache statistics."""
        with self._lock:
            return {
                **self._stats,
                "size": len(self._cache),
                "max_entries": self._max_entries,
            }


__all__ = [
    "StaleCacheEntry",
    "StaleCacheStore",
]
