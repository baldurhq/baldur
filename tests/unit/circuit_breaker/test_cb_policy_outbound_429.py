"""
Circuit-breaker stage — outbound 429 observation and returned-response classification.

Test target: services/circuit_breaker/policy.py
- _is_failure(): the cooldown deferral is excluded ahead of both operator dials
- _on_success(): a *returned* 429/5xx is a breaker failure, not a success
- _on_failure(): the raised half, its request write and its cascade gate
- execute() / AsyncCircuitBreakerPolicy.execute(): the per-call observation scope
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from baldur.interfaces.repositories import CircuitBreakerStateData
from baldur.interfaces.resilience_policy import PolicyHook, PolicyOutcome
from baldur.services.circuit_breaker.config import CircuitBreakerDecision
from baldur.services.circuit_breaker.policy import (
    AsyncCircuitBreakerPolicy,
    CircuitBreakerPolicy,
    circuit_breaker,
)
from baldur.services.circuit_breaker.rate_limit_observation import current_scope
from baldur.services.circuit_breaker.rate_limit_tracker import RateLimitTracker
from baldur.services.circuit_breaker.service import CircuitBreakerService
from baldur.services.rate_limit_coordinator import RateLimitCoordinator
from baldur.services.rate_limit_coordinator.models import RateLimitDeferredError
from baldur.settings.middleware import (
    BaldurMiddlewareSettings,
    reset_middleware_settings,
)
from baldur.settings.rate_limit_backoff import RateLimitBackoffSettings

_TRACKER = "baldur.services.circuit_breaker.rate_limit_tracker.get_rate_limit_tracker"
_CB_SERVICE = "baldur.services.circuit_breaker.convenience.get_circuit_breaker_service"
_COORDINATOR = "baldur.services.rate_limit_coordinator.RateLimitCoordinator"
_BACKOFF_SETTINGS = "baldur.settings.rate_limit_backoff.get_rate_limit_backoff_settings"


class ThrottledError(Exception):
    """A client that raises on 429."""

    def __init__(self):
        super().__init__("HTTP 429 Too Many Requests")


def _response(**attributes):
    """A returned-value double exposing exactly the given attributes."""
    return type("FakeResponse", (), attributes)()


@pytest.fixture
def cb_service():
    """The breaker service the policy records against — admits every call."""
    service = MagicMock(spec=CircuitBreakerService)
    service.is_enabled = True
    service.should_allow_with_state.return_value = CircuitBreakerDecision(
        allowed=True,
        state=CircuitBreakerStateData(service_name="payment_api", state="closed"),
    )
    return service


@pytest.fixture
def observation():
    """The three observation sinks the fan-out reaches, all stubbed.

    Returns ``(tracker, cascade_service, coordinator)`` — the denominator
    writer, the single writer of the 429 counter, and the fleet-wide cooldown.
    """
    tracker = MagicMock(spec=RateLimitTracker)
    cascade_service = MagicMock(spec=CircuitBreakerService)
    coordinator = MagicMock(spec=RateLimitCoordinator)
    settings = MagicMock(spec=RateLimitBackoffSettings)
    settings.coordination_enabled = True

    with (
        patch(_TRACKER, return_value=tracker),
        patch(_CB_SERVICE, return_value=cascade_service),
        patch(_BACKOFF_SETTINGS, return_value=settings),
        patch(_COORDINATOR) as coordinator_cls,
    ):
        coordinator_cls.get_instance.return_value = coordinator
        yield tracker, cascade_service, coordinator


@pytest.fixture
def policy(cb_service):
    """A policy protecting an identified downstream."""
    return CircuitBreakerPolicy(service_name="payment_api", cb_service=cb_service)


# =============================================================================
# Behavior — _is_failure(): the deferral exclusion
# =============================================================================


class TestCircuitBreakerPolicyFailureGate:
    """A cooldown deferral is never a breaker failure, whatever the caller set."""

    def test_a_cooldown_deferral_is_not_a_failure(self, policy):
        """The dependency was never contacted, so it carries no health evidence.

        Counting it let a fleet-wide cooldown that deferred N calls trip the
        breaker on a dependency that was never even asked.
        """
        deferral = RateLimitDeferredError(key="payment_api", not_before=1.0)

        assert policy._is_failure(deferral) is False

    def test_the_deferral_exclusion_outranks_a_caller_supplied_failure_tuple(
        self, cb_service
    ):
        """A domain invariant, not a dial: naming the type cannot re-enable it.

        The exclusion sits ahead of both operator tuples, so a caller who lists
        the deferral class explicitly still does not trip their own breaker on
        Baldur's own refusal to call.
        """
        policy = CircuitBreakerPolicy(
            service_name="payment_api",
            cb_service=cb_service,
            failure_exceptions=(RateLimitDeferredError,),
        )
        deferral = RateLimitDeferredError(key="payment_api", not_before=1.0)

        assert policy._is_failure(deferral) is False

    def test_an_ignored_exception_type_is_still_not_a_failure(self, cb_service):
        """The ordinary ignore tuple keeps working beside the new exclusion."""
        policy = CircuitBreakerPolicy(
            service_name="payment_api",
            cb_service=cb_service,
            ignore_exceptions=(ValueError,),
        )

        assert policy._is_failure(ValueError("business rule")) is False

    def test_an_exception_outside_the_failure_tuple_is_not_a_failure(self, cb_service):
        """A narrowed failure tuple keeps working beside the new exclusion."""
        policy = CircuitBreakerPolicy(
            service_name="payment_api",
            cb_service=cb_service,
            failure_exceptions=(ConnectionError,),
        )

        assert policy._is_failure(ValueError("business rule")) is False

    def test_an_ordinary_exception_is_a_failure(self, policy):
        """The default tuple still counts what it always counted."""
        assert policy._is_failure(ConnectionError("connection reset")) is True


# =============================================================================
# Behavior — _on_success(): a returned response is classified, not assumed OK
# =============================================================================


class TestCircuitBreakerPolicyReturnedResponseBehavior:
    """ "Returned" is not "succeeded" when the value is an HTTP answer."""

    @pytest.mark.parametrize(
        ("value", "records_failure", "feeds_cascade"),
        [
            (_response(status_code=200), False, False),
            (_response(status_code=429), True, True),
            (_response(status_code=503), True, False),
            (_response(), False, False),
        ],
        ids=["ok_2xx", "throttled_429", "server_error_503", "not_a_response"],
    )
    def test_a_returned_status_decides_the_record_and_the_cascade(
        self, policy, cb_service, observation, value, records_failure, feeds_cascade
    ):
        """One classification covers the client convention that returns its answer.

        A client that hands a 429 or a 5xx back instead of raising reported a
        clean success to the breaker, so a fully-throttled dependency read as
        perfectly healthy.
        """
        _, cascade_service, coordinator = observation

        policy.execute(lambda: value)

        assert cb_service.record_failure.called is records_failure
        assert cb_service.record_success.called is not records_failure
        assert cascade_service.record_rate_limit_response.called is feeds_cascade
        assert coordinator.on_rate_limited.called is feeds_cascade

    def test_a_status_in_both_sets_records_a_failure_and_feeds_the_cascade(
        self, policy, cb_service, observation
    ):
        """Membership is non-exclusive, so an overlapping status does both.

        The dispatch this replaced was if/elif: an operator who listed 429 as a
        failure code silently lost cascade detection for it.
        """
        _, cascade_service, _ = observation
        reset_middleware_settings()
        try:
            with patch(
                "baldur.settings.middleware.get_middleware_settings",
                return_value=MagicMock(
                    spec=BaldurMiddlewareSettings,
                    cb_status_codes=[429, 503],
                    rate_limit_codes=[429],
                ),
            ):
                policy.execute(lambda: _response(status_code=429))
        finally:
            reset_middleware_settings()

        cb_service.record_failure.assert_called_once()
        cascade_service.record_rate_limit_response.assert_called_once_with(
            "payment_api"
        )

    def test_the_returned_value_is_handed_back_untouched(self, policy, observation):
        """The classification is an observation; the caller still gets its answer."""
        value = _response(status_code=429)

        result = policy.execute(lambda: value)

        assert result.value is value

    def test_a_returned_failure_status_still_reports_a_success_outcome(
        self, policy, observation
    ):
        """The pipeline outcome stays SUCCESS so no fallback fires and nothing raises.

        A returned response is data, not an exception — turning it into a
        FAILURE outcome would divert the caller's own answer into a fallback
        they never asked for.
        """
        result = policy.execute(lambda: _response(status_code=503))

        assert result.outcome == PolicyOutcome.SUCCESS
        assert result.error is None

    def test_the_success_hook_still_fires_for_a_returned_failure_status(
        self, cb_service, observation
    ):
        """Hooks follow the pipeline outcome, not the recorded verdict."""
        hook = MagicMock(spec=PolicyHook)
        policy = CircuitBreakerPolicy(
            service_name="payment_api", cb_service=cb_service, hooks=[hook]
        )

        policy.execute(lambda: _response(status_code=429))

        hook.on_success.assert_called_once()
        hook.on_failure.assert_not_called()

    def test_a_returned_429_writes_exactly_one_request_to_the_denominator(
        self, policy, observation
    ):
        """One call, one request — the rate's denominator is not the 429 counter.

        Writing it here *and* in the cascade would count the same call twice,
        capping the observed rate at 50% in a pure storm.
        """
        tracker, _, _ = observation

        policy.execute(lambda: _response(status_code=429))

        tracker.record_request.assert_called_once_with("payment_api")

    def test_the_failure_context_names_the_returned_status(
        self, policy, cb_service, observation
    ):
        """The recorded evidence says which status the response carried."""
        policy.execute(lambda: _response(status_code=503))

        error_context = cb_service.record_failure.call_args.kwargs["error_context"]
        assert error_context["error"] == "HTTP 503"
        assert error_context["type"] == "response_status"


# =============================================================================
# Behavior — _on_failure(): the raised half
# =============================================================================


class TestCircuitBreakerPolicyRaisedOutcomeBehavior:
    """A raised outcome: what it counts, and what it must not."""

    def test_a_cooldown_deferral_writes_neither_a_request_nor_a_failure(
        self, policy, cb_service, observation
    ):
        """A deferral is not a dependency call, so it moves neither side of the rate.

        Counting a request for it would dilute the cascade rate with calls the
        cooldown itself prevented, so a storm would look milder the harder the
        cooldown worked.
        """
        tracker, _, _ = observation

        with pytest.raises(RateLimitDeferredError):
            policy.execute(
                _raise(RateLimitDeferredError(key="payment_api", not_before=1.0))
            )

        tracker.record_request.assert_not_called()
        cb_service.record_failure.assert_not_called()

    def test_an_ordinary_failure_writes_one_request(
        self, policy, cb_service, observation
    ):
        """A call that was actually made belongs in the denominator."""
        tracker, _, _ = observation

        with pytest.raises(ConnectionError):
            policy.execute(_raise(ConnectionError("connection reset")))

        tracker.record_request.assert_called_once_with("payment_api")
        cb_service.record_failure.assert_called_once()

    def test_a_raised_429_records_a_failure_and_feeds_the_cascade(
        self, policy, cb_service, observation
    ):
        """The exception-borne convention keeps the behaviour it always had."""
        _, cascade_service, coordinator = observation

        with pytest.raises(ThrottledError):
            policy.execute(_raise(ThrottledError()))

        cb_service.record_failure.assert_called_once()
        cascade_service.record_rate_limit_response.assert_called_once_with(
            "payment_api"
        )
        coordinator.on_rate_limited.assert_called_once()

    def test_an_ignored_429_type_feeds_no_cascade(self, cb_service, observation):
        """Same gate as the breaker's own count, by design.

        A caller who ignores their client's rate-limit exception type ignores it
        everywhere — leaving the cascade fed while the failure count is not
        would trip a breaker on a signal its owner opted out of.
        """
        _, cascade_service, coordinator = observation
        policy = CircuitBreakerPolicy(
            service_name="payment_api",
            cb_service=cb_service,
            ignore_exceptions=(ThrottledError,),
        )

        with pytest.raises(ThrottledError):
            policy.execute(_raise(ThrottledError()))

        cb_service.record_failure.assert_not_called()
        cascade_service.record_rate_limit_response.assert_not_called()
        coordinator.on_rate_limited.assert_not_called()

    def test_an_outcome_an_inner_stage_already_classified_feeds_no_second_cascade(
        self, policy, observation
    ):
        """The identity mark is what makes "counted exactly once" true.

        The retry ladder counts each attempt; the breaker stage sees only the
        final outcome. Without the mark, the last attempt's 429 would be counted
        twice — once by the ladder, once again here.
        """
        _, cascade_service, coordinator = observation
        error = ThrottledError()

        def raise_after_marking():
            current_scope().mark_classified(error)
            raise error

        with pytest.raises(ThrottledError):
            policy.execute(raise_after_marking)

        cascade_service.record_rate_limit_response.assert_not_called()
        coordinator.on_rate_limited.assert_called_once()

    def test_a_claimed_call_gets_no_cooldown_from_the_breaker_stage(
        self, policy, observation
    ):
        """A stage that carries its own coordination decision keeps it.

        This is what makes ``rate_limit_aware=False`` mean what it says even
        though the breaker stage sits above the stage the caller configured.
        """
        _, cascade_service, coordinator = observation
        error = ThrottledError()

        def raise_after_claiming():
            current_scope().claim_coordination()
            raise error

        with pytest.raises(ThrottledError):
            policy.execute(raise_after_claiming)

        cascade_service.record_rate_limit_response.assert_called_once()
        coordinator.on_rate_limited.assert_not_called()

    def test_an_inner_stage_that_counted_attempts_owns_the_denominator(
        self, policy, observation
    ):
        """``attempts != 0`` means the request writes are already accounted for."""
        tracker, _, _ = observation

        def count_two_attempts():
            scope = current_scope()
            scope.note_attempt()
            scope.note_attempt()
            raise ConnectionError("connection reset")

        with pytest.raises(ConnectionError):
            policy.execute(count_two_attempts)

        assert tracker.record_request.call_count == 2


# =============================================================================
# Behavior — the per-call observation scope's lifecycle
# =============================================================================


class TestCircuitBreakerPolicyScopeBehavior:
    """The scope is opened exactly where a dependency call this stage owns runs."""

    def test_an_admitted_call_runs_under_a_scope_keyed_by_the_protected_name(
        self, policy, observation
    ):
        """Inner stages file their counts under the breaker that will trip."""
        seen = {}

        def business_call():
            seen["scope"] = current_scope()
            return "ok"

        policy.execute(business_call)

        assert seen["scope"] is not None
        assert seen["scope"].breaker_key == "payment_api"

    def test_a_disabled_breaker_opens_no_scope(self, cb_service, observation):
        """The direct verdict runs no call this stage owns, so it counts nothing.

        A scope here would create a phantom record for a breaker that can never
        trip — and, worse, one whose 429s no breaker would ever read.
        """
        cb_service.is_enabled = False
        policy = CircuitBreakerPolicy(service_name="payment_api", cb_service=cb_service)
        seen = {}

        policy.execute(lambda: seen.setdefault("scope", current_scope()))

        assert seen["scope"] is None

    def test_a_rejected_call_leaves_no_scope_behind(self, cb_service, observation):
        """The reject verdict never runs the function and never publishes a scope."""
        cb_service.should_allow_with_state.return_value = CircuitBreakerDecision(
            allowed=False,
            state=CircuitBreakerStateData(service_name="payment_api", state="open"),
        )
        policy = CircuitBreakerPolicy(service_name="payment_api", cb_service=cb_service)

        result = policy.execute(lambda: "never runs")

        assert result.outcome == PolicyOutcome.REJECTED
        assert current_scope() is None

    def test_the_scope_is_closed_after_a_successful_call(self, policy, observation):
        """Closed in ``finally``: the next call on this thread starts clean."""
        policy.execute(lambda: "ok")

        assert current_scope() is None

    def test_the_scope_is_closed_after_a_raised_call(self, policy, observation):
        """The exception exit takes the same ``finally``.

        A scope left open would file the *next* call's 429s under this call's
        breaker key — and the leak would survive for the worker's lifetime.
        """
        with pytest.raises(ConnectionError):
            policy.execute(_raise(ConnectionError("connection reset")))

        assert current_scope() is None

    def test_a_nested_protected_call_restores_the_outer_scope(
        self, cb_service, observation
    ):
        """An inner ``@protected`` must not leave its own key current."""
        inner = CircuitBreakerPolicy(
            service_name="inventory_api", cb_service=cb_service
        )
        outer = CircuitBreakerPolicy(service_name="payment_api", cb_service=cb_service)
        seen = {}

        def outer_call():
            inner.execute(lambda: "inner ok")
            seen["after_inner"] = current_scope().breaker_key
            return "ok"

        outer.execute(outer_call)

        assert seen["after_inner"] == "payment_api"

    def test_the_async_policy_opens_the_same_scope(self, policy, observation):
        """Async parity: an awaited call is observed exactly as a sync one is."""
        seen = {}

        async def business_call():
            seen["scope"] = current_scope()
            return "ok"

        asyncio.run(AsyncCircuitBreakerPolicy(policy).execute(business_call))

        assert seen["scope"] is not None
        assert seen["scope"].breaker_key == "payment_api"
        assert current_scope() is None

    def test_the_async_policy_closes_the_scope_after_a_raised_call(
        self, policy, observation
    ):
        """The async ``finally`` mirrors the sync one."""

        async def business_call():
            raise ConnectionError("connection reset")

        with pytest.raises(ConnectionError):
            asyncio.run(AsyncCircuitBreakerPolicy(policy).execute(business_call))

        assert current_scope() is None

    def test_the_decorator_observes_a_returned_429_on_a_sync_function(
        self, cb_service, observation
    ):
        """``@circuit_breaker`` on a ``def`` reaches the same classification point."""
        _, cascade_service, _ = observation

        @circuit_breaker("payment_api", cb_service=cb_service)
        def call_payment_api():
            return _response(status_code=429)

        call_payment_api()

        cascade_service.record_rate_limit_response.assert_called_once_with(
            "payment_api"
        )

    def test_the_decorator_observes_a_returned_429_on_an_async_function(
        self, cb_service, observation
    ):
        """``@circuit_breaker`` on an ``async def`` covers the async path too."""
        _, cascade_service, _ = observation

        @circuit_breaker("payment_api", cb_service=cb_service)
        async def call_payment_api():
            return _response(status_code=429)

        asyncio.run(call_payment_api())

        cascade_service.record_rate_limit_response.assert_called_once_with(
            "payment_api"
        )


def _raise(error):
    """A callable that raises ``error`` when the policy runs it."""

    def _call():
        raise error

    return _call
