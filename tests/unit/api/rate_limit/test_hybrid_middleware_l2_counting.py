"""
HybridRateLimitMiddleware L2 (Redis) window counting — fast regression tests.

Guards the fix that made the Control-API L2 sliding window count *requests*
instead of *distinct seconds*. Before the fix the ZSET member was
``str(int(time.time()))``, so every request within the same wall-clock second
collapsed into one member — a 60s window could never hold more than ~61
members while the default limit is 100, so the advertised "100 req/min"
protection never tripped while Redis was healthy.

The fix records one unique member per ALLOWED request (``uuid4().hex``) and adds
only when under the limit, mirroring the L1 ``SlidingWindowLimiter`` semantics
(over-limit requests are not recorded). These tests run always-on in the fast
unit suite against the ``tests.factories`` mock ZSET; the real-Redis fidelity
backstop lives in ``tests/integration/redis/``.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from tests.factories import MockRedisClient


class _FakeRequest:
    """Minimal Django HttpRequest stub for client-key derivation."""

    def __init__(self, ip: str = "1.2.3.4") -> None:
        self.META: dict = {"REMOTE_ADDR": ip}


class _BoomRedis:
    """Redis client whose pipeline call raises, to exercise the L1 fallback."""

    def pipeline(self, transaction: bool = True):
        raise ConnectionError("simulated redis outage")


def _make_middleware(redis_client) -> object:
    from baldur.api.django.rate_limit.middleware import HybridRateLimitMiddleware

    middleware = HybridRateLimitMiddleware(get_response=lambda request: None)
    middleware.redis_client = redis_client
    # Isolate the fallback path from real shadow-audit side effects.
    middleware._shadow_audit = MagicMock()
    return middleware


@pytest.fixture(autouse=True)
def _reset_rate_limit_singletons():
    """Reset the shared L1 limiter / health-checker singletons around each test."""
    from baldur.api.django.rate_limit.middleware import reset_rate_limit_state

    reset_rate_limit_state()
    yield
    reset_rate_limit_state()


class TestL2WindowCounting:
    """The L2 window must count requests, not distinct seconds."""

    def test_under_limit_allows_and_decrements_remaining(self):
        middleware = _make_middleware(MockRedisClient())
        request = _FakeRequest(ip="10.0.0.1")

        state_1, _mode_1 = middleware._check_redis_limit(request, 3, 60)
        state_2, _mode_2 = middleware._check_redis_limit(request, 3, 60)

        assert state_1.allowed is True
        assert state_1.remaining == 2
        assert state_2.allowed is True
        assert state_2.remaining == 1

    def test_same_second_burst_beyond_limit_returns_rejection(self):
        """The regression case: a same-second burst past the limit must reject.

        Pre-fix, all four requests landed in the same ``str(int(time.time()))``
        member, so the count never exceeded 1 and the burst was fully allowed.
        """
        middleware = _make_middleware(MockRedisClient())
        request = _FakeRequest(ip="10.0.0.2")

        decisions = [
            middleware._check_redis_limit(request, 3, 60)[0].allowed for _ in range(4)
        ]

        assert decisions == [True, True, True, False]

    def test_over_limit_requests_are_not_recorded(self):
        """L1 parity: rejected requests are not appended to the ZSET."""
        redis_client = MockRedisClient()
        middleware = _make_middleware(redis_client)
        request = _FakeRequest(ip="10.0.0.3")
        key = middleware._get_client_key(request)

        for _ in range(3):
            middleware._check_redis_limit(request, 3, 60)
        # Three rejected over-limit attempts must not grow the ZSET.
        for _ in range(3):
            assert middleware._check_redis_limit(request, 3, 60)[0].allowed is False

        assert redis_client.zcard(key) == 3

    def test_window_expiry_restores_allowance(self):
        """Members outside the window are pruned, restoring capacity."""
        redis_client = MockRedisClient()
        middleware = _make_middleware(redis_client)
        request = _FakeRequest(ip="10.0.0.4")
        key = middleware._get_client_key(request)

        for _ in range(3):
            middleware._check_redis_limit(request, 3, 60)
        assert middleware._check_redis_limit(request, 3, 60)[0].allowed is False

        # Age every member out of the window (score 0 is far below window_start).
        redis_client._zsets[key] = dict.fromkeys(redis_client._zsets[key], 0.0)

        state, _mode = middleware._check_redis_limit(request, 3, 60)
        assert state.allowed is True
        assert state.remaining == 2
        assert redis_client.zcard(key) == 1

    def test_redis_error_falls_back_to_local_limiter(self):
        """A Redis failure routes to the L1 emergency limiter (fail-open)."""
        middleware = _make_middleware(_BoomRedis())
        request = _FakeRequest(ip="10.0.0.5")

        state, mode = middleware._check_redis_limit(request, 3, 60)

        assert state.allowed is True
        # The Redis-exception fallback labels the enforced mode as emergency.
        assert mode == "emergency"
        # The fallback logs a shadow-audit event for forensic analysis.
        assert middleware._shadow_audit.log_rate_limit_event.called

    def test_no_redis_client_fails_open(self):
        middleware = _make_middleware(None)
        request = _FakeRequest(ip="10.0.0.6")

        state, _mode = middleware._check_redis_limit(request, 3, 60)

        assert state.allowed is True
        assert state.remaining == 3
        assert state.reset_at == 0
