"""Unit tests for ``baldur.services.rate_limit.sliding_window``.

Scope (complementing ``tests/unit/api/middleware/test_rate_limit_helpers.py``
which covers check/peek boundary behavior and reset-clears-all):

- ``get_client_status``: xtest-compatible dict shape (Contract) and
  peek-delegation consistency (Behavior).
- ``get_all_clients``: empty/non-empty tracking and reset interaction.
- ``reset_client``: existing/non-existing key, partial deletion.
- ``_warn_on_window_mismatch`` (D2): logging side effect on window change.
- ``reset``: ``_last_seen_window`` tracker cleared.
- ``cleanup_interval``: constructor default and custom value.
"""

from __future__ import annotations

import logging
import threading
import time

import pytest

from baldur.services.rate_limit.sliding_window import (
    RateLimitState,
    SlidingWindowLimiter,
)
from tests.factories.time_helpers import freeze_time

# =============================================================================
# get_client_status — Contract
# =============================================================================


class TestGetClientStatusContract:
    """xtest endpoint response shape is the contract for ``get_client_status``."""

    def test_dict_contains_all_required_keys(self):
        """Response dict must expose exactly the xtest-documented key set."""
        limiter = SlidingWindowLimiter()
        status = limiter.get_client_status("k", max_requests=10, window_seconds=60)
        assert set(status.keys()) == {
            "client_key",
            "current_count",
            "limit",
            "remaining",
            "reset_at",
            "blocked",
            "window_seconds",
        }

    def test_clean_client_shows_zero_count_and_unblocked(self):
        """A client with no recorded hits has count=0, blocked=False."""
        limiter = SlidingWindowLimiter()
        status = limiter.get_client_status("new", max_requests=5, window_seconds=60)
        assert status["client_key"] == "new"
        assert status["current_count"] == 0
        assert status["limit"] == 5
        assert status["remaining"] == 5
        assert status["blocked"] is False
        assert status["window_seconds"] == 60

    def test_exhausted_client_shows_blocked(self):
        """A client at the limit shows blocked=True, remaining=0."""
        limiter = SlidingWindowLimiter()
        for _ in range(3):
            limiter.check("k", max_requests=3, window_seconds=60)
        status = limiter.get_client_status("k", max_requests=3, window_seconds=60)
        assert status["blocked"] is True
        assert status["remaining"] == 0
        assert status["current_count"] == 3


# =============================================================================
# get_client_status — Behavior
# =============================================================================


class TestGetClientStatusBehavior:
    """Consistency between ``get_client_status`` and ``peek``."""

    def test_current_count_equals_limit_minus_remaining(self):
        """current_count is derived as limit - remaining from peek()."""
        limiter = SlidingWindowLimiter()
        limiter.check("k", max_requests=10, window_seconds=60)
        limiter.check("k", max_requests=10, window_seconds=60)

        status = limiter.get_client_status("k", max_requests=10, window_seconds=60)
        peek_state = limiter.peek("k", max_requests=10, window_seconds=60)

        assert status["current_count"] == peek_state.limit - peek_state.remaining
        assert status["blocked"] == (not peek_state.allowed)

    def test_does_not_consume_quota(self):
        """get_client_status delegates to peek — must not record a hit."""
        limiter = SlidingWindowLimiter()
        for _ in range(10):
            limiter.get_client_status("k", max_requests=3, window_seconds=60)
        state = limiter.check("k", max_requests=3, window_seconds=60)
        assert state.allowed is True
        assert state.remaining == 2


# =============================================================================
# get_all_clients — Behavior
# =============================================================================


class TestGetAllClientsBehavior:
    """Client tracking lifecycle."""

    def test_empty_on_fresh_limiter(self):
        """No clients tracked before any check/peek call."""
        limiter = SlidingWindowLimiter()
        assert limiter.get_all_clients() == []

    def test_returns_tracked_keys_after_check(self):
        """Each distinct key passed to check() appears in the list."""
        limiter = SlidingWindowLimiter()
        limiter.check("alpha", max_requests=5, window_seconds=60)
        limiter.check("beta", max_requests=5, window_seconds=60)
        clients = limiter.get_all_clients()
        assert set(clients) == {"alpha", "beta"}

    def test_empty_after_reset(self):
        """reset() clears all tracked clients."""
        limiter = SlidingWindowLimiter()
        limiter.check("k", max_requests=5, window_seconds=60)
        limiter.reset()
        assert limiter.get_all_clients() == []


# =============================================================================
# reset_client — Behavior
# =============================================================================


class TestResetClientBehavior:
    """Selective client removal."""

    def test_existing_key_returns_true_and_removes(self):
        """reset_client on a tracked key returns True and removes it."""
        limiter = SlidingWindowLimiter()
        limiter.check("k", max_requests=5, window_seconds=60)
        assert limiter.reset_client("k") is True
        assert "k" not in limiter.get_all_clients()

    def test_nonexistent_key_returns_false(self):
        """reset_client on an unknown key returns False."""
        limiter = SlidingWindowLimiter()
        assert limiter.reset_client("ghost") is False

    def test_partial_deletion_preserves_other_keys(self):
        """Removing one client does not affect others."""
        limiter = SlidingWindowLimiter()
        limiter.check("a", max_requests=5, window_seconds=60)
        limiter.check("b", max_requests=5, window_seconds=60)
        limiter.check("c", max_requests=5, window_seconds=60)

        limiter.reset_client("b")

        remaining = set(limiter.get_all_clients())
        assert remaining == {"a", "c"}

    def test_reset_client_restores_quota(self):
        """After resetting a client, their quota is fully available again."""
        limiter = SlidingWindowLimiter()
        for _ in range(5):
            limiter.check("k", max_requests=5, window_seconds=60)
        limiter.reset_client("k")

        state = limiter.check("k", max_requests=5, window_seconds=60)
        assert state.allowed is True
        assert state.remaining == 4


# =============================================================================
# _warn_on_window_mismatch (D2) — Side effect
# =============================================================================


class TestWindowMismatchWarnBehavior:
    """D2: warn-only mismatch detection for the window-coupling invariant."""

    def test_first_check_does_not_warn(self, caplog):
        """First call sets the tracker without logging."""
        limiter = SlidingWindowLimiter()
        with caplog.at_level(
            logging.WARNING, logger="baldur.services.rate_limit.sliding_window"
        ):
            limiter.check("k", max_requests=5, window_seconds=60)
        assert "rate_limit.window_mismatch" not in caplog.text

    def test_same_window_does_not_warn(self, caplog):
        """Repeated calls with the same window_seconds do not log."""
        limiter = SlidingWindowLimiter()
        limiter.check("k", max_requests=5, window_seconds=60)
        caplog.clear()
        with caplog.at_level(
            logging.WARNING, logger="baldur.services.rate_limit.sliding_window"
        ):
            limiter.check("k", max_requests=5, window_seconds=60)
        assert "rate_limit.window_mismatch" not in caplog.text

    def test_different_window_emits_warning(self, caplog):
        """Switching window_seconds on the same instance triggers a warning."""
        limiter = SlidingWindowLimiter()
        limiter.check("k", max_requests=5, window_seconds=60)
        with caplog.at_level(
            logging.WARNING, logger="baldur.services.rate_limit.sliding_window"
        ):
            limiter.check("k", max_requests=5, window_seconds=120)
        assert "rate_limit.window_mismatch" in caplog.text

    def test_warning_updates_last_seen_window(self):
        """After a mismatch warning, the tracker updates to the new value."""
        limiter = SlidingWindowLimiter()
        limiter.check("k", max_requests=5, window_seconds=60)
        limiter.check("k", max_requests=5, window_seconds=120)
        assert limiter._last_seen_window == 120


# =============================================================================
# reset — Behavior (last_seen_window cleared)
# =============================================================================


class TestResetBehavior:
    """reset() clears the window mismatch tracker in addition to request data."""

    def test_reset_clears_last_seen_window(self):
        """After reset, _last_seen_window is None."""
        limiter = SlidingWindowLimiter()
        limiter.check("k", max_requests=5, window_seconds=60)
        assert limiter._last_seen_window == 60
        limiter.reset()
        assert limiter._last_seen_window is None

    def test_no_mismatch_warning_after_reset_with_different_window(self, caplog):
        """After reset, a different window does not trigger a mismatch warning."""
        limiter = SlidingWindowLimiter()
        limiter.check("k", max_requests=5, window_seconds=60)
        limiter.reset()
        with caplog.at_level(
            logging.WARNING, logger="baldur.services.rate_limit.sliding_window"
        ):
            limiter.check("k", max_requests=5, window_seconds=120)
        assert "rate_limit.window_mismatch" not in caplog.text


# =============================================================================
# cleanup_interval — Contract
# =============================================================================


class TestCleanupIntervalContract:
    """Constructor cleanup_interval defaults and custom values."""

    def test_default_cleanup_interval(self):
        """Default cleanup_interval is 60.0 seconds."""
        limiter = SlidingWindowLimiter()
        assert limiter._cleanup_interval == 60.0

    def test_custom_cleanup_interval_persists(self):
        """Custom cleanup_interval is stored as provided."""
        limiter = SlidingWindowLimiter(cleanup_interval=300.0)
        assert limiter._cleanup_interval == 300.0


# =============================================================================
# Thread safety — Behavior
# =============================================================================


class TestSlidingWindowThreadSafetyBehavior:
    """Multi-threaded access must not corrupt data."""

    def test_concurrent_check_no_data_corruption(self):
        """10 threads issuing check() in parallel produce consistent state."""
        limiter = SlidingWindowLimiter()
        errors: list[Exception] = []

        def worker(tid: int):
            try:
                for _ in range(50):
                    limiter.check(f"client_{tid}", max_requests=100, window_seconds=60)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0
        assert len(limiter.get_all_clients()) == 10


# =============================================================================
# RateLimitState — Contract
# =============================================================================


class TestRateLimitStateContract:
    """RateLimitState is a frozen dataclass with 4 documented fields."""

    def test_fields_match_documented_schema(self):
        """Fields: limit, remaining, reset_at, allowed — per D3."""
        state = RateLimitState(limit=10, remaining=5, reset_at=1000, allowed=True)
        assert state.limit == 10
        assert state.remaining == 5
        assert state.reset_at == 1000
        assert state.allowed is True

    def test_frozen_prevents_mutation(self):
        """Frozen dataclass rejects attribute assignment."""
        state = RateLimitState(limit=10, remaining=5, reset_at=1000, allowed=True)
        with pytest.raises(AttributeError):
            state.limit = 20


# =============================================================================
# _cleanup_expired — Behavior (periodic memory bounding)
# =============================================================================


class TestCleanupBehavior:
    """The periodic ``_cleanup_expired`` sweep bounds memory by pruning
    out-of-window client entries once ``cleanup_interval`` has elapsed."""

    def test_cleanup_prunes_expired_entries_after_interval(self):
        """An entry whose timestamps fall outside the window is removed once
        cleanup_interval elapses and a later check triggers the sweep."""
        # Given — a client recorded at T0 (30s window, 60s cleanup interval)
        with freeze_time("2026-02-10 10:00:00"):
            limiter = SlidingWindowLimiter(cleanup_interval=60.0)
            limiter.check("stale", max_requests=5, window_seconds=30)
            assert "stale" in limiter.get_all_clients()

        # When — 120s later (past both the 30s window and the 60s cleanup
        # interval), a check on another key triggers the cleanup sweep
        with freeze_time("2026-02-10 10:02:00"):
            limiter.check("fresh", max_requests=5, window_seconds=30)

            # Then — the expired client is pruned; the fresh one remains
            clients = limiter.get_all_clients()
            assert "stale" not in clients
            assert "fresh" in clients

    def test_no_cleanup_before_interval_elapses(self):
        """Within cleanup_interval, the sweep does not run — entries persist
        even past their window (lazy pruning, not eager)."""
        with freeze_time("2026-02-10 10:00:00"):
            limiter = SlidingWindowLimiter(cleanup_interval=60.0)
            limiter.check("stale", max_requests=5, window_seconds=30)

        # +40s: past the 30s window but BEFORE the 60s cleanup interval
        with freeze_time("2026-02-10 10:00:40"):
            limiter.check("fresh", max_requests=5, window_seconds=30)
            # The sweep has not fired, so the stale key is still tracked
            assert "stale" in limiter.get_all_clients()


# =============================================================================
# reset_at — Behavior (window-expiry epoch)
# =============================================================================


class TestResetAtBehavior:
    """``reset_at`` marks the window-expiry epoch: ``int(now + window_seconds)``."""

    def test_check_reset_at_is_now_plus_window(self):
        """check() sets reset_at to the current time plus the window length."""
        with freeze_time("2026-02-10 10:00:00"):
            limiter = SlidingWindowLimiter()
            now = time.time()
            state = limiter.check("k", max_requests=5, window_seconds=60)
            assert state.reset_at == int(now + 60)

    def test_peek_reset_at_is_now_plus_window(self):
        """peek() reports the same window-expiry epoch without recording a hit."""
        with freeze_time("2026-02-10 10:00:00"):
            limiter = SlidingWindowLimiter()
            now = time.time()
            state = limiter.peek("k", max_requests=5, window_seconds=60)
            assert state.reset_at == int(now + 60)
