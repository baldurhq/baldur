"""Unit tests for ``baldur.api.middleware.rate_limit`` (PR4).

Scope:
    - ``_client_key``: bucket-key composition across anonymous / authenticated
      / forwarded-for shapes (Contract).
    - ``SlidingWindowLimiter``: sliding-window boundary behavior
      (limit-1 / limit / limit+1), peek non-consuming semantics, reset.
    - ``check_rate_limit``: allow/reject decision + rejection
      ``ResponseContext`` shape (Contract on headers), disabled-default
      invariant, kwarg-override precedence.
    - ``apply_rate_limit_headers``: success-side header key set and the
      "peek does not consume" invariant (Contract + Behavior).

All tests pass ``rate_limit`` / ``window_seconds`` kwargs explicitly where
rate-limiting is exercised, keeping the tests independent of the
``BALDUR_RATE_LIMIT_MIDDLEWARE_*`` env-var singleton. The default-disabled
(limit=0) behavior is verified via the no-kwarg path.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from baldur.api.middleware.rate_limit import (
    _client_key,
    apply_rate_limit_headers,
    build_rate_limit_headers,
    check_rate_limit,
    reset_rate_limit_state,
)
from baldur.interfaces.web_framework import (
    HttpMethod,
    RequestContext,
    ResponseContext,
)
from baldur.services.rate_limit import SlidingWindowLimiter
from baldur.settings.rate_limit import reset_rate_limit_settings

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture(autouse=True)
def _isolated_rate_limit_state():
    """Reset the module-level limiter + rate-limit settings between tests.

    Required because ``rate_limit._limiter`` is a module-level singleton and
    ``get_rate_limit_settings`` caches a ``RateLimitSettings`` instance; without
    isolation the sliding-window counts and env overrides leak across tests
    under ``-n 6``.
    """
    reset_rate_limit_state()
    reset_rate_limit_settings()
    yield
    reset_rate_limit_state()
    reset_rate_limit_settings()


def _make_request(
    *,
    client_ip: str | None = "203.0.113.5",
    user=None,
    headers: dict[str, str] | None = None,
) -> RequestContext:
    return RequestContext(
        method=HttpMethod.GET,
        path="/api/resource/",
        headers=headers or {},
        query_params={},
        path_params={},
        body=None,
        json_body=None,
        user=user,
        is_authenticated=user is not None,
        client_ip=client_ip,
    )


# =============================================================================
# build_rate_limit_headers — Contract (single canonical header set)
# =============================================================================


class TestBuildRateLimitHeaders:
    """The shared builder is the single definition point of the header set.

    Every OSS 429 / success emit site (B and D) constructs its headers
    through this function, so the field set cannot drift between paths.
    """

    def test_base_set_is_the_three_canonical_headers(self):
        """No optional args -> exactly the three X-RateLimit-* headers."""
        headers = build_rate_limit_headers(100, 42, 1700)
        assert headers == {
            "X-RateLimit-Limit": "100",
            "X-RateLimit-Remaining": "42",
            "X-RateLimit-Reset": "1700",
        }

    def test_retry_after_adds_the_429_only_header(self):
        """``retry_after`` present -> 429 shape adds Retry-After."""
        headers = build_rate_limit_headers(100, 0, 1700, retry_after=9)
        assert headers["Retry-After"] == "9"
        assert set(headers) == {
            "X-RateLimit-Limit",
            "X-RateLimit-Remaining",
            "X-RateLimit-Reset",
            "Retry-After",
        }

    def test_mode_adds_the_d_only_extension(self):
        """``mode`` present -> adds the documented D-only X-RateLimit-Mode."""
        headers = build_rate_limit_headers(100, 5, 1700, mode="emergency")
        assert headers["X-RateLimit-Mode"] == "emergency"
        assert "Retry-After" not in headers

    def test_all_values_are_strings(self):
        """Header values must be strings for both response backends."""
        headers = build_rate_limit_headers(10, 3, 1700, retry_after=2, mode="normal")
        assert all(isinstance(v, str) for v in headers.values())

    @pytest.mark.parametrize("retry_after", [None, 5])
    @pytest.mark.parametrize("mode", [None, "normal", "emergency"])
    def test_key_set_matches_optional_additions(self, retry_after, mode):
        """The key set is the base three plus each present optional addition."""
        expected = {
            "X-RateLimit-Limit",
            "X-RateLimit-Remaining",
            "X-RateLimit-Reset",
        }
        if retry_after is not None:
            expected.add("Retry-After")
        if mode is not None:
            expected.add("X-RateLimit-Mode")

        headers = build_rate_limit_headers(
            100, 0, 1700, retry_after=retry_after, mode=mode
        )
        assert set(headers) == expected


# =============================================================================
# _client_key — Contract
# =============================================================================


class TestClientKeyContract:
    """Stable bucket-key composition is the rate-limit contract.

    Changing the key format shifts which requests share a quota — a
    cross-framework-visible behavior change.
    """

    def test_anonymous_user_composes_ip_and_anonymous(self):
        ctx = _make_request(client_ip="10.0.0.1", user=None)
        assert _client_key(ctx) == "ratelimit:10.0.0.1:anonymous"

    def test_authenticated_user_composes_ip_and_user_id(self):
        """``user.id`` is preferred over ``user.pk`` when both exist."""
        ctx = _make_request(
            client_ip="10.0.0.1",
            user=SimpleNamespace(id=42, pk=999),
        )
        assert _client_key(ctx) == "ratelimit:10.0.0.1:42"

    def test_authenticated_user_without_id_falls_back_to_pk(self):
        """Django User → ``pk`` attribute is the fallback identity."""
        ctx = _make_request(
            client_ip="10.0.0.1",
            user=SimpleNamespace(pk=77),
        )
        assert _client_key(ctx) == "ratelimit:10.0.0.1:77"

    def test_user_without_id_or_pk_falls_back_to_anonymous(self):
        """Defence in depth: user proxies missing both keys → anonymous."""
        ctx = _make_request(client_ip="10.0.0.1", user=SimpleNamespace())
        assert _client_key(ctx) == "ratelimit:10.0.0.1:anonymous"

    def test_missing_client_ip_uses_unknown_literal(self):
        """``client_ip=None`` must NOT raise — substitutes 'unknown'."""
        ctx = _make_request(client_ip=None, user=None)
        assert _client_key(ctx) == "ratelimit:unknown:anonymous"

    def test_user_with_zero_id_is_not_falsy_coerced(self):
        """``id=0`` is a valid integer identity — must not fall back to pk."""
        ctx = _make_request(
            client_ip="10.0.0.1",
            user=SimpleNamespace(id=0, pk=999),
        )
        assert _client_key(ctx) == "ratelimit:10.0.0.1:0"


# =============================================================================
# SlidingWindowLimiter — Behavior
# =============================================================================


class TestSlidingWindowLimiterBehavior:
    """Sliding-window allow/reject behavior at the boundary of ``max_requests``."""

    def test_allows_requests_below_limit(self):
        limiter = SlidingWindowLimiter()
        for _ in range(4):
            state = limiter.check("k", max_requests=5, window_seconds=60)
            assert state.allowed is True

    def test_rejects_at_exact_limit(self):
        """The N+1-th request within the window is rejected; remaining == 0."""
        limiter = SlidingWindowLimiter()
        for _ in range(5):
            limiter.check("k", max_requests=5, window_seconds=60)
        state = limiter.check("k", max_requests=5, window_seconds=60)
        assert state.allowed is False
        assert state.remaining == 0
        assert state.limit == 5

    def test_allow_decrements_remaining_monotonically(self):
        """Each allowed call drops ``remaining`` by exactly 1."""
        limiter = SlidingWindowLimiter()
        s1 = limiter.check("k", max_requests=3, window_seconds=60)
        s2 = limiter.check("k", max_requests=3, window_seconds=60)
        s3 = limiter.check("k", max_requests=3, window_seconds=60)
        assert (s1.remaining, s2.remaining, s3.remaining) == (2, 1, 0)

    def test_distinct_keys_have_independent_buckets(self):
        """Bucket isolation: one client exhausting quota does not affect others."""
        limiter = SlidingWindowLimiter()
        for _ in range(5):
            limiter.check("client_a", max_requests=5, window_seconds=60)
        state_b = limiter.check("client_b", max_requests=5, window_seconds=60)
        assert state_b.allowed is True

    def test_peek_does_not_consume_quota(self):
        """``peek`` is idempotent — 10 peeks don't exhaust a 3-req quota."""
        limiter = SlidingWindowLimiter()
        for _ in range(10):
            limiter.peek("k", max_requests=3, window_seconds=60)
        # Quota should still be fully available
        state = limiter.check("k", max_requests=3, window_seconds=60)
        assert state.allowed is True
        assert state.remaining == 2

    def test_reset_clears_all_buckets(self):
        """``reset`` is the test-isolation escape hatch."""
        limiter = SlidingWindowLimiter()
        for _ in range(5):
            limiter.check("k", max_requests=5, window_seconds=60)
        limiter.reset()
        state = limiter.check("k", max_requests=5, window_seconds=60)
        assert state.allowed is True
        assert state.remaining == 4


# =============================================================================
# check_rate_limit — Contract (rejection shape)
# =============================================================================


class TestCheckRateLimitContract:
    """Reject-path ``ResponseContext`` must carry the RFC 6585 envelope."""

    def test_default_setting_disables_rate_limiting(self):
        """``middleware_rate_limit=0`` (default) → helper is a no-op."""
        ctx = _make_request()
        # No kwargs, default settings — must allow any number of requests.
        for _ in range(200):
            assert check_rate_limit(ctx) is None

    def test_returns_none_when_within_limit(self):
        """Explicit kwargs below threshold → allowed."""
        assert (
            check_rate_limit(_make_request(), rate_limit=5, window_seconds=60) is None
        )

    def test_rejection_uses_http_429(self):
        """Status code 429 is the protocol contract — not a config knob."""
        ctx = _make_request()
        check_rate_limit(ctx, rate_limit=1, window_seconds=60)  # consume quota
        response = check_rate_limit(ctx, rate_limit=1, window_seconds=60)
        assert isinstance(response, ResponseContext)
        assert response.status_code == 429

    def test_rejection_headers_include_all_rate_limit_keys(self):
        """Rejection must emit Retry-After + X-RateLimit-Limit/Remaining/Reset."""
        ctx = _make_request()
        check_rate_limit(ctx, rate_limit=1, window_seconds=60)
        response = check_rate_limit(ctx, rate_limit=1, window_seconds=60)
        assert set(response.headers).issuperset(
            {
                "Retry-After",
                "X-RateLimit-Limit",
                "X-RateLimit-Remaining",
                "X-RateLimit-Reset",
            }
        )
        assert response.headers["X-RateLimit-Remaining"] == "0"

    def test_rejection_retry_after_is_positive_int(self):
        ctx = _make_request()
        check_rate_limit(ctx, rate_limit=1, window_seconds=60)
        response = check_rate_limit(ctx, rate_limit=1, window_seconds=60)
        assert int(response.headers["Retry-After"]) >= 1


# =============================================================================
# check_rate_limit — Behavior (limit sourced from settings)
# =============================================================================


class TestCheckRateLimitBehavior:
    """Allow/reject decision respects the resolved rate-limit value."""

    def test_env_var_enables_rate_limiting(self, monkeypatch):
        """Setting ``BALDUR_RATE_LIMIT_MIDDLEWARE_RATE_LIMIT`` enables helpers."""
        monkeypatch.setenv("BALDUR_RATE_LIMIT_MIDDLEWARE_RATE_LIMIT", "2")
        reset_rate_limit_settings()
        ctx = _make_request()
        assert check_rate_limit(ctx) is None
        assert check_rate_limit(ctx) is None
        # 3rd call exceeds the limit
        response = check_rate_limit(ctx)
        assert isinstance(response, ResponseContext)
        assert response.status_code == 429

    def test_kwarg_overrides_settings(self, monkeypatch):
        """Per-instance ``rate_limit`` kwarg wins over the env-derived setting."""
        monkeypatch.setenv("BALDUR_RATE_LIMIT_MIDDLEWARE_RATE_LIMIT", "100")
        reset_rate_limit_settings()
        ctx = _make_request()
        check_rate_limit(ctx, rate_limit=1, window_seconds=60)
        response = check_rate_limit(ctx, rate_limit=1, window_seconds=60)
        assert response is not None
        assert response.status_code == 429

    def test_reject_envelope_mentions_retry_after(self):
        """Body must carry the retry_after field for non-header-aware clients."""
        ctx = _make_request()
        check_rate_limit(ctx, rate_limit=1, window_seconds=60)
        response = check_rate_limit(ctx, rate_limit=1, window_seconds=60)
        assert response.body["error"] == "rate_limit_exceeded"
        assert response.body["retry_after"] == int(response.headers["Retry-After"])


# =============================================================================
# apply_rate_limit_headers — Contract + Behavior
# =============================================================================


class TestApplyRateLimitHeadersContract:
    """Success-side header injection must not leak the reject-response keys."""

    def test_injects_three_rate_limit_headers_when_enabled(self):
        headers: dict[str, str] = {}
        apply_rate_limit_headers(
            headers, _make_request(), rate_limit=10, window_seconds=60
        )
        assert set(headers) == {
            "X-RateLimit-Limit",
            "X-RateLimit-Remaining",
            "X-RateLimit-Reset",
        }

    def test_noop_when_rate_limiting_disabled(self):
        """Default setting (0) → helper does not inject any header."""
        headers: dict[str, str] = {"X-Existing": "keep"}
        apply_rate_limit_headers(headers, _make_request())
        assert headers == {"X-Existing": "keep"}

    def test_does_not_inject_retry_after(self):
        """Retry-After is a 429-only header — must not appear on success responses."""
        headers: dict[str, str] = {}
        apply_rate_limit_headers(
            headers, _make_request(), rate_limit=10, window_seconds=60
        )
        assert "Retry-After" not in headers


class TestApplyRateLimitHeadersBehavior:
    def test_preserves_existing_headers(self):
        """Merging must not clobber unrelated headers the adapter already set."""
        headers = {"Content-Type": "application/json", "X-Trace-Id": "abc"}
        apply_rate_limit_headers(
            headers, _make_request(), rate_limit=10, window_seconds=60
        )
        assert headers["Content-Type"] == "application/json"
        assert headers["X-Trace-Id"] == "abc"

    def test_apply_is_idempotent_across_calls(self):
        """Twice-applying headers yields the same values (peek is non-consuming)."""
        ctx = _make_request()
        h1: dict[str, str] = {}
        apply_rate_limit_headers(h1, ctx, rate_limit=10, window_seconds=60)
        h2: dict[str, str] = {}
        apply_rate_limit_headers(h2, ctx, rate_limit=10, window_seconds=60)
        assert h1 == h2

    def test_apply_reflects_consumed_quota(self):
        """After 2 check_rate_limit calls, Remaining decreases by 2."""
        ctx = _make_request()
        check_rate_limit(ctx, rate_limit=10, window_seconds=60)
        check_rate_limit(ctx, rate_limit=10, window_seconds=60)
        headers: dict[str, str] = {}
        apply_rate_limit_headers(headers, ctx, rate_limit=10, window_seconds=60)
        assert int(headers["X-RateLimit-Remaining"]) == 10 - 2
