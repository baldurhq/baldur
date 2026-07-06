"""
L1Cache stale fallback and invalidation tests (doc 445 G1/G3),
plus the _last_known size bound (doc 594 D8 / G7).

Covers:
- Contract: _last_known dict populated on set(), cleared on clear()
- Behavior: get_stale() returns last known after TTL expiry,
  invalidate() removes TTL entry but preserves _last_known,
  clear() wipes both _cache and _last_known
- Behavior: _last_known never exceeds maxsize under unique-key churn;
  survivors are the newest keys; overwrite refreshes recency
"""

from __future__ import annotations

from baldur.services.precomputed_cache.l1_cache import L1Cache


class TestL1CacheStaleContract:
    """Contract verification for L1Cache _last_known design."""

    def test_get_stale_returns_none_for_unknown_key(self):
        """get_stale() returns None for keys never set."""
        cache = L1Cache(maxsize=10, ttl=10.0)
        assert cache.get_stale("nonexistent") is None

    def test_set_populates_last_known(self):
        """set() must update _last_known dict alongside TTL cache."""
        cache = L1Cache(maxsize=10, ttl=10.0)
        cache.set("k1", "v1")
        assert cache._last_known["k1"] == "v1"

    def test_clear_wipes_last_known(self):
        """clear() must empty _last_known dict."""
        cache = L1Cache(maxsize=10, ttl=10.0)
        cache.set("k1", "v1")
        cache.set("k2", "v2")
        cache.clear()
        assert cache._last_known == {}


class TestL1CacheStaleBehavior:
    """Behavior verification for stale fallback and invalidation."""

    def test_get_stale_returns_value_after_set(self):
        """get_stale() returns the value that was set."""
        cache = L1Cache(maxsize=10, ttl=10.0)
        cache.set("k1", "hello")
        assert cache.get_stale("k1") == "hello"

    def test_get_stale_returns_latest_value_after_overwrite(self):
        """get_stale() returns the most recently set value."""
        cache = L1Cache(maxsize=10, ttl=10.0)
        cache.set("k1", "v1")
        cache.set("k1", "v2")
        assert cache.get_stale("k1") == "v2"

    def test_invalidate_removes_ttl_entry(self):
        """invalidate() removes from TTL cache."""
        cache = L1Cache(maxsize=10, ttl=10.0)
        cache.set("k1", "v1")
        cache.invalidate("k1")
        assert cache.get("k1") is None

    def test_invalidate_preserves_last_known(self):
        """invalidate() only touches TTL cache, not _last_known."""
        cache = L1Cache(maxsize=10, ttl=10.0)
        cache.set("k1", "v1")
        cache.invalidate("k1")
        assert cache.get_stale("k1") == "v1"

    def test_clear_removes_all_ttl_entries(self):
        """clear() empties the TTL cache."""
        cache = L1Cache(maxsize=10, ttl=10.0)
        cache.set("k1", "v1")
        cache.set("k2", "v2")
        cache.clear()
        assert cache.get("k1") is None
        assert cache.get("k2") is None

    def test_clear_removes_all_stale_entries(self):
        """clear() also empties _last_known."""
        cache = L1Cache(maxsize=10, ttl=10.0)
        cache.set("k1", "v1")
        cache.clear()
        assert cache.get_stale("k1") is None

    def test_invalidate_nonexistent_key_is_safe(self):
        """invalidate() on unknown key does not raise."""
        cache = L1Cache(maxsize=10, ttl=10.0)
        cache.invalidate("nonexistent")

    def test_multiple_keys_independent_stale(self):
        """Each key maintains independent stale state."""
        cache = L1Cache(maxsize=10, ttl=10.0)
        cache.set("k1", "v1")
        cache.set("k2", "v2")
        cache.invalidate("k1")
        assert cache.get_stale("k1") == "v1"
        assert cache.get_stale("k2") == "v2"
        assert cache.get("k1") is None
        assert cache.get("k2") == "v2"


class TestL1CacheStaleBoundBehavior:
    """_last_known bounded by the main cache's maxsize (doc 594 D8 / G7)."""

    def test_last_known_never_exceeds_maxsize_under_unique_key_churn(self):
        """Unique-key churn keeps _last_known at or below maxsize throughout."""
        maxsize = 5
        cache = L1Cache(maxsize=maxsize, ttl=10.0)

        for i in range(50):
            cache.set(f"k{i}", f"v{i}")
            assert len(cache._last_known) <= maxsize

        assert len(cache._last_known) == maxsize

    def test_bound_survivors_are_the_newest_keys(self):
        """Eviction drops the oldest entries - the newest maxsize survive."""
        maxsize = 3
        cache = L1Cache(maxsize=maxsize, ttl=10.0)

        for i in range(10):
            cache.set(f"k{i}", f"v{i}")

        # Newest 3 are servable as stale
        assert cache.get_stale("k7") == "v7"
        assert cache.get_stale("k8") == "v8"
        assert cache.get_stale("k9") == "v9"
        # Older entries were evicted from the stale store
        assert cache.get_stale("k0") is None
        assert cache.get_stale("k6") is None

    def test_overwrite_does_not_grow_or_evict(self):
        """Overwriting an existing key at capacity neither grows the
        store nor evicts an unrelated entry."""
        cache = L1Cache(maxsize=3, ttl=10.0)
        cache.set("k0", "v0")
        cache.set("k1", "v1")
        cache.set("k2", "v2")

        cache.set("k0", "v0-new")

        assert len(cache._last_known) == 3
        assert cache.get_stale("k0") == "v0-new"
        assert cache.get_stale("k1") == "v1"
        assert cache.get_stale("k2") == "v2"

    def test_overwrite_refreshes_recency(self):
        """An overwritten key moves to the newest position, so the next
        eviction targets the actually-oldest entry."""
        cache = L1Cache(maxsize=3, ttl=10.0)
        cache.set("k0", "v0")
        cache.set("k1", "v1")
        cache.set("k2", "v2")

        cache.set("k0", "v0-new")  # k0 becomes newest; k1 is now oldest
        cache.set("k3", "v3")  # at capacity -> evicts k1

        assert cache.get_stale("k1") is None
        assert cache.get_stale("k0") == "v0-new"
        assert cache.get_stale("k2") == "v2"
        assert cache.get_stale("k3") == "v3"
