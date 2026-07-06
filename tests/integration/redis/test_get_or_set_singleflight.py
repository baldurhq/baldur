"""
Redis-backed get_or_set distributed singleflight integration tests (doc 594 D3).

Verifies the infra-dependent contracts that InMemory mocks cannot prove:
real RedisDistributedLock mutual exclusion electing exactly one winner
across adapter instances (simulated processes), real setex visibility
ordering between the winner's set and the losers' value polls, and lock
TTL self-expiry backing crashed-winner takeover.

Test Categories:
    A. Read-through basics:
        - miss computes once and stores with TTL
        - hit returns cached without running the factory
    B. Concurrent dedup across adapter instances:
        - one factory run total, every caller shares the value
    C. Loser value-poll visibility:
        - a lock-blocked loser returns the winner's set without computing
    D. Crashed-winner takeover:
        - lock TTL expiry lets a polling loser take over and compute
    E. wait_timeout fail-open:
        - a loser that never sees a value computes anyway (bounded duplication)

Note: All tests require a running Redis instance.
      Marked with @pytest.mark.requires_redis for auto-skip.
"""

from __future__ import annotations

import threading
from datetime import timedelta

import pytest

from baldur.adapters.cache.redis_adapter import RedisCacheAdapter

pytestmark = pytest.mark.requires_redis

KEY_PREFIX = "test:sf:"


def _make_adapter(redis_url: str) -> RedisCacheAdapter:
    """One adapter instance == one simulated process (own miss funnel)."""
    return RedisCacheAdapter(
        url=redis_url,
        key_prefix=KEY_PREFIX,
        socket_timeout=5.0,
        socket_connect_timeout=5.0,
    )


@pytest.fixture
def cache(redis_url) -> RedisCacheAdapter:
    """Primary adapter connected to test Redis."""
    return _make_adapter(redis_url)


# =============================================================================
# A. Read-through basics
# =============================================================================


class TestGetOrSetReadThroughBasics:
    """Single-caller read-through against real Redis."""

    def test_miss_computes_once_and_stores_with_ttl(self, cache):
        """
        Purpose:
            Verify a miss runs the factory once and stores the value with
            the requested TTL on real Redis.
        Expected:
            - factory runs exactly once
            - the value is readable afterwards
            - the Redis TTL reflects the requested ttl
        """
        factory_calls: list[int] = []

        def factory() -> dict:
            factory_calls.append(1)
            return {"value": 42}

        result = cache.get_or_set("ro-key", factory, ttl=timedelta(seconds=30))

        assert result == {"value": 42}
        assert factory_calls == [1]
        assert cache.get("ro-key") == {"value": 42}
        remaining = cache.ttl("ro-key")
        assert remaining is not None
        assert 25 <= remaining <= 30

    def test_hit_returns_cached_without_running_factory(self, cache):
        """
        Purpose:
            Verify the fast-path hit never enters the miss dance.
        Expected:
            - cached value returned, factory not invoked
        """
        cache.set("ro-key", {"cached": True}, ttl=timedelta(seconds=30))
        factory_calls: list[int] = []

        def factory() -> dict:
            factory_calls.append(1)
            return {"cached": False}

        assert cache.get_or_set("ro-key", factory) == {"cached": True}
        assert factory_calls == []


# =============================================================================
# B. Concurrent dedup across adapter instances
# =============================================================================


class TestGetOrSetCrossInstanceDedup:
    """The distributed lock elects one winner across adapter instances."""

    def test_concurrent_instances_run_factory_exactly_once(self, redis_url):
        """
        Purpose:
            Verify N adapter instances (each with its own in-process
            funnel, simulating N worker processes) racing on one missing
            key elect exactly one winner through the real Redis lock.
        Expected:
            - the factory runs exactly once
            - every caller returns the winner's value (losers via the
              value poll or the post-release double-check)
        """
        # Given
        n_callers = 5
        adapters = [_make_adapter(redis_url) for _ in range(n_callers)]
        factory_calls: list[int] = []
        results: list[dict] = []
        errors: list[Exception] = []
        results_lock = threading.Lock()
        barrier = threading.Barrier(n_callers + 1)  # +1 for the main thread
        release = threading.Event()

        def gated_factory() -> dict:
            factory_calls.append(1)  # winner-only
            release.wait(timeout=5.0)
            return {"value": 42}

        def worker(adapter: RedisCacheAdapter) -> None:
            try:
                barrier.wait(timeout=5.0)
                value = adapter.get_or_set(
                    "hot-key",
                    gated_factory,
                    ttl=timedelta(seconds=30),
                    wait_timeout=15.0,
                )
                with results_lock:
                    results.append(value)
            except Exception as e:  # pragma: no cover - failure diagnostics
                with results_lock:
                    errors.append(e)

        # When
        threads = [threading.Thread(target=worker, args=(a,)) for a in adapters]
        for t in threads:
            t.start()
        barrier.wait(timeout=5.0)
        release.set()
        for t in threads:
            t.join(timeout=20.0)

        # Then
        assert errors == []
        assert len(factory_calls) == 1
        assert results == [{"value": 42}] * n_callers


# =============================================================================
# C. Loser value-poll visibility
# =============================================================================


class TestGetOrSetLoserValuePoll:
    """A lock-blocked loser observes the winner's setex through the poll."""

    def test_loser_returns_winner_value_without_computing(self, redis_url):
        """
        Purpose:
            Verify real setex visibility ordering: while the singleflight
            lock is held by a (simulated) winner, a polling loser returns
            the value the winner publishes, never running its own factory.
        Expected:
            - the loser returns the winner's value
            - the loser's factory is never invoked
        """
        # Given - the per-key singleflight lock is held by the "winner"
        winner = _make_adapter(redis_url)
        loser = _make_adapter(redis_url)
        blocker = winner.get_lock("singleflight:lock:hot-key")
        assert blocker.acquire(blocking=False) is True

        factory_calls: list[int] = []

        def factory() -> dict:
            factory_calls.append(1)
            return {"from": "loser"}

        results: list[dict] = []
        started = threading.Event()

        def caller() -> None:
            started.set()
            results.append(loser.get_or_set("hot-key", factory, wait_timeout=10.0))

        # When - the loser starts, then the winner's value lands
        try:
            t = threading.Thread(target=caller)
            t.start()
            assert started.wait(timeout=5.0)
            winner.set("hot-key", {"from": "winner"}, ttl=timedelta(seconds=30))
            t.join(timeout=15.0)
        finally:
            blocker.release()

        # Then
        assert results == [{"from": "winner"}]
        assert factory_calls == []


# =============================================================================
# D. Crashed-winner takeover via lock TTL expiry
# =============================================================================


class TestGetOrSetCrashedWinnerTakeover:
    """Lock TTL self-expiry replaces a crashed winner with the next poller."""

    def test_takeover_after_lock_ttl_expiry(self, cache):
        """
        Purpose:
            Verify a crashed winner (lock acquired with a short TTL, no
            value ever published, never released) is replaced: the Redis
            key expires and the polling loser's non-blocking acquire
            succeeds, making it the new winner.
        Expected:
            - the caller computes and returns the value within wait_timeout
            - the value is stored for subsequent readers
        """
        # Given - a 1s-TTL lock simulating a winner that died mid-compute
        crashed = cache.get_lock(
            "singleflight:lock:hot-key", timeout=timedelta(seconds=1)
        )
        assert crashed.acquire(blocking=False) is True

        factory_calls: list[int] = []

        def factory() -> dict:
            factory_calls.append(1)
            return {"recovered": True}

        # When - polling outlasts the crashed winner's lock TTL
        result = cache.get_or_set("hot-key", factory, wait_timeout=15.0)

        # Then
        assert result == {"recovered": True}
        assert factory_calls == [1]
        assert cache.get("hot-key") == {"recovered": True}


# =============================================================================
# E. wait_timeout fail-open
# =============================================================================


class TestGetOrSetWaitTimeoutFailOpen:
    """A loser that never sees a value computes anyway (R1 bounded duplication)."""

    def test_wait_timeout_expiry_computes_anyway(self, cache):
        """
        Purpose:
            Verify the fail-open bound: with the lock held (long TTL) and
            no value ever published, the loser computes after wait_timeout
            instead of erroring or blocking forever.
        Expected:
            - the factory runs once after the wait budget is exhausted
            - the computed value is returned and stored
        """
        # Given - a winner that holds a long-TTL lock and never publishes
        blocker = cache.get_lock(
            "singleflight:lock:hot-key", timeout=timedelta(seconds=30)
        )
        assert blocker.acquire(blocking=False) is True

        factory_calls: list[int] = []

        def factory() -> dict:
            factory_calls.append(1)
            return {"computed": "anyway"}

        # When
        try:
            result = cache.get_or_set("hot-key", factory, wait_timeout=1.0)
        finally:
            blocker.release()

        # Then
        assert result == {"computed": "anyway"}
        assert factory_calls == [1]
        assert cache.get("hot-key") == {"computed": "anyway"}
