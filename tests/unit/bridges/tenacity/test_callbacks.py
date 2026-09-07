"""Unit tests for ``baldur.bridges.tenacity.callbacks`` (impl 451).

Scope:
- ``chain()`` — wrapping helper preserves user callbacks.
- ``RetryExhaustedSnapshot`` — frozen-view fields populated correctly.
- Individual callback factories — budget guard, rate-limit emission, snapshot capture.
"""

from __future__ import annotations

from unittest.mock import MagicMock, call, patch

import pytest

from baldur.bridges.tenacity.callbacks import (
    BridgeCallbackContext,
    RetryExhaustedSnapshot,
    _BudgetExhaustedAbort,
    _CooldownDeferredAbort,
    chain,
    make_after_callback,
    make_before_callback,
    make_before_sleep_callback,
    make_retry_error_callback,
    observe_bridge_outcome,
)
from baldur.services.circuit_breaker.rate_limit_tracker import RateLimitTracker
from baldur.services.circuit_breaker.service import CircuitBreakerService
from baldur.services.rate_limit_coordinator import RateLimitCoordinator
from baldur.services.rate_limit_coordinator.models import RateLimitResult

_RECORD_ATTEMPT_STARTED = (
    "baldur.services.metrics.recorders.record_retry_attempt_started"
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
# Behavior — make_before_callback records the timely pressure series (729 D6)
# =============================================================================


class TestBeforeCallbackAttemptsStartedBehavior:
    """``before`` is this bridge's only metric surface, on every attempt.

    A tenacity-driven sequence writes no terminal series at all, so without the
    record here bridge-managed retries would be missing from retry pressure
    while the two native policies are present in it — one alert reading two
    populations. ``retry_state.attempt_number`` is already 1-based, which is
    the helper's own convention, so the bridge forwards it unchanged.
    """

    @staticmethod
    def _ctx(**overrides):
        """Bridge context with no collaborators unless a test supplies them."""
        kwargs = {
            "domain": "payment",
            "rate_limit_key": None,
            "rate_limit_coordinator": None,
            "retry_budget": None,
        }
        kwargs.update(overrides)
        return BridgeCallbackContext(**kwargs)

    @pytest.mark.parametrize(
        ("attempt_number", "expected_is_retry"),
        [(1, False), (2, True), (5, True)],
        ids=["first_attempt", "first_retry", "deep_in_the_ladder"],
    )
    def test_bridge_records_attempts_started_with_the_one_based_attempt_number(
        self, make_retry_state, attempt_number, expected_is_retry
    ):
        """tenacity's 1-based counter maps straight onto the helper's contract."""
        cb = make_before_callback(self._ctx())

        with patch(_RECORD_ATTEMPT_STARTED, autospec=True) as mock_started:
            cb(make_retry_state(attempt_number=attempt_number))

        mock_started.assert_called_once_with("payment", is_retry=expected_is_retry)

    def test_bridge_records_attempts_started_with_no_retry_budget_injected(
        self, make_retry_state
    ):
        """The record is not gated on the budget the line above it consults.

        Nothing in the tree constructs an ``AdaptiveRetryBudget``, so a record
        living inside that guard would never fire on any real deployment — the
        defect this series was added to avoid repeating.
        """
        cb = make_before_callback(self._ctx(retry_budget=None))

        with patch(_RECORD_ATTEMPT_STARTED, autospec=True) as mock_started:
            cb(make_retry_state(attempt_number=2))

        mock_started.assert_called_once_with("payment", is_retry=True)

    def test_bridge_records_attempts_started_before_the_cooldown_wait_begins(
        self, make_retry_state
    ):
        """Same ordering pin as the native loops: counted at sleep start.

        The coordinator double reports how many starts existed when the wait
        was entered; one means the attempt about to sleep out an honored
        ``Retry-After`` had already been counted.
        """
        # Given
        coordinator = MagicMock(spec=RateLimitCoordinator)
        starts_at_wait_entry: list[int] = []
        cb = make_before_callback(
            self._ctx(rate_limit_key="payment", rate_limit_coordinator=coordinator)
        )

        # When
        with patch(_RECORD_ATTEMPT_STARTED, autospec=True) as mock_started:

            def _wait(_key, max_wait=None):
                starts_at_wait_entry.append(mock_started.call_count)
                return RateLimitResult(waited=False)

            coordinator.wait_if_needed.side_effect = _wait
            cb(make_retry_state(attempt_number=2))

        # Then
        assert starts_at_wait_entry == [1]

    def test_bridge_deferred_attempt_records_attempts_started_before_aborting(
        self, make_retry_state
    ):
        """A deferral aborts the loop, but the demand it refused is still counted."""
        coordinator = MagicMock(spec=RateLimitCoordinator)
        coordinator.wait_if_needed.return_value = RateLimitResult(
            deferred=True, not_before=1.0
        )
        cb = make_before_callback(
            self._ctx(rate_limit_key="payment", rate_limit_coordinator=coordinator)
        )

        with patch(_RECORD_ATTEMPT_STARTED, autospec=True) as mock_started:
            with pytest.raises(_CooldownDeferredAbort):
                cb(make_retry_state(attempt_number=1))

        assert mock_started.call_args_list == [call("payment", is_retry=False)]


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


# =============================================================================
# Behavior — the bridge's share of the outbound 429 observation
# =============================================================================

_BRIDGE_CB_SERVICE = (
    "baldur.services.circuit_breaker.convenience.get_circuit_breaker_service"
)
_BRIDGE_TRACKER = (
    "baldur.services.circuit_breaker.rate_limit_tracker.get_rate_limit_tracker"
)


@pytest.fixture
def bridge_scope():
    """An open observation scope with its two sinks stubbed.

    Yields ``(scope, cascade_service, tracker)``.
    """
    from baldur.services.circuit_breaker.rate_limit_observation import (
        close_scope,
        open_scope,
    )

    cascade_service = MagicMock(spec=CircuitBreakerService)
    tracker = MagicMock(spec=RateLimitTracker)
    token, scope = open_scope("payment")
    try:
        with (
            patch(_BRIDGE_CB_SERVICE, return_value=cascade_service),
            patch(_BRIDGE_TRACKER, return_value=tracker),
        ):
            yield scope, cascade_service, tracker
    finally:
        close_scope(token)


def _ctx(*, scope=None, coordinator=None, key="payment"):
    """A bridge callback context carrying the given collaborators."""
    return BridgeCallbackContext(
        domain="d",
        rate_limit_key=key,
        rate_limit_coordinator=coordinator,
        retry_budget=None,
        scope=scope,
    )


class TestBridgeBeforeCallbackBehavior:
    """``before`` counts the attempt last, after the deferral could abort it."""

    def test_a_deferred_attempt_is_never_counted(self, make_retry_state, bridge_scope):
        """The deferral aborts before the dependency is called.

        tenacity runs ``before`` outside the attempt's own try, so the abort
        leaves the loop with no further callback — which is exactly why the
        count cannot be moved above it. A counted deferral would put calls the
        cooldown prevented into the cascade rate's denominator.
        """
        scope, _cascade, tracker = bridge_scope
        coordinator = MagicMock(spec=RateLimitCoordinator)
        coordinator.wait_if_needed.return_value = RateLimitResult(
            deferred=True, not_before=123.0
        )

        with pytest.raises(_CooldownDeferredAbort):
            make_before_callback(_ctx(scope=scope, coordinator=coordinator))(
                make_retry_state(attempt_number=1)
            )

        assert scope.attempts == 0
        tracker.record_request.assert_not_called()

    def test_a_served_attempt_is_counted(self, make_retry_state, bridge_scope):
        """Discriminator: the skip above is the deferral, not a blanket no-count."""
        scope, _cascade, tracker = bridge_scope
        coordinator = MagicMock(spec=RateLimitCoordinator)
        coordinator.wait_if_needed.return_value = RateLimitResult(
            waited=True, wait_time=0.5
        )

        make_before_callback(_ctx(scope=scope, coordinator=coordinator))(
            make_retry_state(attempt_number=1)
        )

        assert scope.attempts == 1
        tracker.record_request.assert_called_once_with("payment")

    def test_a_coordinator_less_bridge_still_counts_its_attempt(
        self, make_retry_state, bridge_scope
    ):
        """The count belongs to the breaker above, not to the coordinator.

        A bridge with no key coordinates nothing, but the calls it makes are
        still the denominator of the breaker's cascade rate.
        """
        scope, _cascade, _tracker = bridge_scope

        make_before_callback(_ctx(scope=scope, coordinator=None, key=None))(
            make_retry_state(attempt_number=1)
        )

        assert scope.attempts == 1

    def test_a_coordinator_fault_does_not_lose_the_attempt_count(
        self, make_retry_state, bridge_scope
    ):
        """Fail-open on the wait must not take the bookkeeping with it."""
        scope, _cascade, _tracker = bridge_scope
        coordinator = MagicMock(spec=RateLimitCoordinator)
        coordinator.wait_if_needed.side_effect = RuntimeError("coordinator down")

        make_before_callback(_ctx(scope=scope, coordinator=coordinator))(
            make_retry_state(attempt_number=1)
        )

        assert scope.attempts == 1


class TestBridgeAfterCallbackBehavior:
    """``after`` stashes what the execute-level translation needs, then observes."""

    def test_a_failed_attempt_stashes_its_error_and_number(self, make_retry_state):
        """The deferral translation reports this error instead of a phantom one."""
        error = ValueError("boom")
        ctx = _ctx()

        make_after_callback(ctx)(
            make_retry_state(attempt_number=3, failed=True, exception=error)
        )

        assert ctx.last_error is error
        assert ctx.last_attempt == 3

    def test_a_non_failed_outcome_clears_the_stashed_error(self, make_retry_state):
        """A later success must not leave an earlier attempt's error standing.

        The deferral translation synthesises its own error only when none is
        stashed, so a stale one would report a failure for a call never made.
        """
        ctx = _ctx()
        make_after_callback(ctx)(
            make_retry_state(attempt_number=1, failed=True, exception=ValueError("x"))
        )

        make_after_callback(ctx)(
            make_retry_state(attempt_number=2, failed=False, exception=None)
        )

        assert ctx.last_error is None
        assert ctx.last_attempt == 2

    def test_a_429_is_classified_even_when_no_key_was_given(
        self, bridge_scope, make_retry_state
    ):
        """Classification precedes the coordinator/key guard.

        The guard used to sit first, so a bridge without a ``rate_limit_key``
        fed the breaker's cascade nothing at all — which is the configuration
        every non-coordinating tenacity user has.
        """
        scope, cascade_service, _tracker = bridge_scope
        error = Exception("429 too many requests")

        make_after_callback(_ctx(scope=scope, coordinator=None, key=None))(
            make_retry_state(attempt_number=1, failed=True, exception=error)
        )

        assert scope.rate_limited == 1
        cascade_service.record_rate_limit_response.assert_called_once_with("payment")

    def test_an_ordinary_failure_is_marked_but_counts_no_429(
        self, bridge_scope, make_retry_state
    ):
        """Every outcome is marked; only a 429 is observed as one."""
        scope, cascade_service, _tracker = bridge_scope
        error = ConnectionError("connection reset")

        make_after_callback(_ctx(scope=scope))(
            make_retry_state(attempt_number=1, failed=True, exception=error)
        )

        assert scope.was_classified(error) is True
        assert scope.rate_limited == 0
        cascade_service.record_rate_limit_response.assert_not_called()

    def test_an_outcome_less_state_is_a_noop(self, make_retry_state):
        """tenacity can hand over a state with no outcome; nothing is stashed."""
        ctx = _ctx()

        make_after_callback(ctx)(make_retry_state(attempt_number=2))

        assert ctx.last_attempt is None
        assert ctx.last_error is None


class TestBridgeOutcomeObservationBehavior:
    """``observe_bridge_outcome`` — one classification, wrapped fail-open."""

    def test_neither_scope_nor_coordinator_is_a_noop(self):
        """Nothing to inform, so nothing is read off the caller's object."""

        class Exploding:
            @property
            def status_code(self):
                raise AssertionError("classification must not run")

        observe_bridge_outcome(_ctx(), Exploding())

    def test_a_scope_only_context_records_the_cascade_without_a_cooldown(
        self, bridge_scope
    ):
        """A bridge with no coordinator still feeds the breaker above it."""
        scope, cascade_service, _tracker = bridge_scope

        observe_bridge_outcome(
            _ctx(scope=scope, coordinator=None, key=None),
            Exception("429 too many requests"),
        )

        assert scope.rate_limited == 1
        cascade_service.record_rate_limit_response.assert_called_once_with("payment")

    def test_the_mark_is_applied_before_detection_can_fail(self, bridge_scope):
        """Detection reads caller-supplied attributes, so it can raise.

        Marking after it would let a caller's exploding property hand the same
        outcome to the breaker stage as unseen, and it would be counted twice.
        """
        scope, _cascade, _tracker = bridge_scope
        outcome = Exception("429 too many requests")

        with patch(
            "baldur.bridges.tenacity.callbacks.detect_rate_limit",
            side_effect=RuntimeError("classifier fault"),
        ):
            observe_bridge_outcome(_ctx(scope=scope), outcome)

        assert scope.was_classified(outcome) is True

    def test_a_coordinator_fault_does_not_break_the_users_loop(self, bridge_scope):
        """tenacity invokes ``after`` un-guarded, so an escape aborts the retry."""
        scope, cascade_service, _tracker = bridge_scope
        coordinator = MagicMock(spec=RateLimitCoordinator)
        coordinator.on_rate_limited.side_effect = RuntimeError("coordinator down")

        observe_bridge_outcome(
            _ctx(scope=scope, coordinator=coordinator),
            Exception("429 too many requests"),
        )

        cascade_service.record_rate_limit_response.assert_called_once()

    def test_the_retry_after_reaches_the_coordinator(self, bridge_scope):
        """A provider's stated wait survives the hop into the cooldown."""
        scope, _cascade, _tracker = bridge_scope
        coordinator = MagicMock(spec=RateLimitCoordinator)

        class ThrottledError(Exception):
            retry_after = 30.0

        observe_bridge_outcome(
            _ctx(scope=scope, coordinator=coordinator),
            ThrottledError("429 too many requests"),
        )

        coordinator.on_rate_limited.assert_called_once_with(
            key="payment", retry_after=30.0
        )
