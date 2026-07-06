"""Unit tests for ``baldur.decorators.rate_limit`` (#458 §D2, §D5, §D7, §D8).

Verification techniques applied:
- Contract: decoration-time TypeError on missing/invalid ``max_requests``
  and ``window_seconds`` (boundary).
- Behavior: state_transition / boundary_analysis — Nth call passes,
  N+1 rejects; ``raise_on_limit=False`` returns None on rejection.
- Behavior: dependency_interaction — D5 ``decorator_enabled=False`` short
  circuits without consulting ``SlidingWindowLimiter``.
- Behavior: D7 limiter sharing — distinct ``window_seconds`` keep state
  isolated; same window with distinct ``max_requests`` and qualnames keep
  caps independent; ``_reset_limiters()`` clears state.
- Behavior: D7 key selection — default ``func.__qualname__`` shares one
  bucket; custom ``key_fn`` partitions per call.
- Contract: D8 logging — ``rate_limit.request_blocked`` WARNING fires
  with the documented ``extra`` payload on rejection for both
  ``raise_on_limit=True`` and ``raise_on_limit=False`` paths.
"""

from __future__ import annotations

import logging

import pytest

from baldur.core.exceptions import RateLimitExceeded
from baldur.decorators.rate_limit import _reset_limiters, rate_limit

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture(autouse=True)
def clear_module_limiters():
    """Reset the module-level limiter dict before/after each test."""
    _reset_limiters()
    yield
    _reset_limiters()


@pytest.fixture
def reset_rate_limit_settings_singleton():
    """Reset rate-limit settings singleton so env-var changes take effect."""
    from baldur.settings.rate_limit import reset_rate_limit_settings

    reset_rate_limit_settings()
    yield
    reset_rate_limit_settings()


# =============================================================================
# Decoration-time contract — kwarg validation
# =============================================================================


class TestRateLimitDecorationContract:
    """``@rate_limit`` rejects missing/invalid kwargs at decoration time."""

    def test_missing_max_requests_raises_typeerror(self):
        with pytest.raises(TypeError):
            rate_limit()  # type: ignore[call-arg]

    def test_zero_max_requests_raises_typeerror(self):
        with pytest.raises(TypeError, match="positive int"):
            rate_limit(max_requests=0)

    def test_negative_max_requests_raises_typeerror(self):
        with pytest.raises(TypeError, match="positive int"):
            rate_limit(max_requests=-1)

    def test_zero_window_seconds_raises_typeerror(self):
        with pytest.raises(TypeError, match="window_seconds"):
            rate_limit(max_requests=1, window_seconds=0)

    def test_negative_window_seconds_raises_typeerror(self):
        with pytest.raises(TypeError, match="window_seconds"):
            rate_limit(max_requests=1, window_seconds=-5)


# =============================================================================
# Decision paths — allowed / rejected, sync / async, raise_on_limit
# =============================================================================


class TestRateLimitDecisionPaths:
    """Boundary: Nth call passes, N+1 rejects; raise_on_limit toggle."""

    def test_sync_calls_under_limit_pass(self):
        @rate_limit(max_requests=3, window_seconds=60)
        def op() -> str:
            return "ok"

        for _ in range(3):
            assert op() == "ok"

    def test_sync_call_above_limit_raises_rate_limit_exceeded(self):
        @rate_limit(max_requests=2, window_seconds=60)
        def op() -> str:
            return "ok"

        op()
        op()
        with pytest.raises(RateLimitExceeded) as exc_info:
            op()
        assert exc_info.value.limit == 2
        assert exc_info.value.window_seconds == 60

    def test_sync_raise_on_limit_false_returns_none_on_rejection(self):
        @rate_limit(max_requests=1, window_seconds=60, raise_on_limit=False)
        def op() -> str:
            return "ok"

        assert op() == "ok"
        assert op() is None

    @pytest.mark.asyncio
    async def test_async_calls_under_limit_pass(self):
        @rate_limit(max_requests=2, window_seconds=60)
        async def op() -> str:
            return "ok"

        assert await op() == "ok"
        assert await op() == "ok"

    @pytest.mark.asyncio
    async def test_async_call_above_limit_raises(self):
        @rate_limit(max_requests=1, window_seconds=60)
        async def op() -> str:
            return "ok"

        await op()
        with pytest.raises(RateLimitExceeded):
            await op()

    @pytest.mark.asyncio
    async def test_async_raise_on_limit_false_returns_none(self):
        @rate_limit(max_requests=1, window_seconds=60, raise_on_limit=False)
        async def op() -> str:
            return "ok"

        assert await op() == "ok"
        assert await op() is None


# =============================================================================
# D5 toggle — decorator_enabled=False short-circuits
# =============================================================================


class TestRateLimitToggle:
    """D5: when ``decorator_enabled=False`` the limiter is not consulted."""

    def test_disabled_decorator_lets_all_calls_through(
        self, monkeypatch, reset_rate_limit_settings_singleton
    ):
        monkeypatch.setenv("BALDUR_RATE_LIMIT_DECORATOR_ENABLED", "false")
        # Force a fresh singleton with the new env var.
        from baldur.settings.rate_limit import reset_rate_limit_settings

        reset_rate_limit_settings()

        @rate_limit(max_requests=1, window_seconds=60)
        def op() -> str:
            return "ok"

        # 5 calls all pass even though limit is 1
        for _ in range(5):
            assert op() == "ok"

    def test_disabled_decorator_skips_limiter_construction(
        self, monkeypatch, reset_rate_limit_settings_singleton
    ):
        monkeypatch.setenv("BALDUR_RATE_LIMIT_DECORATOR_ENABLED", "false")
        from baldur.settings.rate_limit import reset_rate_limit_settings

        reset_rate_limit_settings()

        import sys

        import baldur.decorators.rate_limit  # noqa: F401  (ensures import)

        rate_limit_mod = sys.modules["baldur.decorators.rate_limit"]

        @rate_limit(max_requests=1, window_seconds=60)
        def op() -> str:
            return "ok"

        op()
        # No limiter was created because the toggle short-circuited at entry.
        assert rate_limit_mod._LIMITERS == {}


# =============================================================================
# D7 limiter sharing — different windows isolated, same window shared
# =============================================================================


class TestRateLimitLimiterSharing:
    """D7: per-window limiter dict prevents cross-window state corruption."""

    def test_distinct_windows_keep_independent_state(self):
        # Two decorators with different windows; exhausting one must not
        # affect the other.
        @rate_limit(max_requests=1, window_seconds=60)
        def short_window() -> str:
            return "short"

        @rate_limit(max_requests=1, window_seconds=120)
        def long_window() -> str:
            return "long"

        short_window()
        # Exhausting short_window must not exhaust long_window.
        assert long_window() == "long"
        with pytest.raises(RateLimitExceeded):
            short_window()
        with pytest.raises(RateLimitExceeded):
            long_window()

    def test_same_window_distinct_qualnames_independent_caps(self):
        # Two decorators sharing the SAME window but with different
        # max_requests and different qualnames — each must apply its own cap.
        @rate_limit(max_requests=1, window_seconds=60)
        def alpha() -> str:
            return "a"

        @rate_limit(max_requests=2, window_seconds=60)
        def beta() -> str:
            return "b"

        assert alpha() == "a"
        with pytest.raises(RateLimitExceeded):
            alpha()
        # beta has its own cap and qualname-based bucket.
        assert beta() == "b"
        assert beta() == "b"
        with pytest.raises(RateLimitExceeded):
            beta()

    def test_reset_limiters_clears_module_state(self):
        import sys

        import baldur.decorators.rate_limit  # noqa: F401  (ensures import)

        rate_limit_mod = sys.modules["baldur.decorators.rate_limit"]

        @rate_limit(max_requests=1, window_seconds=60)
        def op() -> str:
            return "ok"

        op()
        assert 60 in rate_limit_mod._LIMITERS

        _reset_limiters()
        assert rate_limit_mod._LIMITERS == {}

    def test_same_window_decorators_share_limiter_instance(self):
        # Two decorators with the same window share one SlidingWindowLimiter
        # (efficiency goal of the dict-keyed-by-window design).
        import sys

        import baldur.decorators.rate_limit  # noqa: F401  (ensures import)

        rate_limit_mod = sys.modules["baldur.decorators.rate_limit"]

        @rate_limit(max_requests=10, window_seconds=60)
        def alpha() -> str:
            return "a"

        @rate_limit(max_requests=20, window_seconds=60)
        def beta() -> str:
            return "b"

        alpha()
        beta()
        assert len(rate_limit_mod._LIMITERS) == 1
        assert 60 in rate_limit_mod._LIMITERS


# =============================================================================
# D7 key selection — default qualname vs custom key_fn
# =============================================================================


class TestRateLimitKeySelection:
    """Default qualname key vs explicit ``key_fn`` partitioning."""

    def test_default_key_uses_function_qualname(self):
        # Both calls share the qualname-based bucket → second call rejected.
        @rate_limit(max_requests=1, window_seconds=60)
        def op() -> str:
            return "ok"

        op()
        with pytest.raises(RateLimitExceeded):
            op()

    def test_custom_key_fn_partitions_calls(self):
        # Distinct user_ids → distinct buckets → no rejection at limit=1.
        @rate_limit(
            max_requests=1, window_seconds=60, key_fn=lambda user_id: f"u:{user_id}"
        )
        def op(user_id: int) -> str:
            return f"ok:{user_id}"

        assert op(1) == "ok:1"
        assert op(2) == "ok:2"
        # Same user_id collides on the bucket → rejected.
        with pytest.raises(RateLimitExceeded):
            op(1)


# =============================================================================
# D8 logging — rate_limit.request_blocked WARNING
# =============================================================================


class TestRateLimitLogging:
    """D8: WARNING event with documented extras fires on rejection."""

    def test_logs_warning_when_raising(self, caplog):
        @rate_limit(max_requests=1, window_seconds=60)
        def op() -> str:
            return "ok"

        op()
        with caplog.at_level(logging.WARNING, logger="baldur.decorators.rate_limit"):
            with pytest.raises(RateLimitExceeded):
                op()

        records = [
            r for r in caplog.records if r.message == "rate_limit.request_blocked"
        ]
        assert len(records) == 1
        record = records[0]
        assert record.levelno == logging.WARNING
        assert record.function == op.__qualname__
        assert record.window_seconds == 60
        assert record.max_requests == 1
        assert record.remaining == 0

    def test_logs_warning_when_raise_on_limit_false(self, caplog):
        @rate_limit(max_requests=1, window_seconds=60, raise_on_limit=False)
        def op() -> str:
            return "ok"

        op()
        with caplog.at_level(logging.WARNING, logger="baldur.decorators.rate_limit"):
            assert op() is None

        records = [
            r for r in caplog.records if r.message == "rate_limit.request_blocked"
        ]
        assert len(records) == 1
        assert records[0].levelno == logging.WARNING
