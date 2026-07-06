"""
TTLCacheStrategy unit tests — TTLCacheBase composition verification.

Verifies that TTLCacheStrategy correctly delegates to TTLCacheBase
after the #362 functional deduplication migration.

Verification techniques:
- Dependency interaction: callback invocation (§8.5)
- Time dependency: TTL expiry via freeze_time (§8.11)
- Idempotency: cache hit avoids duplicate callback (§8.3)
- Concurrency: same-target miss dedup via get_or_compute (§8.7, doc 594 D6)
"""

from __future__ import annotations

import threading

from baldur.adapters.health_checker import TTLCacheStrategy
from baldur.core.ttl_cache import TTLCacheBase
from tests.factories.time_helpers import freeze_time


class TestTTLCacheStrategyCompositionContract:
    """TTLCacheStrategy composes TTLCacheBase internally."""

    def test_internal_cache_is_ttl_cache_base(self):
        """Internal _ttl_cache is a TTLCacheBase[str, bool] instance."""
        strategy = TTLCacheStrategy(ttl=10.0)
        assert isinstance(strategy._ttl_cache, TTLCacheBase)

    def test_strategy_name_is_ttl_cache_strategy(self):
        """get_name() returns 'TTLCacheStrategy'."""
        strategy = TTLCacheStrategy(ttl=10.0)
        assert strategy.get_name() == "TTLCacheStrategy"


class TestTTLCacheStrategyBehavior:
    """TTLCacheStrategy check/invalidate behavior."""

    def test_check_calls_callback_on_miss(self):
        """Cache miss triggers the check_callback."""
        called_with: list[str] = []

        def mock_check(target: str) -> bool:
            called_with.append(target)
            return True

        strategy = TTLCacheStrategy(check_callback=mock_check, ttl=30.0)

        with freeze_time("2026-03-19 10:00:00"):
            result = strategy.check("localhost:8080")

        assert result is True
        assert called_with == ["localhost:8080"]

    def test_check_returns_cached_on_hit(self):
        """Second check returns cached value without calling callback again."""
        call_count = [0]

        def mock_check(target: str) -> bool:
            call_count[0] += 1
            return True

        strategy = TTLCacheStrategy(check_callback=mock_check, ttl=30.0)

        with freeze_time("2026-03-19 10:00:00"):
            strategy.check("target")
            strategy.check("target")

        assert call_count[0] == 1

    def test_check_rechecks_after_ttl_expiry(self):
        """After TTL expires, callback is invoked again."""
        call_count = [0]

        def mock_check(target: str) -> bool:
            call_count[0] += 1
            return True

        strategy = TTLCacheStrategy(check_callback=mock_check, ttl=10.0)

        with freeze_time("2026-03-19 10:00:00"):
            strategy.check("target")

        with freeze_time("2026-03-19 10:00:11"):
            strategy.check("target")

        assert call_count[0] == 2

    def test_check_without_callback_returns_true(self):
        """No callback configured returns True (healthy assumption)."""
        strategy = TTLCacheStrategy(ttl=10.0)
        assert strategy.check("target") is True

    def test_check_callback_exception_returns_false(self):
        """Callback exception results in False (unhealthy)."""

        def failing_check(target: str) -> bool:
            raise ConnectionError("unreachable")

        strategy = TTLCacheStrategy(check_callback=failing_check, ttl=30.0)
        result = strategy.check("target")
        assert result is False

    def test_invalidate_forces_recheck(self):
        """invalidate() forces next check to call callback."""
        call_count = [0]

        def mock_check(target: str) -> bool:
            call_count[0] += 1
            return True

        strategy = TTLCacheStrategy(check_callback=mock_check, ttl=60.0)

        with freeze_time("2026-03-19 10:00:00"):
            strategy.check("target")
            assert call_count[0] == 1

            strategy.invalidate("target")
            strategy.check("target")
            assert call_count[0] == 2

    def test_invalidate_all_clears_all_entries(self):
        """invalidate_all() clears all cached entries."""
        call_count = [0]

        def mock_check(target: str) -> bool:
            call_count[0] += 1
            return True

        strategy = TTLCacheStrategy(check_callback=mock_check, ttl=60.0)

        with freeze_time("2026-03-19 10:00:00"):
            strategy.check("a")
            strategy.check("b")
            assert call_count[0] == 2

            strategy.invalidate_all()
            strategy.check("a")
            strategy.check("b")
            assert call_count[0] == 4

    def test_configure_replaces_callback_and_ttl(self):
        """configure() replaces both callback and TTL."""
        first_calls = [0]
        second_calls = [0]

        def first_check(target: str) -> bool:
            first_calls[0] += 1
            return True

        def second_check(target: str) -> bool:
            second_calls[0] += 1
            return False

        strategy = TTLCacheStrategy(check_callback=first_check, ttl=30.0)

        with freeze_time("2026-03-19 10:00:00"):
            strategy.check("target")
            assert first_calls[0] == 1

        strategy.configure(check_callback=second_check, ttl=5.0)

        with freeze_time("2026-03-19 10:00:00"):
            result = strategy.check("target")

        assert result is False
        assert second_calls[0] == 1


class TestTTLCacheStrategyDedupBehavior:
    """Concurrent same-target miss dedup via get_or_compute (doc 594 D6)."""

    def test_concurrent_checks_invoke_callback_exactly_once(self):
        """N threads checking one target -> the check callback runs once.

        Deterministic regardless of scheduling: overlapping callers share
        the winner's Future; a late arrival hits the cached result via
        get_or_compute's double-check.
        """
        # Given
        n_threads = 8
        calls: list[str] = []
        results: list[bool] = []
        errors: list[Exception] = []
        results_lock = threading.Lock()
        barrier = threading.Barrier(n_threads + 1)  # +1 for the main thread
        release = threading.Event()

        def gated_check(target: str) -> bool:
            calls.append(target)  # winner-only
            release.wait(timeout=5.0)
            return True

        strategy = TTLCacheStrategy(check_callback=gated_check, ttl=30.0)

        def worker() -> None:
            try:
                barrier.wait(timeout=5.0)
                healthy = strategy.check("localhost:8080")
                with results_lock:
                    results.append(healthy)
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

        # Then - one real health check total, every caller got its verdict
        assert errors == []
        assert calls == ["localhost:8080"]
        assert results == [True] * n_threads

    def test_callback_exception_caches_false_without_recheck(self):
        """A failed check maps to False AND the False is cached (as before).

        False is a legitimate cached value in TTLCacheBase - only None is
        the miss sentinel - so the unhealthy verdict must not trigger a
        duplicate probe on the next call within the TTL.
        """
        calls: list[int] = []

        def failing_check(target: str) -> bool:
            calls.append(1)
            raise ConnectionError("unreachable")

        strategy = TTLCacheStrategy(check_callback=failing_check, ttl=30.0)

        with freeze_time("2026-03-19 10:00:00"):
            first = strategy.check("target")
            second = strategy.check("target")

        assert first is False
        assert second is False
        assert len(calls) == 1  # cached False served the second call
