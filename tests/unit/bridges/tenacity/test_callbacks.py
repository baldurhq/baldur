"""Unit tests for ``baldur.bridges.tenacity.callbacks`` (impl 451).

Scope:
- ``chain()`` — wrapping helper preserves user callbacks.
- ``RetryExhaustedSnapshot`` — frozen-view fields populated correctly.
- Individual callback factories — budget guard, rate-limit emission, snapshot capture.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

import pytest

from baldur.bridges.tenacity.callbacks import (
    BridgeCallbackContext,
    RetryExhaustedSnapshot,
    _BudgetExhaustedAbort,
    _CooldownDeferredAbort,
    chain,
    make_after_callback,
    make_async_after_callback,
    make_async_before_callback,
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
    """``after(retry_state)`` classifies what tenacity is about to retry, and never resets."""

    def test_a_retried_value_is_classified_and_never_resets_the_ladder(
        self, make_retry_state
    ):
        """failed=False here means a result predicate is retrying the value.

        tenacity runs ``after`` only for an attempt it retries or exhausts,
        so a non-failed outcome seen here is a value about to be retried —
        not the accepted success that earns a ladder reset. Resetting on it
        (the earlier behaviour) inverted the reset's meaning; the reset now
        lives with the execute-level translation, which knows the accepted
        outcome. A retried value that is itself a 429 response is a
        rate-limit answer and installs a cooldown, once.
        """
        coord = MagicMock()
        ctx = BridgeCallbackContext(
            domain="d",
            rate_limit_key="payment",
            rate_limit_coordinator=coord,
            retry_budget=None,
        )
        cb = make_after_callback(ctx)

        plain = SimpleNamespace(status_code=500, headers={})
        cb(make_retry_state(attempt_number=1, failed=False, result=plain))
        coord.on_success.assert_not_called()
        coord.on_rate_limited.assert_not_called()
        assert ctx.rate_limit_signal is False

        throttled = SimpleNamespace(status_code=429, headers={"Retry-After": "7"})
        cb(make_retry_state(attempt_number=2, failed=False, result=throttled))
        coord.on_rate_limited.assert_called_once_with(key="payment", retry_after=7.0)
        coord.on_success.assert_not_called()
        assert ctx.rate_limit_signal is True
        assert ctx.last_error is None
        assert ctx.last_attempt == 2

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
        cascade_service.record_rate_limit_observation.assert_called_once_with("payment")

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
        cascade_service.record_rate_limit_observation.assert_not_called()

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
        cascade_service.record_rate_limit_observation.assert_called_once_with("payment")

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

        cascade_service.record_rate_limit_observation.assert_called_once()

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


# =============================================================================
# Behavior — chain() is coroutine-aware
# =============================================================================


class TestChainBehavior:
    """When either member is a coroutine function the chained callback is one too.

    ``tenacity.AsyncRetrying`` awaits a coroutine action natively, so the
    async bridge's ``before`` / ``after`` pair can be chained behind a user's
    synchronous callback — which keeps running first, unchanged.
    """

    def test_two_sync_members_chain_to_a_sync_callable(self):
        order: list[str] = []

        def user(_state):
            order.append("user")

        def baldur(_state):
            order.append("baldur")

        chained = chain(user, baldur)
        chained(None)

        assert not asyncio.iscoroutinefunction(chained)
        assert order == ["user", "baldur"]

    def test_a_sync_user_and_a_coroutine_baldur_chain_to_a_coroutine(self):
        """The user's synchronous ``before`` still runs first, ahead of the awaited one."""
        order: list[str] = []

        def user(_state):
            order.append("user")

        async def baldur(_state):
            order.append("baldur")
            return "awaited"

        chained = chain(user, baldur)

        assert asyncio.iscoroutinefunction(chained)
        assert asyncio.run(chained(None)) == "awaited"
        assert order == ["user", "baldur"]

    def test_a_coroutine_user_and_a_sync_baldur_chain_to_a_coroutine(self):
        order: list[str] = []

        async def user(_state):
            order.append("user")

        def baldur(_state):
            order.append("baldur")

        chained = chain(user, baldur)

        assert asyncio.iscoroutinefunction(chained)
        asyncio.run(chained(None))
        assert order == ["user", "baldur"]

    def test_a_callable_object_with_an_async_call_is_awaited(self):
        """A user callback may be an object whose ``__call__`` is a coroutine."""
        order: list[str] = []

        class AsyncUserCallback:
            async def __call__(self, _state):
                order.append("user")

        def baldur(_state):
            order.append("baldur")

        chained = chain(AsyncUserCallback(), baldur)

        assert asyncio.iscoroutinefunction(chained)
        asyncio.run(chained(None))
        assert order == ["user", "baldur"]

    def test_an_exception_in_the_user_member_propagates_before_baldur_runs(self):
        """User callbacks are preserved verbatim, errors included."""
        order: list[str] = []

        def user(_state):
            raise ValueError("user callback bug")

        async def baldur(_state):
            order.append("baldur")

        chained = chain(user, baldur)

        with pytest.raises(ValueError, match="user callback bug"):
            asyncio.run(chained(None))
        assert order == []


# =============================================================================
# Behavior — the coroutine before / after pair
# =============================================================================

_CALLBACKS_TO_THREAD = "baldur.bridges.tenacity.callbacks.asyncio.to_thread"


def _async_coordinator():
    """A spec'd coordinator whose awaitable wait admits by default."""
    coordinator = MagicMock(spec=RateLimitCoordinator)
    coordinator.await_if_needed.return_value = RateLimitResult(waited=False)
    return coordinator


class TestAsyncBridgeCallbacksBehavior:
    """``make_async_before_callback`` / ``make_async_after_callback`` never block the loop.

    The wait is ``await_if_needed`` (an ``asyncio.sleep``); the classification
    in ``after`` runs on a worker thread because it installs the cooldown,
    which publishes. Every guard of the synchronous pair holds.
    """

    def test_before_awaits_the_cooldown_wait_with_key_and_bound(self, make_retry_state):
        coordinator = _async_coordinator()
        ctx = _ctx(coordinator=coordinator)
        ctx.rate_limit_max_wait = 2.5

        asyncio.run(make_async_before_callback(ctx)(make_retry_state(attempt_number=1)))

        coordinator.await_if_needed.assert_awaited_once_with("payment", max_wait=2.5)
        coordinator.wait_if_needed.assert_not_called()

    def test_before_raises_the_deferral_abort_from_the_coroutine(
        self, make_retry_state, bridge_scope
    ):
        """The abort escapes the loop exactly as the synchronous raise does."""
        scope, _cascade, tracker = bridge_scope
        coordinator = _async_coordinator()
        coordinator.await_if_needed.return_value = RateLimitResult(
            deferred=True, not_before=123.0
        )

        with pytest.raises(_CooldownDeferredAbort) as exc_info:
            asyncio.run(
                make_async_before_callback(_ctx(scope=scope, coordinator=coordinator))(
                    make_retry_state(attempt_number=1)
                )
            )

        assert exc_info.value.not_before == 123.0
        assert scope.attempts == 0
        tracker.record_request.assert_not_called()

    @pytest.mark.parametrize(
        ("wait_result", "expected_signal"),
        [
            (RateLimitResult(waited=False), False),
            (RateLimitResult(waited=True, wait_time=0.1), True),
            (RateLimitResult(waited=False, was_rate_limited=True), True),
        ],
        ids=["no_signal", "waited", "was_rate_limited"],
    )
    def test_before_sets_the_signal_the_execute_level_reset_reads(
        self, make_retry_state, wait_result, expected_signal
    ):
        coordinator = _async_coordinator()
        coordinator.await_if_needed.return_value = wait_result
        ctx = _ctx(coordinator=coordinator)

        asyncio.run(make_async_before_callback(ctx)(make_retry_state(attempt_number=1)))

        assert ctx.rate_limit_signal is expected_signal

    def test_before_counts_the_served_attempt_and_the_budget(
        self, make_retry_state, bridge_scope
    ):
        from baldur.services.backoff_calculator.budget import AdaptiveRetryBudget

        scope, _cascade, tracker = bridge_scope
        budget = MagicMock(spec=AdaptiveRetryBudget)
        ctx = _ctx(scope=scope, coordinator=_async_coordinator())
        ctx.retry_budget = budget

        asyncio.run(make_async_before_callback(ctx)(make_retry_state(attempt_number=2)))

        assert scope.attempts == 1
        tracker.record_request.assert_called_once_with("payment")
        budget.record_request.assert_called_once_with(is_retry=True)

    def test_before_with_an_empty_key_waits_on_nothing(
        self, make_retry_state, bridge_scope
    ):
        """An empty override is not an identity: no wait, but the attempt is counted."""
        scope, _cascade, _tracker = bridge_scope
        coordinator = _async_coordinator()

        asyncio.run(
            make_async_before_callback(
                _ctx(scope=scope, coordinator=coordinator, key="")
            )(make_retry_state(attempt_number=1))
        )

        coordinator.await_if_needed.assert_not_called()
        assert scope.attempts == 1

    def test_before_wait_fault_is_fail_open(self, make_retry_state, bridge_scope):
        scope, _cascade, _tracker = bridge_scope
        coordinator = _async_coordinator()
        coordinator.await_if_needed.side_effect = RuntimeError("coordinator down")
        ctx = _ctx(scope=scope, coordinator=coordinator)

        asyncio.run(make_async_before_callback(ctx)(make_retry_state(attempt_number=1)))

        assert scope.attempts == 1
        assert ctx.rate_limit_signal is False

    def test_before_cancellation_propagates(self, make_retry_state):
        """A cancelled request is cancelled — the fail-open wrap never swallows it."""
        coordinator = _async_coordinator()
        coordinator.await_if_needed.side_effect = asyncio.CancelledError()

        with pytest.raises(asyncio.CancelledError):
            asyncio.run(
                make_async_before_callback(_ctx(coordinator=coordinator))(
                    make_retry_state(attempt_number=1)
                )
            )

    def test_after_classifies_on_a_worker_thread(self, make_retry_state, bridge_scope):
        """The classification installs the cooldown, so it leaves the loop."""
        from tests.factories.rate_limit_doubles import ToThreadSpy

        scope, cascade_service, _tracker = bridge_scope
        coordinator = _async_coordinator()
        ctx = _ctx(scope=scope, coordinator=coordinator)
        spy = ToThreadSpy()

        with patch(_CALLBACKS_TO_THREAD, new=spy):
            asyncio.run(
                make_async_after_callback(ctx)(
                    make_retry_state(
                        attempt_number=1,
                        failed=True,
                        exception=Exception("429 too many requests"),
                    )
                )
            )

        assert spy.hopped("observe_bridge_outcome") is True
        coordinator.on_rate_limited.assert_called_once()
        assert coordinator.on_rate_limited.call_args.kwargs["key"] == "payment"
        assert scope.rate_limited == 1
        cascade_service.record_rate_limit_observation.assert_called_once_with("payment")
        assert ctx.rate_limit_signal is True
        assert ctx.last_attempt == 1

    def test_after_classifies_a_retried_returned_429_too(
        self, make_retry_state, bridge_scope
    ):
        scope, _cascade, _tracker = bridge_scope
        coordinator = _async_coordinator()
        ctx = _ctx(scope=scope, coordinator=coordinator)
        throttled = SimpleNamespace(status_code=429, headers={"Retry-After": "7"})

        asyncio.run(
            make_async_after_callback(ctx)(
                make_retry_state(attempt_number=2, failed=False, result=throttled)
            )
        )

        coordinator.on_rate_limited.assert_called_once_with(
            key="payment", retry_after=7.0
        )
        coordinator.on_success.assert_not_called()
        assert ctx.last_error is None

    def test_after_with_an_empty_key_feeds_the_cascade_but_no_coordinator(
        self, make_retry_state, bridge_scope
    ):
        scope, cascade_service, _tracker = bridge_scope
        coordinator = _async_coordinator()

        asyncio.run(
            make_async_after_callback(
                _ctx(scope=scope, coordinator=coordinator, key="")
            )(
                make_retry_state(
                    attempt_number=1,
                    failed=True,
                    exception=Exception("429 too many requests"),
                )
            )
        )

        coordinator.on_rate_limited.assert_not_called()
        assert scope.rate_limited == 1
        cascade_service.record_rate_limit_observation.assert_called_once_with("payment")

    def test_after_with_no_outcome_hops_nothing(self, make_retry_state):
        from tests.factories.rate_limit_doubles import ToThreadSpy

        ctx = _ctx(coordinator=_async_coordinator())
        spy = ToThreadSpy()

        with patch(_CALLBACKS_TO_THREAD, new=spy):
            asyncio.run(
                make_async_after_callback(ctx)(make_retry_state(attempt_number=2))
            )

        assert spy.calls == []
        assert ctx.last_attempt is None


# =============================================================================
# Behavior — the result-rejection exhaustion synthesis
# =============================================================================


class TestRetryErrorCallbackResultRejectionBehavior:
    """An exhaustion with no exception behind it synthesises its own error.

    A loop driven by ``retry_if_result`` that never accepts leaves tenacity
    with a non-failed final outcome; ``_retry_error`` used to return ``None``
    for it, which the bridge translated as ``SUCCESS(value=None)`` — a success
    the loop never had.
    """

    @pytest.fixture
    def event_bus(self, monkeypatch):
        from baldur.services.event_bus import BaldurEventBus

        bus = MagicMock(spec=BaldurEventBus)
        monkeypatch.setattr("baldur.services.event_bus.get_event_bus", lambda: bus)
        return bus

    def test_no_user_callback_raises_a_synthesised_exhaustion(
        self, make_retry_state, event_bus, bridge_scope
    ):
        from baldur.services.retry_handler.models import MaxRetriesExceededError

        scope, _cascade, _tracker = bridge_scope
        ctx = _ctx(scope=scope)
        retry_state = make_retry_state(
            attempt_number=3, failed=False, result="rejected value"
        )

        with pytest.raises(MaxRetriesExceededError) as exc_info:
            make_retry_error_callback(ctx, None)(retry_state)

        exhausted = exc_info.value
        assert exhausted.result_rejected is True
        assert exhausted.last_result == "rejected value"
        assert exhausted.last_error is None
        assert exhausted.retry_count == 3
        assert scope.was_classified(exhausted) is True
        assert ctx.snapshot.last_error is exhausted

    def test_a_user_callback_returns_its_fallback_over_the_synthesised_error(
        self, make_retry_state, event_bus
    ):
        from baldur.services.retry_handler.models import MaxRetriesExceededError

        ctx = _ctx()
        retry_state = make_retry_state(
            attempt_number=2, failed=False, result="rejected"
        )

        value = make_retry_error_callback(ctx, lambda _state: "user-default")(
            retry_state
        )

        assert value == "user-default"
        assert isinstance(ctx.snapshot.last_error, MaxRetriesExceededError)
        assert ctx.snapshot.last_error.result_rejected is True
        assert ctx.snapshot.user_fallback_value == "user-default"

    def test_the_exhausted_event_names_the_synthesised_type(
        self, make_retry_state, event_bus
    ):
        ctx = _ctx()
        retry_state = make_retry_state(
            attempt_number=2, failed=False, result="rejected"
        )

        make_retry_error_callback(ctx, lambda _state: None)(retry_state)

        event_data = event_bus.emit.call_args.kwargs["data"]
        assert event_data["final_error_type"] == "MaxRetriesExceededError"
        assert event_data["attempts"] == 2

    def test_a_failed_outcome_keeps_its_own_error(self, make_retry_state, event_bus):
        """Discriminator: the synthesis fires only when nothing was raised."""
        error = ValueError("real failure")
        ctx = _ctx()
        retry_state = make_retry_state(attempt_number=2, failed=True, exception=error)

        with pytest.raises(ValueError):
            make_retry_error_callback(ctx, None)(retry_state)

        assert ctx.snapshot.last_error is error
