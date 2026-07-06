"""
Pre-computed Cache Service - L1 In-Process Cache.

Zero network overhead cache implementation backed by TTLCacheBase.
"""

from __future__ import annotations

from collections import OrderedDict

from baldur.core.ttl_cache import TTLCacheBase

from .constants import _get_l1_maxsize, _get_l1_ttl_seconds

# =============================================================================
# L1 In-Process Cache (TTLCacheBase)
# =============================================================================


class L1Cache:
    """
    L1 In-Process Cache using TTLCacheBase.

    Zero network overhead - immediate response.
    LRU eviction when max size is reached.
    """

    def __init__(self, maxsize: int | None = None, ttl: float | None = None):
        if maxsize is None:
            maxsize = _get_l1_maxsize()
        if ttl is None:
            ttl = _get_l1_ttl_seconds()
        self._maxsize = maxsize
        self._cache: TTLCacheBase[str, str] = TTLCacheBase(
            ttl_seconds=ttl,
            max_size=maxsize,
        )
        # Why OrderedDict: a plain dict preserves insertion order on
        # 3.7+ but lacks O(1) move_to_end and FIFO popitem(last=False),
        # both needed for the bound below. get_stale() does not reorder
        # - this is a last-known-value store, not an LRU.
        self._last_known: OrderedDict[str, str] = OrderedDict()

    def get(self, key: str) -> str | None:
        """Get value from L1 cache."""
        return self._cache.get(key)

    def get_stale(self, key: str) -> str | None:
        """Get last known value regardless of TTL expiry."""
        return self._last_known.get(key)

    def set(self, key: str, value: str) -> None:
        """Set value in L1 cache."""
        self._cache.set(key, value)
        # Bound _last_known by the same maxsize as the main cache so it
        # cannot grow unboundedly under dynamic key cardinality.
        self._last_known[key] = value
        self._last_known.move_to_end(key)
        while len(self._last_known) > self._maxsize:
            self._last_known.popitem(last=False)

    def invalidate(self, key: str) -> None:
        """Invalidate a specific cache entry."""
        self._cache.invalidate(key)

    def clear(self) -> None:
        """Clear all cache entries."""
        self._cache.invalidate_all()
        self._last_known.clear()


# Global L1 cache instance
_l1_cache = L1Cache()


def get_l1_cache() -> L1Cache:
    """Get the global L1 cache instance."""
    return _l1_cache


def reset_l1_cache() -> None:
    """Reset the global L1 cache instance to a fresh L1Cache."""
    global _l1_cache

    _l1_cache = L1Cache()
