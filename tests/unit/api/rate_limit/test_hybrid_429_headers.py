"""
Control-API 429 header parity + enforced-limit accuracy.

Path D (Django Control-API hybrid) previously omitted ``X-RateLimit-Limit`` on
its 429 and, on the Redis-exception fallback, mislabelled the mode/limit
(``mode="normal"`` while enforcement was emergency). These tests pin:

- D's 429 now carries ``X-RateLimit-Limit`` equal to the limit *actually*
  enforced — normal, emergency, and the Redis-exception fallback (emergency
  limit + ``mode=emergency``, not the normal limit);
- the 429 header key set is identical across path B (framework-free
  ``check_rate_limit``) and path D, modulo the documented D-only extension
  ``{X-RateLimit-Mode}``.

The comparison is over the rate-limit header namespace (``X-RateLimit-*`` +
``Retry-After``); framework-added headers (Content-Type on the Django
``JsonResponse``) are outside the rate-limit contract.
"""

from __future__ import annotations

import time
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


def _make_d_middleware(redis_client) -> object:
    from baldur.api.django.rate_limit.middleware import HybridRateLimitMiddleware

    middleware = HybridRateLimitMiddleware(get_response=lambda request: None)
    middleware.redis_client = redis_client
    middleware._shadow_audit = MagicMock()
    return middleware


def _future_reset() -> int:
    return int(time.time()) + 60


def _rate_limit_keys(headers) -> set[str]:
    """Rate-limit header namespace: X-RateLimit-* plus Retry-After."""
    return {k for k in headers if k.startswith("X-RateLimit") or k == "Retry-After"}


_CANONICAL_429_KEYS = {
    "X-RateLimit-Limit",
    "X-RateLimit-Remaining",
    "X-RateLimit-Reset",
    "Retry-After",
    "X-RateLimit-Mode",
}


@pytest.fixture(autouse=True)
def _reset_limiters():
    """Reset both the D (hybrid L1) and B (framework-free) limiter singletons."""
    from baldur.api.django.rate_limit.middleware import (
        reset_rate_limit_state as reset_d_state,
    )
    from baldur.api.middleware.rate_limit import (
        reset_rate_limit_state as reset_b_state,
    )

    reset_d_state()
    reset_b_state()
    yield
    reset_d_state()
    reset_b_state()


class TestD429IncludesEnforcedLimit:
    """D's 429 carries X-RateLimit-Limit equal to the enforced limit."""

    def test_normal_mode_429_reports_normal_limit(self):
        from baldur.services.rate_limit import RateLimitState

        middleware = _make_d_middleware(MockRedisClient())
        state = RateLimitState(
            limit=100, remaining=0, reset_at=_future_reset(), allowed=False
        )

        response = middleware._rate_limit_response(state, "normal")

        assert response.status_code == 429
        assert response["X-RateLimit-Limit"] == "100"
        assert response["X-RateLimit-Remaining"] == "0"
        assert response["X-RateLimit-Mode"] == "normal"
        assert _rate_limit_keys(response.headers) == _CANONICAL_429_KEYS

    def test_emergency_mode_429_reports_emergency_limit(self):
        from baldur.services.rate_limit import RateLimitState

        middleware = _make_d_middleware(MockRedisClient())
        state = RateLimitState(
            limit=10, remaining=0, reset_at=_future_reset(), allowed=False
        )

        response = middleware._rate_limit_response(state, "emergency")

        assert response["X-RateLimit-Limit"] == "10"
        assert response["X-RateLimit-Mode"] == "emergency"

    def test_redis_exception_fallback_429_reports_emergency_not_normal(
        self, monkeypatch
    ):
        """The self-pacing bug: the fallback enforces emergency but must not
        advertise the normal limit. The 429 reports the emergency limit."""
        from baldur.api.django.rate_limit import middleware as d_middleware

        # A low emergency limit so the L1 fallback rejects quickly, and a
        # distinct normal limit so a mislabel would be visible.
        monkeypatch.setattr(
            d_middleware,
            "get_rate_limit_config",
            lambda: {
                "control_api_rate_limit": 100,
                "control_api_window_seconds": 60,
                "emergency_rate_limit": 2,
                "emergency_window_seconds": 60,
            },
        )

        middleware = _make_d_middleware(_BoomRedis())
        request = _FakeRequest(ip="10.0.0.9")

        # Exhaust the emergency (L1) allowance of 2 via the exception fallback.
        for _ in range(2):
            middleware._check_redis_limit(request, 100, 60)
        state, mode = middleware._check_redis_limit(request, 100, 60)

        assert state.allowed is False
        assert mode == "emergency"

        response = middleware._rate_limit_response(state, mode)

        # Reports the EMERGENCY limit actually enforced, not the normal 100.
        assert response["X-RateLimit-Limit"] == "2"
        assert response["X-RateLimit-Mode"] == "emergency"


class TestBDHeaderParity:
    """B and D 429 header key sets are identical modulo the D-only extension."""

    def _b_429(self):
        from baldur.api.middleware.rate_limit import check_rate_limit
        from baldur.interfaces.web_framework import HttpMethod, RequestContext

        request = RequestContext(
            method=HttpMethod.GET,
            path="/api/resource/",
            headers={},
            query_params={},
            path_params={},
            body=None,
            json_body=None,
            user=None,
            is_authenticated=False,
            client_ip="203.0.113.9",
        )
        check_rate_limit(request, rate_limit=1, window_seconds=60)  # consume quota
        response = check_rate_limit(request, rate_limit=1, window_seconds=60)
        assert response is not None
        assert response.status_code == 429
        return response

    def _d_429(self):
        from baldur.services.rate_limit import RateLimitState

        middleware = _make_d_middleware(MockRedisClient())
        state = RateLimitState(
            limit=100, remaining=0, reset_at=_future_reset(), allowed=False
        )
        return middleware._rate_limit_response(state, "normal")

    def test_429_key_sets_parity_modulo_mode(self):
        b_keys = _rate_limit_keys(self._b_429().headers)
        d_keys = _rate_limit_keys(self._d_429().headers)

        # D adds exactly one documented extension (X-RateLimit-Mode).
        assert d_keys - {"X-RateLimit-Mode"} == b_keys
        assert "X-RateLimit-Mode" in d_keys
        assert "X-RateLimit-Mode" not in b_keys

    def test_both_carry_x_ratelimit_limit(self):
        """The header G3 restored — present on both paths' 429."""
        assert "X-RateLimit-Limit" in _rate_limit_keys(self._b_429().headers)
        assert "X-RateLimit-Limit" in _rate_limit_keys(self._d_429().headers)
