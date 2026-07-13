"""
Serve-stale cache store tests.

Test Coverage:
- StaleCacheStore set/get, stale detection, statistics
- StaleCacheStore O(1) oldest-entry eviction (doc 594 D9 / G8)
- StaleCacheStore.build_stale_cache_key cache-key helper (#234)

The store was relocated from the circuit-breaker package to
``baldur.core.stale_cache`` (it is a CB-independent serve-stale primitive).
"""

from datetime import UTC, datetime, timedelta

from baldur.core.stale_cache import StaleCacheEntry, StaleCacheStore

# =============================================================================
# StaleCacheStore
# =============================================================================


class TestStaleCacheStore:
    """StaleCacheStore tests."""

    def test_set_and_get(self):
        """Cache store and lookup."""
        store = StaleCacheStore()
        store.set("key1", {"data": "value"}, ttl_seconds=300)

        entry = store.get("key1", max_stale_age=300)

        assert entry is not None
        assert entry.value == {"data": "value"}

    def test_get_nonexistent_returns_none(self):
        """Lookup of a missing key returns None."""
        store = StaleCacheStore()

        entry = store.get("nonexistent")

        assert entry is None

    def test_stale_detection(self):
        """Stale state detection."""
        entry = StaleCacheEntry(
            key="key1",
            value="data",
            ttl_seconds=1,
        )
        # Simulate TTL exceedance
        entry.cached_at = datetime.now(UTC) - timedelta(seconds=2)

        assert entry.is_stale() is True

    def test_cache_stats(self):
        """Cache statistics."""
        store = StaleCacheStore()
        store.set("key1", "value1")
        store.get("key1")
        store.get("key2")  # miss

        stats = store.get_stats()

        assert stats["sets"] == 1
        assert stats["hits"] >= 1 or stats["stale_hits"] >= 1
        assert stats["misses"] >= 1


class TestStaleCacheStoreEvictionBehavior:
    """O(1) oldest-entry eviction semantics (doc 594 D9 / G8).

    Insertion/update order equals age order because set() always stores a
    fresh StaleCacheEntry (overwrites move to the end), so FIFO-head
    eviction IS oldest-entry eviction.
    """

    @staticmethod
    def _make_store(max_entries: int):
        return StaleCacheStore(max_entries=max_entries)

    def test_new_key_at_capacity_evicts_oldest(self):
        """Inserting a NEW key at capacity evicts the oldest entry only."""
        store = self._make_store(max_entries=3)
        store.set("k0", "v0")
        store.set("k1", "v1")
        store.set("k2", "v2")

        store.set("k3", "v3")

        assert store.get("k0") is None  # oldest evicted
        assert store.get("k1") is not None
        assert store.get("k2") is not None
        assert store.get("k3") is not None
        assert store.get_stats()["size"] == 3

    def test_overwrite_at_capacity_evicts_nothing(self):
        """Overwriting an existing key at capacity replaces in place -
        no unrelated entry is evicted (fixes the pre-594 quirk)."""
        store = self._make_store(max_entries=3)
        store.set("k0", "v0")
        store.set("k1", "v1")
        store.set("k2", "v2")

        store.set("k0", "v0-new")

        assert store.get_stats()["size"] == 3
        entry = store.get("k0")
        assert entry is not None
        assert entry.value == "v0-new"
        assert store.get("k1") is not None
        assert store.get("k2") is not None

    def test_overwrite_refreshes_age_order(self):
        """An overwrite gets a fresh cached_at and moves to the end, so the
        next at-capacity insert evicts the actually-oldest entry."""
        store = self._make_store(max_entries=3)
        store.set("k0", "v0")
        store.set("k1", "v1")
        store.set("k2", "v2")

        store.set("k0", "v0-new")  # k0 becomes newest; k1 is now oldest
        store.set("k3", "v3")  # at capacity -> evicts k1

        assert store.get("k1") is None
        assert store.get("k0") is not None
        assert store.get("k2") is not None
        assert store.get("k3") is not None

    def test_repeated_inserts_keep_size_at_capacity(self):
        """Sustained unique-key churn never grows the store past capacity."""
        store = self._make_store(max_entries=2)

        for i in range(10):
            store.set(f"k{i}", f"v{i}")
            assert store.get_stats()["size"] <= 2

        # The two newest survive
        assert store.get("k8") is not None
        assert store.get("k9") is not None


# =============================================================================
# Behavior — StaleCacheStore.build_stale_cache_key (#234)
# =============================================================================


class TestBuildStaleCacheKeyBehavior:
    """StaleCacheStore.build_stale_cache_key() behavior verification (#234)."""

    def test_static_method_format(self):
        """The static method returns a 'domain:identifier' formatted key."""
        result = StaleCacheStore.build_stale_cache_key("payment", "user123")
        assert result == "payment:user123"

    def test_various_domains(self):
        """Various domain values are correctly included in the key."""
        assert StaleCacheStore.build_stale_cache_key("payment", "123") == "payment:123"
        assert StaleCacheStore.build_stale_cache_key("product", "abc") == "product:abc"
        assert StaleCacheStore.build_stale_cache_key("user", "xyz") == "user:xyz"

    def test_empty_strings(self):
        """Empty strings still preserve the format."""
        result = StaleCacheStore.build_stale_cache_key("", "")
        assert result == ":"

    def test_special_characters_in_identifier(self):
        """Special characters in the identifier are preserved as-is."""
        result = StaleCacheStore.build_stale_cache_key("payment", "user-123_v2")
        assert result == "payment:user-123_v2"

    def test_consistent_key_for_same_input(self):
        """The same input always produces the same key (deterministic)."""
        key1 = StaleCacheStore.build_stale_cache_key("service", "id")
        key2 = StaleCacheStore.build_stale_cache_key("service", "id")
        assert key1 == key2
