"""Unit tests for ``baldur.bridges.tenacity.callbacks`` (impl 451).

Scope:
- ``chain()`` — wrapping helper preserves user callbacks.
- ``RetryExhaustedSnapshot`` — frozen-view fields populated correctly.
- Individual callback factories — budget guard, rate-limit emission, snapshot capture.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from baldur.bridges.tenacity.callbacks import (
    BridgeCallbackContext,
    RetryExhaustedSnapshot,
    _BudgetExhaustedAbort,
    chain,
    make_after_callback,
    make_before_callback,
    make_before_sleep_callback,
    make_retry_error_callback,
)

# =============================================================================
# Contract — chain() wrapper
# =============================================================================


class TestChainContract:
    """``chain()`` returns a single callable that runs original first."""

    def test_returns_baldur_unchanged_when_original_is_none(self):
        """No user callback → caller gets the Baldur callable directly."""

        def _baldur(_state):
            return "baldur"

        result = chain(None, _baldur)

        assert result is _baldur

    def test_runs_original_before_baldur(self):
        """When both supplied, original runs first then Baldur."""
        order: list[str] = []

        def _user(_state):
            order.append("user")

        def _baldur(_state):
            order.append("baldur")

        chained = chain(_user, _baldur)
        chained(None)

        assert order == ["user", "baldur"]


# =============================================================================
# Contract — RetryExhaustedSnapshot
# =============================================================================


class TestRetryExhaustedSnapshotContract:
    """Snapshot fields capture the final retry state for the policy caller."""

    def test_snapshot_stores_attempt_number_and_error(self):
        """Constructor positional args populate __slots__ fields."""
        err = ValueError("boom")
        snap = RetryExhaustedSnapshot(attempt_number=4, last_error=err)

        assert snap.attempt_number == 4
        assert snap.last_error is err
        assert snap.user_fallback_value is None

    def test_snapshot_uses_slots_no_dict(self):
        """``__slots__`` declared — no per-instance __dict__ overhead."""
        snap = RetryExhaustedSnapshot(attempt_number=1, last_error=None)

        with pytest.raises(AttributeError):
            snap.unknown_field = "x"  # type: ignore[attr-defined]


# =============================================================================
# Behavior — make_before_callback (budget + rate-limit wait)
# =============================================================================


class TestMakeBeforeCallbackBehavior:
    """``before(retry_state)`` records request and waits if rate-limited."""

    def test_records_first_attempt_as_non_retry(self, make_retry_state):
        """attempt_number=1 → record_request(is_retry=False)."""
        budget = MagicMock()
        ctx = BridgeCallbackContext(
            domain="d",
            rate_limit_key=None,
            rate_limit_coordinator=None,
            retry_budget=budget,
        )
        cb = make_before_callback(ctx)
        cb(make_retry_state(attempt_number=1))

        budget.record_request.assert_called_once_with(is_retry=False)

    def test_records_subsequent_attempt_as_retry(self, make_retry_state):
        """attempt_number>1 → record_request(is_retry=True)."""
        budget = MagicMock()
        ctx = BridgeCallbackContext(
            domain="d",
            rate_limit_key=None,
            rate_limit_coordinator=None,
            retry_budget=budget,
        )
        cb = make_before_callback(ctx)
        cb(make_retry_state(attempt_number=2))

        budget.record_request.assert_called_once_with(is_retry=True)

    def test_skips_budget_when_none(self, make_retry_state):
        """No budget → no record_request call (vanilla tenacity behavior)."""
        ctx = BridgeCallbackContext(
            domain="d",
            rate_limit_key=None,
            rate_limit_coordinator=None,
            retry_budget=None,
        )
        cb = make_before_callback(ctx)
        # Should not raise — no budget to call.
        cb(make_retry_state(attempt_number=1))


# =============================================================================
# Behavior — make_before_sleep_callback (budget guard abort)
# =============================================================================


class TestMakeBeforeSleepCallbackBehavior:
    """before_sleep raises ``_BudgetExhaustedAbort`` when budget rejects."""

    def test_raises_when_budget_rejects(self, make_retry_state):
        """should_allow_retry=False → abort."""
        budget = MagicMock()
        budget.should_allow_retry.return_value = False
        budget.get_stats.return_value = {}

        ctx = BridgeCallbackContext(
            domain="d",
            rate_limit_key=None,
            rate_limit_coordinator=None,
            retry_budget=budget,
        )
        cb = make_before_sleep_callback(ctx)

        with pytest.raises(_BudgetExhaustedAbort):
            cb(make_retry_state(attempt_number=2))

    def test_does_not_raise_when_budget_allows(self, make_retry_state):
        """should_allow_retry=True → no-op."""
        budget = MagicMock()
        budget.should_allow_retry.return_value = True

        ctx = BridgeCallbackContext(
            domain="d",
            rate_limit_key=None,
            rate_limit_coordinator=None,
            retry_budget=budget,
        )
        cb = make_before_sleep_callback(ctx)
        cb(make_retry_state(attempt_number=2))  # must not raise

    def test_no_op_when_budget_is_none(self, make_retry_state):
        """No budget → never raise."""
        ctx = BridgeCallbackContext(
            domain="d",
            rate_limit_key=None,
            rate_limit_coordinator=None,
            retry_budget=None,
        )
        cb = make_before_sleep_callback(ctx)
        cb(make_retry_state(attempt_number=2))  # must not raise


# =============================================================================
# Behavior — make_retry_error_callback (snapshot capture before user callback)
# =============================================================================


class TestMakeRetryErrorCallbackBehavior:
    """``retry_error_callback`` captures snapshot BEFORE user callback runs."""

    def test_snapshot_recorded_before_user_callback(
        self, make_retry_state, monkeypatch
    ):
        """Even when user callback returns fallback, ctx.snapshot has the error."""
        # Stub the EventBus emission to keep the test hermetic.
        monkeypatch.setattr(
            "baldur.services.event_bus.get_event_bus",
            lambda: MagicMock(),
        )

        ctx = BridgeCallbackContext(
            domain="payment",
            rate_limit_key=None,
            rate_limit_coordinator=None,
            retry_budget=None,
        )

        err = RuntimeError("boom")
        retry_state = make_retry_state(attempt_number=3, failed=True, exception=err)

        def _user_fallback(_state):
            return "user-default"

        cb = make_retry_error_callback(ctx, _user_fallback)
        result = cb(retry_state)

        assert result == "user-default"
        assert ctx.snapshot is not None
        assert ctx.snapshot.attempt_number == 3
        assert ctx.snapshot.last_error is err
        assert ctx.snapshot.user_fallback_value == "user-default"

    def test_reraises_last_error_when_no_user_callback(
        self, make_retry_state, monkeypatch
    ):
        """Without user callback, vanilla tenacity behavior re-raises."""
        monkeypatch.setattr(
            "baldur.services.event_bus.get_event_bus",
            lambda: MagicMock(),
        )

        ctx = BridgeCallbackContext(
            domain="d",
            rate_limit_key=None,
            rate_limit_coordinator=None,
            retry_budget=None,
        )
        err = ValueError("nope")
        retry_state = make_retry_state(attempt_number=2, failed=True, exception=err)

        cb = make_retry_error_callback(ctx, None)

        with pytest.raises(ValueError, match="nope"):
            cb(retry_state)


# =============================================================================
# Behavior — make_after_callback (success/failure routing)
# =============================================================================


class TestMakeAfterCallbackBehavior:
    """``after(retry_state)`` routes to on_success / on_rate_limited as appropriate."""

    def test_success_invokes_on_success(self, make_retry_state):
        """failed=False → on_success(key)."""
        coord = MagicMock()
        ctx = BridgeCallbackContext(
            domain="d",
            rate_limit_key="payment",
            rate_limit_coordinator=coord,
            retry_budget=None,
        )
        cb = make_after_callback(ctx)
        cb(make_retry_state(attempt_number=1, failed=False, exception=None))

        coord.on_success.assert_called_once_with("payment")
        coord.on_rate_limited.assert_not_called()

    def test_skips_when_no_coordinator(self, make_retry_state):
        """coordinator=None → no-op even with key."""
        ctx = BridgeCallbackContext(
            domain="d",
            rate_limit_key="payment",
            rate_limit_coordinator=None,
            retry_budget=None,
        )
        cb = make_after_callback(ctx)
        cb(make_retry_state(attempt_number=1, failed=False, exception=None))
        # No assertion target — purely no-raise check.
