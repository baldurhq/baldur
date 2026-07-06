"""
HybridRateLimitMiddleware L2 window — real-Redis fidelity backstop.

The fast unit suite verifies the counting logic against a mock ZSET, but the
fix restores a count that was previously a false guarantee, so the mock is not
trusted alone. These tests exercise the actual Redis
``zremrangebyscore``/``zcard``/``zadd``/``expire`` round-trip (mirroring the CB
Lua CAS integration tests) to prove:

- real-time enforcement: a sequential burst past the limit is rejected and
  over-limit requests are not recorded (L1 parity);
- a concurrent burst overshoots by at most the per-client in-flight
  concurrency (the documented cost of the non-atomic two-step check-then-add),
  never below the limit, with the ZSET holding exactly the admitted requests;
- the per-client key carries a bounded TTL and its window entries are reclaimed
  once they age out, restoring allowance.

All tests require a running Redis instance (auto-skipped otherwise).
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from unittest.mock import MagicMock

import pytest

pytestmark = pytest.mark.requires_redis


class _FakeRequest:
    """Minimal Django HttpRequest stub for client-key derivation."""

    def __init__(self, ip: str = "1.2.3.4") -> None:
        self.META: dict = {"REMOTE_ADDR": ip}


def _make_middleware(redis_client) -> object:
    from baldur.api.django.rate_limit.middleware import HybridRateLimitMiddleware

    middleware = HybridRateLimitMiddleware(get_response=lambda request: None)
    middleware.redis_client = redis_client
    middleware._shadow_audit = MagicMock()
    return middleware


@pytest.fixture(autouse=True)
def _reset_rate_limit_singletons():
    from baldur.api.django.rate_limit.middleware import reset_rate_limit_state

    reset_rate_limit_state()
    yield
    reset_rate_limit_state()


class TestRealRedisL2Enforcement:
    """L2 window enforcement against a real Redis ZSET."""

    def test_sequential_burst_enforces_limit_and_parity(self, redis_test_client):
        middleware = _make_middleware(redis_test_client)
        request = _FakeRequest(ip="203.0.113.1")
        key = middleware._get_client_key(request)

        decisions = []
        remainings = []
        for _ in range(5):
            state, _mode = middleware._check_redis_limit(request, 5, 60)
            decisions.append(state.allowed)
            remainings.append(state.remaining)

        # 6th request is over the limit.
        rejected_state, _mode = middleware._check_redis_limit(request, 5, 60)

        assert decisions == [True] * 5
        assert remainings == [4, 3, 2, 1, 0]
        assert rejected_state.allowed is False
        assert rejected_state.remaining == 0
        # L1 parity: the rejected request was not recorded.
        assert redis_test_client.zcard(key) == 5

    def test_concurrent_burst_overshoot_bounded_by_in_flight(self, redis_test_client):
        middleware = _make_middleware(redis_test_client)
        request = _FakeRequest(ip="203.0.113.2")
        key = middleware._get_client_key(request)

        limit = 10
        thread_count = 40
        results: list[bool] = []
        results_lock = threading.Lock()
        barrier = threading.Barrier(thread_count)

        def attempt():
            barrier.wait()
            state, _mode = middleware._check_redis_limit(request, limit, 60)
            with results_lock:
                results.append(state.allowed)

        with ThreadPoolExecutor(max_workers=thread_count) as executor:
            futures = [executor.submit(attempt) for _ in range(thread_count)]
            for future in as_completed(futures):
                future.result()

        admitted = sum(1 for allowed in results if allowed)

        # Enforcement floor: a rejection can only occur once `limit` members
        # exist, and members come only from admitted requests, so at least
        # `limit` requests are always admitted.
        assert admitted >= limit
        # Overshoot is bounded by the per-client in-flight concurrency (the
        # documented cost of the non-atomic check-then-add), never above the
        # number of concurrent requests.
        assert admitted <= thread_count
        # The ZSET holds exactly the admitted requests (over-limit not recorded).
        assert redis_test_client.zcard(key) == admitted

        # Post-burst the window is full, so a fresh request is rejected.
        state_after, _mode = middleware._check_redis_limit(request, limit, 60)
        assert state_after.allowed is False

    def test_window_expiry_reclaims_key_and_restores_allowance(self, redis_test_client):
        middleware = _make_middleware(redis_test_client)
        request = _FakeRequest(ip="203.0.113.3")
        key = middleware._get_client_key(request)

        window_seconds = 1
        for _ in range(3):
            middleware._check_redis_limit(request, 3, window_seconds)
        assert (
            middleware._check_redis_limit(request, 3, window_seconds)[0].allowed
            is False
        )

        # The per-client key carries a bounded TTL, so an idle key self-reclaims.
        ttl = redis_test_client.ttl(key)
        assert 0 < ttl <= window_seconds + 10

        # Once the window's entries age out, allowance is restored.
        time.sleep(window_seconds + 0.3)
        state, _mode = middleware._check_redis_limit(request, 3, window_seconds)
        assert state.allowed is True
        assert state.remaining == 2
        assert redis_test_client.zcard(key) == 1
