"""
TTLCacheBase and CacheStats unit tests.

Verification techniques:
- Time dependency: freeze_time for TTL expiry (§8.11)
- Thread safety: concurrent access (§8.7)
- Boundary analysis: max_size=0 vs >0, jitter (§8.1)
- Idempotency: double invalidate (§8.3)
- Side effects: stats tracking (§8.4)
"""

from __future__ import annotations

import threading

import pytest

from baldur.core.ttl_cache import CacheStats, TTLCacheBase
from tests.factories.time_helpers import freeze_time

# =============================================================================
# CacheStats
# =============================================================================


class TestCacheStatsContract:
    """CacheStats initial values and hit_rate formula contract."""

    def test_initial_values_are_zero(self):
        """All counters start at zero."""
        stats = CacheStats()
        assert stats.hits == 0
        assert stats.misses == 0
        assert stats.invalidations == 0
        assert stats.expirations == 0

    def test_hit_rate_with_no_accesses_returns_zero(self):
        """hit_rate is 0.0 when no get() calls have been made."""
        stats = CacheStats()
        assert stats.hit_rate == 0.0


class TestCacheStatsBehavior:
    """CacheStats hit_rate calculation behavior."""

    def test_hit_rate_calculation_matches_formula(self):
        """hit_rate = hits / (hits + misses)."""
        stats = CacheStats(hits=80, misses=20)
        assert stats.hit_rate == pytest.approx(0.8)

    def test_hit_rate_all_misses_returns_zero(self):
        """hit_rate is 0.0 when all accesses are misses."""
        stats = CacheStats(hits=0, misses=100)
        assert stats.hit_rate == pytest.approx(0.0)

    def test_hit_rate_all_hits_returns_one(self):
        """hit_rate is 1.0 when all accesses are hits."""
        stats = CacheStats(hits=50, misses=0)
        assert stats.hit_rate == pytest.approx(1.0)


# =============================================================================
# TTLCacheBase — Basic Operations
# =============================================================================


class TestTTLCacheBasicBehavior:
    """TTLCacheBase get/set/invalidate basic behavior."""

    def test_get_nonexistent_key_returns_none(self):
        """Cache miss returns None."""
        cache: TTLCacheBase[str, str] = TTLCacheBase(ttl_seconds=30.0)
        assert cache.get("nonexistent") is None

    def test_set_and_get_returns_value(self):
        """Value stored with set() is returned by get()."""
        cache: TTLCacheBase[str, int] = TTLCacheBase(ttl_seconds=30.0)
        with freeze_time("2026-03-19 10:00:00"):
            cache.set("key", 42)
            assert cache.get("key") == 42

    def test_set_overwrites_existing_value(self):
        """Overwriting a key updates the stored value."""
        cache: TTLCacheBase[str, str] = TTLCacheBase(ttl_seconds=30.0)
        with freeze_time("2026-03-19 10:00:00"):
            cache.set("key", "first")
            cache.set("key", "second")
            assert cache.get("key") == "second"

    def test_invalidate_existing_key_returns_true(self):
        """Invalidating an existing key returns True and removes it."""
        cache: TTLCacheBase[str, int] = TTLCacheBase(ttl_seconds=30.0)
        with freeze_time("2026-03-19 10:00:00"):
            cache.set("key", 1)
            assert cache.invalidate("key") is True
            assert cache.get("key") is None

    def test_invalidate_nonexistent_key_returns_false(self):
        """Invalidating a missing key returns False."""
        cache: TTLCacheBase[str, int] = TTLCacheBase(ttl_seconds=30.0)
        assert cache.invalidate("missing") is False

    def test_invalidate_all_clears_cache(self):
        """invalidate_all removes all entries and returns count."""
        cache: TTLCacheBase[str, int] = TTLCacheBase(ttl_seconds=30.0)
        with freeze_time("2026-03-19 10:00:00"):
            cache.set("a", 1)
            cache.set("b", 2)
            cache.set("c", 3)
            count = cache.invalidate_all()
            assert count == 3
            assert cache.size == 0

    def test_size_tracks_entry_count(self):
        """size property reflects current number of entries."""
        cache: TTLCacheBase[str, int] = TTLCacheBase(ttl_seconds=30.0)
        assert cache.size == 0
        with freeze_time("2026-03-19 10:00:00"):
            cache.set("a", 1)
            assert cache.size == 1
            cache.set("b", 2)
            assert cache.size == 2
            cache.invalidate("a")
            assert cache.size == 1


# =============================================================================
# TTLCacheBase — TTL Expiry (Time Dependency §8.11)
# =============================================================================


class TestTTLCacheExpiryBehavior:
    """TTL expiry behavior using freeze_time."""

    def test_entry_available_before_ttl(self):
        """Entry is accessible before TTL expires."""
        cache: TTLCacheBase[str, str] = TTLCacheBase(ttl_seconds=30.0)
        with freeze_time("2026-03-19 10:00:00"):
            cache.set("key", "value")
        with freeze_time("2026-03-19 10:00:29"):
            assert cache.get("key") == "value"

    def test_entry_expired_after_ttl(self):
        """Entry returns None after TTL expires."""
        cache: TTLCacheBase[str, str] = TTLCacheBase(ttl_seconds=30.0)
        with freeze_time("2026-03-19 10:00:00"):
            cache.set("key", "value")
        with freeze_time("2026-03-19 10:00:31"):
            assert cache.get("key") is None

    def test_expired_entry_increments_expiration_stat(self):
        """Accessing an expired entry increments expirations counter."""
        cache: TTLCacheBase[str, str] = TTLCacheBase(ttl_seconds=10.0)
        with freeze_time("2026-03-19 10:00:00"):
            cache.set("key", "value")
        with freeze_time("2026-03-19 10:00:11"):
            cache.get("key")
        assert cache.get_stats().expirations == 1

    def test_ttl_override_uses_custom_ttl(self):
        """ttl_override in set() overrides the default TTL."""
        cache: TTLCacheBase[str, str] = TTLCacheBase(ttl_seconds=60.0)
        with freeze_time("2026-03-19 10:00:00"):
            cache.set("key", "value", ttl_override=5.0)
        with freeze_time("2026-03-19 10:00:06"):
            assert cache.get("key") is None

    def test_cleanup_expired_removes_stale_entries(self):
        """_cleanup_expired removes only expired entries."""
        cache: TTLCacheBase[str, int] = TTLCacheBase(ttl_seconds=10.0)
        with freeze_time("2026-03-19 10:00:00"):
            cache.set("short", 1, ttl_override=5.0)
            cache.set("long", 2, ttl_override=60.0)
        with freeze_time("2026-03-19 10:00:06"):
            removed = cache._cleanup_expired()
            assert removed == 1
            assert cache.get("long") == 2


# =============================================================================
# TTLCacheBase — LRU Eviction (Boundary §8.1)
# =============================================================================


class TestTTLCacheLRUEvictionBehavior:
    """LRU eviction behavior when max_size is set."""

    def test_unlimited_cache_does_not_evict(self):
        """max_size=0 means unlimited — no eviction."""
        cache: TTLCacheBase[str, int] = TTLCacheBase(ttl_seconds=30.0, max_size=0)
        with freeze_time("2026-03-19 10:00:00"):
            for i in range(100):
                cache.set(f"key{i}", i)
            assert cache.size == 100

    def test_evicts_lru_entry_when_full(self):
        """Oldest (LRU) entry is evicted when max_size is reached."""
        cache: TTLCacheBase[str, int] = TTLCacheBase(ttl_seconds=30.0, max_size=3)
        with freeze_time("2026-03-19 10:00:00"):
            cache.set("a", 1)
            cache.set("b", 2)
            cache.set("c", 3)
            # Cache full — "a" is LRU
            cache.set("d", 4)
            assert cache.get("a") is None  # Evicted
            assert cache.get("d") == 4

    def test_get_promotes_entry_in_lru_order(self):
        """Accessing an entry moves it to MRU, protecting it from eviction."""
        cache: TTLCacheBase[str, int] = TTLCacheBase(ttl_seconds=30.0, max_size=3)
        with freeze_time("2026-03-19 10:00:00"):
            cache.set("a", 1)
            cache.set("b", 2)
            cache.set("c", 3)
            # Access "a" — promotes it to MRU
            cache.get("a")
            # Now LRU order: b, c, a — "b" should be evicted
            cache.set("d", 4)
            assert cache.get("a") == 1  # Protected by access
            assert cache.get("b") is None  # Evicted (was LRU)

    def test_overwrite_does_not_evict(self):
        """Overwriting an existing key does not trigger eviction."""
        cache: TTLCacheBase[str, int] = TTLCacheBase(ttl_seconds=30.0, max_size=2)
        with freeze_time("2026-03-19 10:00:00"):
            cache.set("a", 1)
            cache.set("b", 2)
            cache.set("a", 10)  # Overwrite, not insert
            assert cache.size == 2
            assert cache.get("a") == 10
            assert cache.get("b") == 2


# =============================================================================
# TTLCacheBase — Jitter
# =============================================================================


class TestTTLCacheJitterBehavior:
    """Jitter application in TTL calculation."""

    def test_zero_jitter_returns_exact_ttl(self):
        """jitter_range=0 means _calculate_ttl returns exact ttl_seconds."""
        cache: TTLCacheBase[str, int] = TTLCacheBase(ttl_seconds=30.0, jitter_range=0.0)
        assert cache._calculate_ttl() == 30.0

    def test_positive_jitter_varies_ttl(self):
        """jitter_range>0 produces TTL within [ttl-jitter, ttl+jitter]."""
        cache: TTLCacheBase[str, int] = TTLCacheBase(ttl_seconds=30.0, jitter_range=5.0)
        ttls = {cache._calculate_ttl() for _ in range(100)}
        # All values should be in [25.0, 35.0]
        assert all(25.0 <= ttl <= 35.0 for ttl in ttls)
        # At least some variation should exist
        assert len(ttls) > 1


# =============================================================================
# TTLCacheBase — Stats Tracking (Side Effects §8.4)
# =============================================================================


class TestTTLCacheStatsBehavior:
    """Cache statistics tracking behavior."""

    def test_hit_increments_on_cache_hit(self):
        """Successful get() increments hits counter."""
        cache: TTLCacheBase[str, int] = TTLCacheBase(ttl_seconds=30.0)
        with freeze_time("2026-03-19 10:00:00"):
            cache.set("key", 1)
            cache.get("key")
            cache.get("key")
        assert cache.get_stats().hits == 2

    def test_miss_increments_on_cache_miss(self):
        """Failed get() increments misses counter."""
        cache: TTLCacheBase[str, int] = TTLCacheBase(ttl_seconds=30.0)
        cache.get("missing1")
        cache.get("missing2")
        assert cache.get_stats().misses == 2

    def test_invalidation_counter_tracks_removals(self):
        """invalidate()/invalidate_all() increment invalidations counter."""
        cache: TTLCacheBase[str, int] = TTLCacheBase(ttl_seconds=30.0)
        with freeze_time("2026-03-19 10:00:00"):
            cache.set("a", 1)
            cache.set("b", 2)
            cache.invalidate("a")
            cache.invalidate_all()
        stats = cache.get_stats()
        assert stats.invalidations == 2  # 1 from invalidate + 1 from invalidate_all

    def test_get_stats_returns_stats_object(self):
        """get_stats() returns a CacheStats instance."""
        cache: TTLCacheBase[str, int] = TTLCacheBase(ttl_seconds=30.0)
        stats = cache.get_stats()
        assert isinstance(stats, CacheStats)

    def test_get_stats_returns_snapshot_not_internal_object(self):
        """get_stats() is not the live internal stats object (copy-on-return)."""
        cache: TTLCacheBase[str, int] = TTLCacheBase(ttl_seconds=30.0)
        assert cache.get_stats() is not cache._stats

    def test_mutating_returned_stats_is_isolated_from_cache(self):
        """Caller mutation of the returned snapshot never corrupts accounting."""
        cache: TTLCacheBase[str, int] = TTLCacheBase(ttl_seconds=30.0)
        with freeze_time("2026-03-19 10:00:00"):
            cache.set("key", 1)
            cache.get("key")
            cache.get("missing")

        snapshot = cache.get_stats()
        snapshot.hits = 999
        snapshot.misses = 999
        snapshot.invalidations = 999

        fresh = cache.get_stats()
        assert fresh.hits == 1
        assert fresh.misses == 1
        assert fresh.invalidations == 0

    def test_snapshot_is_frozen_in_time_after_further_operations(self):
        """A held snapshot does not change as the cache keeps operating."""
        cache: TTLCacheBase[str, int] = TTLCacheBase(ttl_seconds=30.0)
        with freeze_time("2026-03-19 10:00:00"):
            cache.set("key", 1)
            cache.get("key")
            snapshot = cache.get_stats()
            cache.get("key")
            cache.get("key")

        assert snapshot.hits == 1
        assert cache.get_stats().hits == 3


# =============================================================================
# TTLCacheBase — Thread Safety (§8.7)
# =============================================================================


class TestTTLCacheThreadSafetyBehavior:
    """Concurrent access does not corrupt data."""

    def test_concurrent_set_get_no_errors(self):
        """Multiple threads performing set/get simultaneously produce no errors."""
        cache: TTLCacheBase[str, int] = TTLCacheBase(ttl_seconds=60.0, max_size=100)
        errors: list[Exception] = []

        def worker(thread_id: int) -> None:
            try:
                for i in range(100):
                    key = f"t{thread_id}_k{i}"
                    cache.set(key, i)
                    cache.get(key)
                    if i % 10 == 0:
                        cache.invalidate(key)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0


# =============================================================================
# TTLCacheBase — Idempotency (§8.3)
# =============================================================================


class TestTTLCacheIdempotencyBehavior:
    """Idempotent operations produce consistent results."""

    def test_double_invalidate_same_key_returns_false_second_time(self):
        """Second invalidate of same key returns False (already removed)."""
        cache: TTLCacheBase[str, int] = TTLCacheBase(ttl_seconds=30.0)
        with freeze_time("2026-03-19 10:00:00"):
            cache.set("key", 1)
        assert cache.invalidate("key") is True
        assert cache.invalidate("key") is False

    def test_double_invalidate_all_returns_zero_second_time(self):
        """Second invalidate_all on empty cache returns 0."""
        cache: TTLCacheBase[str, int] = TTLCacheBase(ttl_seconds=30.0)
        with freeze_time("2026-03-19 10:00:00"):
            cache.set("key", 1)
        cache.invalidate_all()
        assert cache.invalidate_all() == 0


# =============================================================================
# TTLCacheBase — get_or_compute (doc 594 D2)
# =============================================================================


class TestGetOrComputeBehavior:
    """get_or_compute outcome matrix: hit / miss / None / exception / False."""

    def test_cache_hit_returns_cached_without_calling_fn(self):
        """A hit returns immediately - the compute callable is never invoked."""
        cache: TTLCacheBase[str, str] = TTLCacheBase(ttl_seconds=30.0)
        calls: list[int] = []

        def fn() -> str:
            calls.append(1)
            return "fresh"

        with freeze_time("2026-03-19 10:00:00"):
            cache.set("key", "cached")
            result = cache.get_or_compute("key", fn)

        assert result == "cached"
        assert calls == []

    def test_miss_computes_stores_and_subsequent_call_hits(self):
        """A miss runs fn once; the stored value serves the next call."""
        cache: TTLCacheBase[str, str] = TTLCacheBase(ttl_seconds=30.0)
        calls: list[int] = []

        def fn() -> str:
            calls.append(1)
            return "computed"

        with freeze_time("2026-03-19 10:00:00"):
            first = cache.get_or_compute("key", fn)
            second = cache.get_or_compute("key", fn)

        assert first == "computed"
        assert second == "computed"
        assert len(calls) == 1

    def test_none_result_returned_but_not_cached(self):
        """fn returning None is the miss sentinel - returned, never stored."""
        cache: TTLCacheBase[str, str] = TTLCacheBase(ttl_seconds=30.0)
        calls: list[int] = []

        def fn() -> str | None:
            calls.append(1)
            return None

        with freeze_time("2026-03-19 10:00:00"):
            first = cache.get_or_compute("key", fn)
            second = cache.get_or_compute("key", fn)

        assert first is None
        assert second is None
        assert len(calls) == 2  # nothing cached -> recomputed each call
        assert cache.size == 0

    def test_exception_propagates_and_caches_nothing(self):
        """A raising fn propagates; the key stays computable afterwards."""
        cache: TTLCacheBase[str, str] = TTLCacheBase(ttl_seconds=30.0)

        def failing_fn() -> str:
            raise ConnectionError("backend down")

        with freeze_time("2026-03-19 10:00:00"):
            with pytest.raises(ConnectionError, match="backend down"):
                cache.get_or_compute("key", failing_fn)

            assert cache.size == 0
            assert cache.get_or_compute("key", lambda: "recovered") == "recovered"

    def test_false_result_is_a_legitimate_cached_value(self):
        """False (unlike None) is stored and served on subsequent calls."""
        cache: TTLCacheBase[str, bool] = TTLCacheBase(ttl_seconds=30.0)
        calls: list[int] = []

        def fn() -> bool:
            calls.append(1)
            return False

        with freeze_time("2026-03-19 10:00:00"):
            first = cache.get_or_compute("key", fn)
            second = cache.get_or_compute("key", fn)
            stored = cache.get("key")

        assert first is False
        assert second is False
        assert len(calls) == 1  # False cached -> fn not re-invoked
        assert stored is False

    def test_expired_entry_recomputes(self):
        """An expired entry is a miss - fn runs again after TTL."""
        cache: TTLCacheBase[str, str] = TTLCacheBase(ttl_seconds=10.0)
        calls: list[int] = []

        def fn() -> str:
            calls.append(1)
            return f"v{len(calls)}"

        with freeze_time("2026-03-19 10:00:00"):
            assert cache.get_or_compute("key", fn) == "v1"
        with freeze_time("2026-03-19 10:00:11"):
            assert cache.get_or_compute("key", fn) == "v2"

        assert len(calls) == 2

    def test_ttl_override_controls_stored_entry_expiry(self):
        """ttl_override (not the instance default) bounds the stored entry."""
        # Given - instance default 60s, override 5s
        cache: TTLCacheBase[str, str] = TTLCacheBase(ttl_seconds=60.0)
        calls: list[int] = []

        def fn() -> str:
            calls.append(1)
            return "value"

        with freeze_time("2026-03-19 10:00:00"):
            cache.get_or_compute("key", fn, ttl_override=5.0)

        # Then - alive just before the override boundary...
        with freeze_time("2026-03-19 10:00:04"):
            cache.get_or_compute("key", fn, ttl_override=5.0)
            assert len(calls) == 1

        # ...expired just after it (instance default 60s would still be alive)
        with freeze_time("2026-03-19 10:00:06"):
            cache.get_or_compute("key", fn, ttl_override=5.0)
            assert len(calls) == 2


class TestGetOrComputeConcurrencyBehavior:
    """Concurrent same-key misses execute fn exactly once (§8.7)."""

    def test_concurrent_miss_computes_exactly_once(self):
        """N threads missing one key -> 1 compute, all share the value.

        Deterministic regardless of thread scheduling: overlapping callers
        dedup through the Singleflight, and a late arrival hits the winner's
        stored value via the double-check in _compute_and_store.
        """
        # Given
        cache: TTLCacheBase[str, str] = TTLCacheBase(ttl_seconds=60.0)
        n_threads = 8
        calls: list[int] = []
        results: list[str] = []
        errors: list[Exception] = []
        results_lock = threading.Lock()
        barrier = threading.Barrier(n_threads + 1)  # +1 for the main thread
        release = threading.Event()

        def gated_fn() -> str:
            calls.append(1)  # winner-only
            release.wait(timeout=5.0)
            return "computed"

        def worker() -> None:
            try:
                barrier.wait(timeout=5.0)
                value = cache.get_or_compute("hot-key", gated_fn)
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
        assert results == ["computed"] * n_threads
        assert cache._singleflight._inflight == {}
