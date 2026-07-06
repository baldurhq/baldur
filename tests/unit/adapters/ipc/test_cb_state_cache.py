"""
IPCStateCache unit tests.

Coverage:
- TTL-based cache expiry
- EventBus event-driven invalidation
- Thread-safe concurrent access
- Cache statistics
- get_or_set singleflight semantics (doc 594 D7)
"""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock

import pytest

from baldur.adapters.ipc.cb_state_cache import (
    CacheStats,
    CBStateCache,
    IPCCacheEntry,
    IPCStateCache,
    get_cb_state_cache,
    reset_cb_state_cache,
)


class TestIPCCacheEntry:
    """IPCCacheEntry tests."""

    def test_create_entry(self):
        """Cache entry creation."""
        entry = IPCCacheEntry(value={"allowed": True}, expires_at=time.time() + 10)

        assert entry.value == {"allowed": True}
        assert entry.expires_at > time.time()
        assert entry.created_at <= time.time()


class TestCacheStats:
    """CacheStats tests."""

    def test_initial_stats(self):
        """Initial statistics."""
        stats = CacheStats()

        assert stats.hits == 0
        assert stats.misses == 0
        assert stats.invalidations == 0
        assert stats.expirations == 0

    def test_hit_rate_zero(self):
        """Hit rate is 0 with no lookups."""
        stats = CacheStats()

        assert stats.hit_rate == 0.0

    def test_hit_rate_calculation(self):
        """Hit rate calculation."""
        stats = CacheStats(hits=80, misses=20)

        assert stats.hit_rate == 0.8


class TestCBStateCache:
    """CBStateCache tests."""

    def test_init_default(self):
        """Default initialization."""
        cache = CBStateCache(enable_event_invalidation=False)

        assert cache._ttl == CBStateCache.DEFAULT_TTL_SECONDS
        assert cache.size == 0

    def test_init_custom_ttl(self):
        """Initialization with a custom TTL."""
        cache = CBStateCache(ttl_seconds=10.0, enable_event_invalidation=False)

        assert cache._ttl == 10.0

    def test_set_and_get(self):
        """Cache store and lookup."""
        cache = CBStateCache(enable_event_invalidation=False)

        cache.set("payment_gateway", {"allowed": True, "state": "closed"})
        value, hit = cache.get("payment_gateway")

        assert hit is True
        assert value == {"allowed": True, "state": "closed"}

    def test_get_nonexistent(self):
        """Lookup of a nonexistent key."""
        cache = CBStateCache(enable_event_invalidation=False)

        value, hit = cache.get("nonexistent")

        assert hit is False
        assert value is None

    def test_get_expired(self):
        """Lookup of an expired entry."""
        cache = CBStateCache(ttl_seconds=0.01, enable_event_invalidation=False)

        cache.set("test", {"data": "value"})
        time.sleep(0.02)  # exceed TTL

        value, hit = cache.get("test")

        assert hit is False
        assert value is None
        assert cache._stats.expirations == 1

    def test_get_or_set_with_factory(self):
        """get_or_set with a factory function."""
        cache = CBStateCache(enable_event_invalidation=False)
        factory_called = [0]

        def factory():
            factory_called[0] += 1
            return {"data": "new"}

        # First call - factory runs
        result1 = cache.get_or_set("key", factory)
        assert result1 == {"data": "new"}
        assert factory_called[0] == 1

        # Second call - served from cache
        result2 = cache.get_or_set("key", factory)
        assert result2 == {"data": "new"}
        assert factory_called[0] == 1  # factory not re-invoked

    def test_invalidate(self):
        """Invalidate a specific key."""
        cache = CBStateCache(enable_event_invalidation=False)

        cache.set("service1", {"data": 1})
        cache.set("service2", {"data": 2})

        result = cache.invalidate("service1")

        assert result is True
        assert cache.get("service1")[1] is False
        assert cache.get("service2")[1] is True
        assert cache._stats.invalidations == 1

    def test_invalidate_nonexistent(self):
        """Invalidate a nonexistent key."""
        cache = CBStateCache(enable_event_invalidation=False)

        result = cache.invalidate("nonexistent")

        assert result is False

    def test_invalidate_all(self):
        """Invalidate the entire cache."""
        cache = CBStateCache(enable_event_invalidation=False)

        cache.set("service1", {"data": 1})
        cache.set("service2", {"data": 2})
        cache.set("service3", {"data": 3})

        count = cache.invalidate_all()

        assert count == 3
        assert cache.size == 0

    def test_contains(self):
        """Key existence check."""
        cache = CBStateCache(enable_event_invalidation=False)

        cache.set("existing", {"data": True})

        assert cache.contains("existing") is True
        assert cache.contains("nonexistent") is False

    def test_keys(self):
        """List of cached keys."""
        cache = CBStateCache(enable_event_invalidation=False)

        cache.set("a", 1)
        cache.set("b", 2)
        cache.set("c", 3)

        keys = cache.keys()

        assert set(keys) == {"a", "b", "c"}

    def test_stats(self):
        """Cache statistics."""
        cache = CBStateCache(enable_event_invalidation=False)

        cache.set("key", {"data": True})
        cache.get("key")  # hit
        cache.get("key")  # hit
        cache.get("missing")  # miss

        stats = cache.get_stats_dict()

        assert stats["size"] == 1
        assert stats["hits"] == 2
        assert stats["misses"] == 1
        assert stats["hit_rate"] == pytest.approx(0.6667, rel=0.01)

    def test_close(self):
        """Cache shutdown."""
        cache = CBStateCache(enable_event_invalidation=False)

        cache.set("key1", 1)
        cache.set("key2", 2)

        cache.close()

        assert cache.size == 0


class TestCBStateCacheEventIntegration:
    """EventBus integration tests."""

    def test_event_invalidation_handler_called(self):
        """The invalidation handler fires on a state-change event."""
        cache = CBStateCache(enable_event_invalidation=False)

        # Exercise the event handler manually
        mock_event = MagicMock()
        mock_event.data = {"service_name": "test_service"}
        mock_event.event_type = MagicMock()
        mock_event.event_type.value = "circuit_breaker_opened"

        cache.set("test_service", {"state": "closed"})
        assert cache.contains("test_service")

        cache._on_state_change(mock_event)

        assert not cache.contains("test_service")


class TestCBStateCacheConcurrency:
    """Concurrency tests."""

    def test_concurrent_access(self):
        """Concurrent access produces no errors."""
        cache = CBStateCache(enable_event_invalidation=False)
        errors = []

        def worker(service_id):
            try:
                for _ in range(100):
                    key = f"service_{service_id}"
                    cache.set(key, {"id": service_id})
                    cache.get(key)
                    cache.invalidate(key)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0


class TestIPCGetOrSetBehavior:
    """get_or_set singleflight semantics via inherited get_or_compute (doc 594 D7)."""

    def test_concurrent_get_or_set_runs_factory_exactly_once(self):
        """N threads missing one service -> the factory runs once.

        Deterministic regardless of scheduling: overlapping callers share
        the winner's Future, and a late arrival hits the value the winner
        cached via get_or_compute's double-check.
        """
        # Given
        cache = IPCStateCache(enable_event_invalidation=False)
        n_threads = 8
        calls: list[int] = []
        results: list[dict] = []
        errors: list[Exception] = []
        results_lock = threading.Lock()
        barrier = threading.Barrier(n_threads + 1)  # +1 for the main thread
        release = threading.Event()

        def gated_factory() -> dict:
            calls.append(1)  # winner-only
            release.wait(timeout=5.0)
            return {"allowed": True}

        def worker() -> None:
            try:
                barrier.wait(timeout=5.0)
                value = cache.get_or_set("payment_gateway", gated_factory)
                with results_lock:
                    results.append(value)
            except Exception as e:  # pragma: no cover - failure diagnostics
                with results_lock:
                    errors.append(e)

        # When
        threads = [threading.Thread(target=worker) for _ in range(n_threads)]
        for t in threads:
            t.start()
        barrier.wait(timeout=5.0)
        release.set()
        for t in threads:
            t.join(timeout=10.0)

        # Then
        assert errors == []
        assert len(calls) == 1
        assert results == [{"allowed": True}] * n_threads

    def test_non_callable_factory_value_is_stored_and_returned(self):
        """A plain value passed as factory is returned and cached."""
        cache = IPCStateCache(enable_event_invalidation=False)

        result = cache.get_or_set("svc", {"allowed": True})

        assert result == {"allowed": True}
        value, hit = cache.get("svc")
        assert hit is True
        assert value == {"allowed": True}

    def test_factory_returning_none_is_returned_but_not_stored(self):
        """A None factory result is not stored - observably identical to
        the previous behavior, where a stored None read as a miss."""
        cache = IPCStateCache(enable_event_invalidation=False)
        calls: list[int] = []

        def none_factory() -> None:
            calls.append(1)
            return None

        first = cache.get_or_set("svc", none_factory)
        second = cache.get_or_set("svc", none_factory)

        assert first is None
        assert second is None
        assert len(calls) == 2  # nothing cached -> factory re-ran
        assert cache.get("svc") == (None, False)

    def test_cached_value_is_hit_despite_tuple_get_override(self):
        """get_or_set detects hits through _lookup, immune to the subclass
        get() override returning (value, hit) tuples (doc 594 D2 rationale)."""
        # Given - a value cached through the overridden set()
        cache = IPCStateCache(enable_event_invalidation=False)
        cache.set("svc", {"state": "closed"})
        calls: list[int] = []

        def factory() -> dict:
            calls.append(1)
            return {"state": "wrong"}

        # When
        result = cache.get_or_set("svc", factory)

        # Then - the raw cached dict came back, not a (value, hit) tuple,
        # and the factory never ran
        assert result == {"state": "closed"}
        assert calls == []


class TestGlobalCBStateCache:
    """Singleton instance tests."""

    def teardown_method(self):
        """Reset the singleton after each test."""
        reset_cb_state_cache()

    def test_singleton(self):
        """Returns the same singleton instance."""
        cache1 = get_cb_state_cache()
        cache2 = get_cb_state_cache()

        assert cache1 is cache2

    def test_reset_singleton(self):
        """Singleton reset creates a fresh instance."""
        cache1 = get_cb_state_cache()

        reset_cb_state_cache()

        cache2 = get_cb_state_cache()

        assert cache1 is not cache2
