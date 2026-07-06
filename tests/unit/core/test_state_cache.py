"""
CBStateCache unit tests.

Tests the CBStateCache's delegation to TTLCacheBase and
DegradedModeHandler integration on fetch failure.
"""

from __future__ import annotations

import threading

import pytest

from baldur.core.state_cache import CBStateCache
from tests.factories.time_helpers import freeze_time


@pytest.fixture(autouse=True)
def _reset_cache():
    """Reset CBStateCache before and after each test."""
    CBStateCache.reset()
    yield
    CBStateCache.reset()


class TestCBStateCacheBehavior:
    """CBStateCache delegation and basic operations."""

    def test_get_state_returns_none_without_callback(self):
        """get_state returns None when no fetch callback is configured."""
        assert CBStateCache.get_state("service") is None

    def test_configure_and_get_state_calls_callback(self):
        """get_state calls fetch_callback on cache miss."""
        called_with: list[str] = []

        def mock_fetch(service: str) -> dict:
            called_with.append(service)
            return {"state": "closed"}

        CBStateCache.configure(fetch_callback=mock_fetch)

        with freeze_time("2026-03-19 10:00:00"):
            result = CBStateCache.get_state("payment")

        assert result == {"state": "closed"}
        assert called_with == ["payment"]

    def test_get_state_returns_cached_on_second_call(self):
        """Second get_state returns cached value without calling callback."""
        call_count = [0]

        def mock_fetch(service: str) -> dict:
            call_count[0] += 1
            return {"state": "closed", "call": call_count[0]}

        CBStateCache.configure(fetch_callback=mock_fetch)

        with freeze_time("2026-03-19 10:00:00"):
            result1 = CBStateCache.get_state("svc")
            result2 = CBStateCache.get_state("svc")

        assert result1 == result2
        assert call_count[0] == 1

    def test_set_state_stores_value_in_cache(self):
        """set_state directly stores a value retrievable by get_state."""
        with freeze_time("2026-03-19 10:00:00"):
            CBStateCache.set_state("svc", {"state": "open"})
            assert CBStateCache.get_state("svc") == {"state": "open"}

    def test_invalidate_removes_cached_entry(self):
        """invalidate removes a specific service's cached state."""

        def mock_fetch(service: str) -> dict:
            return {"state": "closed"}

        CBStateCache.configure(fetch_callback=mock_fetch)

        with freeze_time("2026-03-19 10:00:00"):
            CBStateCache.get_state("svc")
            CBStateCache.invalidate("svc")
            # Next call should trigger fetch_callback again
            result = CBStateCache.get_state("svc")

        assert result == {"state": "closed"}

    def test_invalidate_all_clears_entire_cache(self):
        """invalidate_all removes all cached entries."""
        with freeze_time("2026-03-19 10:00:00"):
            CBStateCache.set_state("a", {"state": "closed"})
            CBStateCache.set_state("b", {"state": "open"})
            CBStateCache.invalidate_all()

        stats = CBStateCache.get_cache_stats()
        assert stats["total_entries"] == 0

    def test_get_cache_stats_returns_entry_counts(self):
        """get_cache_stats reports total/active/expired entries."""
        with freeze_time("2026-03-19 10:00:00"):
            CBStateCache.set_state("svc", {"state": "closed"})
            stats = CBStateCache.get_cache_stats()

        assert stats["total_entries"] == 1
        assert stats["active_entries"] == 1
        assert stats["expired_entries"] == 0

    def test_reset_clears_cache_and_callback(self):
        """reset clears cache and removes fetch callback."""
        CBStateCache.configure(fetch_callback=lambda s: {"state": "ok"})

        with freeze_time("2026-03-19 10:00:00"):
            CBStateCache.set_state("svc", {"state": "ok"})

        CBStateCache.reset()
        assert CBStateCache.get_state("svc") is None


class TestCBStateCacheDegradedModeBehavior:
    """DegradedModeHandler integration on fetch failure."""

    def test_fetch_failure_enters_degraded_mode(self):
        """Fetch callback exception triggers DegradedModeHandler.enter_degraded_mode."""
        from baldur.core.degraded_mode_handler import DegradedModeHandler

        DegradedModeHandler.reset()

        def failing_fetch(service: str) -> dict:
            raise ConnectionError("unreachable")

        CBStateCache.configure(fetch_callback=failing_fetch)

        # When fetch fails
        result = CBStateCache.get_state("svc")

        # Then degraded mode is entered with reason
        assert DegradedModeHandler.is_degraded() is True
        assert (
            DegradedModeHandler.get_status()["reason"] == "command_center_unreachable"
        )
        # And fallback config is returned
        assert result is not None
        assert "failure_threshold" in result

        DegradedModeHandler.reset()

    def test_degraded_result_not_cached_next_call_retries_fetch(self):
        """The degraded config is never cached - each call retries the fetch.

        Doc 594 D4: the fallback moved from _refresh into get_state's
        except block; the not-cached property must survive that move.
        """
        from baldur.core.degraded_mode_handler import DegradedModeHandler

        DegradedModeHandler.reset()
        calls: list[int] = []

        def failing_fetch(service: str) -> dict:
            calls.append(1)
            raise ConnectionError("unreachable")

        CBStateCache.configure(fetch_callback=failing_fetch)

        # When - two sequential calls while the fetch keeps failing
        first = CBStateCache.get_state("svc")
        second = CBStateCache.get_state("svc")

        # Then - both returned the degraded config, and nothing was cached
        # (a cached degraded config would have made the second call a hit)
        assert len(calls) == 2
        assert "failure_threshold" in first
        assert "failure_threshold" in second
        assert CBStateCache.get_cache_stats()["total_entries"] == 0

        DegradedModeHandler.reset()


class TestCBStateCacheDedupBehavior:
    """Concurrent same-service misses dedup to one fetch (doc 594 D4)."""

    def test_concurrent_get_state_misses_invoke_fetch_exactly_once(self):
        """N threads missing one service -> fetch_callback runs once.

        Deterministic regardless of scheduling: overlapping callers share
        the winner's Future, and a late arrival hits the value the winner
        cached via get_or_compute's double-check.
        """
        # Given
        n_threads = 8
        calls: list[str] = []
        results: list[dict] = []
        errors: list[Exception] = []
        results_lock = threading.Lock()
        barrier = threading.Barrier(n_threads + 1)  # +1 for the main thread
        release = threading.Event()

        def gated_fetch(service: str) -> dict:
            calls.append(service)  # winner-only
            release.wait(timeout=5.0)
            return {"state": "closed"}

        CBStateCache.configure(fetch_callback=gated_fetch)

        def worker() -> None:
            try:
                barrier.wait(timeout=5.0)
                state = CBStateCache.get_state("payment")
                with results_lock:
                    results.append(state)
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

        # Then - one command-center call total, every caller got its result
        assert errors == []
        assert calls == ["payment"]
        assert results == [{"state": "closed"}] * n_threads
