"""The asynchronous retry stage's share of the outbound 429 observation.

Target: resilience/policies/async_retry.py
- ``execute()``: the observation-scope claim in every mode, the per-attempt
  count, and the per-attempt 429 observation the breaker stage above cannot see
- ``_aobserve_attempt_outcome()`` / ``_anotify_rate_limit_cooldown()``: raised,
  returned-accepted and returned-rejected 429s each advance the cooldown once;
  classify-once ownership when an inner surface already marked the outcome
- the gated ``aon_success``; the inner deferral that counts no attempt
- the synthesised exhaustion error, marked so it is classified exactly once
- the enclosing breaker's ``ignore_exceptions`` reaching those attempts

Twin of ``services/test_retry_outbound_429_observation.py``. The breaker stage
sees one outcome per protected call; this stage sees every attempt. Every test
here must be able to fail because a storm the ladder overcame was swallowed as
a single success — or counted twice.
"""

from __future__ import annotations

import asyncio
import threading
from unittest.mock import MagicMock, patch

import pytest

from baldur.adapters.rate_limit.memory_adapter import InMemoryRateLimitStorage
from baldur.core.backoff import ConstantBackoff
from baldur.core.exceptions import RateLimitDeferredError
from baldur.core.execution_mode import (
    ExecutionMode,
    clear_execution_mode_override,
    set_execution_mode,
)
from baldur.interfaces.repositories import CircuitBreakerStateData
from baldur.interfaces.resilience_policy import PolicyOutcome
from baldur.resilience.policies.async_retry import AsyncRetryPolicy
from baldur.services.circuit_breaker.config import CircuitBreakerDecision
from baldur.services.circuit_breaker.policy import (
    AsyncCircuitBreakerPolicy,
    CircuitBreakerPolicy,
)
from baldur.services.circuit_breaker.rate_limit_observation import (
    close_scope,
    open_scope,
)
from baldur.services.circuit_breaker.rate_limit_tracker import RateLimitTracker
from baldur.services.circuit_breaker.service import CircuitBreakerService
from baldur.services.rate_limit_coordinator import RateLimitCoordinator
from baldur.services.rate_limit_coordinator.models import (
    RateLimitCoordinatorConfig,
    RateLimitResult,
)
from baldur.services.retry_handler.models import RetryPolicyConfig
from baldur.settings.retry import reset_retry_settings

_TRACKER = "baldur.services.circuit_breaker.rate_limit_tracker.get_rate_limit_tracker"
_CB_SERVICE = "baldur.services.circuit_breaker.convenience.get_circuit_breaker_service"

# A message the shared 429 classifier recognises.
_RATE_LIMIT_MESSAGE = "429 too many requests"


def _response(status_code):
    """A returned-value double a client hands back instead of raising."""
    return type("FakeResponse", (), {"status_code": status_code})()


def _policy(**config_kwargs) -> AsyncRetryPolicy:
    """The async stage with no injected coordinator and no real delays."""
    config_kwargs.setdefault("max_attempts", 1)
    config_kwargs.setdefault("domain", "payment")
    return AsyncRetryPolicy.from_policy_config(
        RetryPolicyConfig(**config_kwargs), backoff=ConstantBackoff(delay=0.0)
    )


def _injected_coordinator():
    """A spec'd coordinator handed to a policy, admitting every attempt."""
    coordinator = MagicMock(spec=RateLimitCoordinator)
    coordinator.await_if_needed.return_value = RateLimitResult(waited=False)
    coordinator.aon_rate_limited.return_value = 0.0
    return coordinator


def _injected_policy(coordinator, **kwargs) -> AsyncRetryPolicy:
    """The async stage with an injected coordinator, built the constructor way.

    The facade never injects a coordinator, so the injection seam is the
    constructor's own; ``max_attempts`` is translated the way the config
    mapping does it (total attempts -> additional retries).
    """
    max_attempts = kwargs.pop("max_attempts", 1)
    kwargs.setdefault("domain", "payment")
    return AsyncRetryPolicy(
        max_retries=max(max_attempts - 1, 0),
        backoff=ConstantBackoff(delay=0.0),
        rate_limit_coordinator=coordinator,
        **kwargs,
    )


async def _ok():
    return "ok"


def _run(policy, func=_ok):
    return asyncio.run(policy.execute(func))


@pytest.fixture
def observation():
    """The observation sinks the scope reaches. Yields ``(tracker, cascade)``."""
    tracker = MagicMock(spec=RateLimitTracker)
    cascade_service = MagicMock(spec=CircuitBreakerService)
    with (
        patch(_TRACKER, return_value=tracker),
        patch(_CB_SERVICE, return_value=cascade_service),
    ):
        yield tracker, cascade_service


@pytest.fixture(autouse=True)
def default_singleton_coordinator():
    """Stand a spec'd mock in for the process-wide coordinator singleton."""
    coordinator = _injected_coordinator()
    with patch.object(
        RateLimitCoordinator, "get_instance", autospec=True, return_value=coordinator
    ):
        yield coordinator


@pytest.fixture
def scope():
    """An open observation scope, as a breaker stage above would publish one."""
    token, record = open_scope("payment")
    try:
        yield record
    finally:
        close_scope(token)


# =============================================================================
# Behavior — the scope claim and the per-attempt count
# =============================================================================


class TestAsyncRetryObservationScopeBehavior:
    """This stage claims coordination in every mode and counts every attempt."""

    def test_the_claim_survives_a_globally_disabled_retry_stage(
        self, monkeypatch, scope, observation
    ):
        """A disabled loop still carries the caller's coordination decision.

        Behavioural change, recorded and intentional: an async call with
        retry disabled used to leave the scope unclaimed, so the breaker stage
        installed a per-sequence cooldown for it. It now coordinates nothing,
        exactly as the synchronous coverage statement says.
        """
        monkeypatch.setenv("BALDUR_RETRY_ENABLED", "false")
        reset_retry_settings()
        try:
            _run(_policy())
        finally:
            reset_retry_settings()

        assert scope.coordination_claimed is True

    def test_the_claim_survives_observe_only_mode(self, scope, observation):
        set_execution_mode(ExecutionMode.shadow())
        try:
            _run(_policy())
        finally:
            clear_execution_mode_override()

        assert scope.coordination_claimed is True

    def test_the_claim_is_made_before_the_loop_runs(self, scope, observation):
        _run(_policy())

        assert scope.coordination_claimed is True

    def test_one_attempt_is_counted_for_each_call_of_the_function(
        self, scope, observation
    ):
        """The ladder owns the denominator once it starts making calls."""
        tracker, _ = observation
        calls = {"n": 0}

        async def flaky():
            calls["n"] += 1
            if calls["n"] < 3:
                raise ConnectionError("connection reset")
            return "ok"

        _run(_policy(max_attempts=3), flaky)

        assert calls["n"] == 3
        assert scope.attempts == 3
        assert tracker.record_request.call_count == 3

    def test_a_cooldown_deferral_counts_no_attempt(
        self, scope, observation, default_singleton_coordinator
    ):
        """A refused call never reached the dependency, so it is not a request."""
        tracker, _ = observation
        default_singleton_coordinator.await_if_needed.return_value = RateLimitResult(
            deferred=True, not_before=1_700_000_000.0
        )
        calls = {"n": 0}

        async def func():
            calls["n"] += 1
            return "ok"

        result = _run(_policy(max_attempts=2), func)

        assert calls["n"] == 0
        assert result.metadata["reason"] == "rate_limit_deferred"
        assert scope.attempts == 0
        tracker.record_request.assert_not_called()

    def test_every_attempt_borne_429_is_observed_separately(self, scope, observation):
        """N attempts against a throttling dependency are N cascade observations."""
        _, cascade_service = observation
        calls = {"n": 0}

        async def throttled_then_ok():
            calls["n"] += 1
            if calls["n"] < 3:
                raise Exception(_RATE_LIMIT_MESSAGE)
            return "ok"

        _run(_policy(max_attempts=3), throttled_then_ok)

        assert scope.rate_limited == 2
        assert cascade_service.record_rate_limit_response.call_count == 2

    def test_a_call_with_no_scope_counts_nothing_and_does_not_raise(self, observation):
        """A bare async ``@retry`` has no breaker to trip, so it creates no record."""
        tracker, cascade_service = observation

        result = _run(_policy())

        assert result.value == "ok"
        tracker.record_request.assert_not_called()
        cascade_service.record_rate_limit_response.assert_not_called()

    def test_the_breaker_frame_writes_no_second_request_for_a_counted_attempt(
        self, observation
    ):
        """``scope.attempts`` is non-zero, so the frame above leaves the denominator alone."""
        tracker, _ = observation
        cb_service = MagicMock(spec=CircuitBreakerService)
        cb_service.is_enabled = True
        cb_service.should_allow_with_state.return_value = CircuitBreakerDecision(
            allowed=True,
            state=CircuitBreakerStateData(service_name="payment", state="closed"),
        )
        breaker = AsyncCircuitBreakerPolicy(
            CircuitBreakerPolicy(service_name="payment", cb_service=cb_service)
        )
        retry = _policy(max_attempts=2)
        calls = {"n": 0}

        async def flaky():
            calls["n"] += 1
            if calls["n"] == 1:
                raise ConnectionError("connection reset")
            return "ok"

        async def inner():
            result = await retry.execute(flaky)
            return result.value

        asyncio.run(breaker.execute(inner))

        assert tracker.record_request.call_count == 2


# =============================================================================
# Behavior — each observed 429 advances the cooldown exactly once
# =============================================================================


class TestAsyncRetryOutbound429ObservationBehavior:
    """Raised or returned, a 429 is one ``aon_rate_limited`` — never two."""

    @pytest.mark.parametrize(
        ("status", "rejected", "expected_observations"),
        [
            (200, False, 0),
            (429, False, 1),
            (429, True, 1),
            (500, True, 0),
        ],
        ids=[
            "accepted_non_429",
            "accepted_429",
            "rejected_429",
            "rejected_non_429",
        ],
    )
    def test_every_result_branch_classifies_its_value_exactly_once(
        self, scope, observation, status, rejected, expected_observations
    ):
        """Accepted or rejected, a returned value passes the classifier once."""
        _, cascade_service = observation
        coordinator = _injected_coordinator()
        response = _response(status)

        async def func():
            return response

        _run(
            _injected_policy(
                coordinator, max_attempts=1, retry_on_result=(lambda r: rejected)
            ),
            func,
        )

        assert scope.rate_limited == expected_observations
        assert (
            cascade_service.record_rate_limit_response.call_count
            == expected_observations
        )
        assert coordinator.aon_rate_limited.await_count == expected_observations

    def test_a_raised_429_advances_the_cooldown_exactly_once(self, scope, observation):
        coordinator = _injected_coordinator()

        async def throttled():
            raise Exception(_RATE_LIMIT_MESSAGE)

        _run(_injected_policy(coordinator, max_attempts=1), throttled)

        assert coordinator.aon_rate_limited.await_count == 1
        assert coordinator.aon_rate_limited.await_count != 2
        assert coordinator.aon_rate_limited.call_args.kwargs["key"] == "payment"
        assert scope.rate_limited == 1

    def test_a_retry_after_reaches_the_awaitable_twin(self, observation):
        class ThrottledError(Exception):
            retry_after = 30.0

        coordinator = _injected_coordinator()

        async def throttled():
            raise ThrottledError(_RATE_LIMIT_MESSAGE)

        _run(_injected_policy(coordinator, max_attempts=1), throttled)

        assert coordinator.aon_rate_limited.call_args.kwargs["retry_after"] == 30.0

    def test_a_returned_429_that_a_predicate_retries_installs_one_cooldown_per_attempt(
        self, observation
    ):
        coordinator = _injected_coordinator()

        async def func():
            return _response(429)

        _run(
            _injected_policy(
                coordinator,
                max_attempts=2,
                retry_on_result=lambda r: r.status_code == 429,
            ),
            func,
        )

        assert coordinator.aon_rate_limited.await_count == 2

    def test_an_accepted_429_is_never_read_as_a_reset(self, scope, observation):
        """Resetting on a 429 drops the ladder back to its base delay mid-storm."""
        coordinator = _injected_coordinator()

        async def func():
            return _response(429)

        _run(_injected_policy(coordinator, max_attempts=1), func)

        coordinator.aon_rate_limited.assert_awaited_once()
        coordinator.aon_success.assert_not_called()

    def test_a_later_non_429_success_does_reset_the_counter_once(
        self, scope, observation
    ):
        """The reset is owed once the storm actually clears — and only once."""
        coordinator = _injected_coordinator()
        calls = {"n": 0}

        async def throttled_then_ok():
            calls["n"] += 1
            if calls["n"] == 1:
                raise Exception(_RATE_LIMIT_MESSAGE)
            return "ok"

        _run(_injected_policy(coordinator, max_attempts=2), throttled_then_ok)

        coordinator.aon_rate_limited.assert_awaited_once()
        coordinator.aon_success.assert_awaited_once_with("payment")

    def test_a_success_with_no_signal_never_resets(self, scope, observation):
        coordinator = _injected_coordinator()

        _run(_injected_policy(coordinator, max_attempts=1))

        coordinator.aon_success.assert_not_called()

    def test_each_attempt_outcome_is_marked_on_the_scope(self, scope, observation):
        """The mark is what stops the breaker stage classifying the same object."""
        response = _response(429)

        async def func():
            return response

        _run(_policy(max_attempts=1), func)

        assert scope.was_classified(response) is True

    def test_a_non_429_outcome_is_marked_but_not_observed(self, scope, observation):
        error = ConnectionError("connection reset")

        async def func():
            raise error

        _run(_policy(max_attempts=1), func)

        assert scope.was_classified(error) is True
        assert scope.rate_limited == 0


# =============================================================================
# Behavior — classify-once ownership with an inner surface
# =============================================================================


class TestAsyncRetryClassifyOnceBehavior:
    """Whoever classified the outcome first owns the cooldown."""

    @pytest.fixture(autouse=True)
    def _no_cluster_broadcast(self):
        with patch.object(RateLimitCoordinator, "_broadcast_to_cluster", autospec=True):
            yield

    @staticmethod
    def _real_coordinator(storage):
        return RateLimitCoordinator(
            storage=storage,
            config=RateLimitCoordinatorConfig(
                jitter_percent=0.0,
                debounce_window_seconds=0.0,
                default_retry_after=0.0,
            ),
        )

    def test_a_decorated_client_inside_the_loop_installs_one_cooldown_per_429(
        self, scope, observation
    ):
        """The reproduction: async decorator and async loop on one key, one 429.

        Before the ownership rule both surfaces notified, so the consecutive
        counter advanced by two per answer; it now advances by one, and the
        loop still sees the signal that gates its success reset.
        """
        storage = InMemoryRateLimitStorage()
        coordinator = self._real_coordinator(storage)
        response = _response(429)

        @coordinator.rate_limit_aware("payment")
        async def client():
            return response

        result = _run(_injected_policy(coordinator, max_attempts=1), client)

        assert result.value is response
        assert storage.get_state("payment").consecutive_429s == 1
        assert storage.get_state("payment").consecutive_429s != 2
        assert scope.rate_limited == 1

    def test_a_decorated_client_that_raises_inside_the_loop_installs_one_cooldown(
        self, scope, observation
    ):
        """The raised half of the same rule: the decorator marks, the loop defers to it."""
        storage = InMemoryRateLimitStorage()
        coordinator = self._real_coordinator(storage)

        @coordinator.rate_limit_aware("payment")
        async def client():
            raise Exception(_RATE_LIMIT_MESSAGE)

        _run(_injected_policy(coordinator, max_attempts=1), client)

        assert storage.get_state("payment").consecutive_429s == 1
        assert scope.rate_limited == 1

    def test_an_already_marked_429_is_a_signal_without_a_second_cooldown(
        self, scope, observation
    ):
        """At the helper: an outcome an inner surface marked is detected, not re-noted."""
        coordinator = _injected_coordinator()
        response = _response(429)
        scope.mark_classified(response)

        detected = asyncio.run(
            AsyncRetryPolicy._anotify_rate_limit_cooldown(
                coordinator, "payment", response, scope
            )
        )

        assert detected is True
        assert scope.rate_limited == 0
        coordinator.aon_rate_limited.assert_not_called()

    def test_an_unmarked_429_takes_the_full_path(self, scope, observation):
        """Discriminator for the row above."""
        coordinator = _injected_coordinator()
        response = _response(429)

        detected = asyncio.run(
            AsyncRetryPolicy._anotify_rate_limit_cooldown(
                coordinator, "payment", response, scope
            )
        )

        assert detected is True
        assert scope.rate_limited == 1
        coordinator.aon_rate_limited.assert_awaited_once()
        assert scope.was_classified(response) is True


# =============================================================================
# Behavior — an inner surface's cooldown deferral
# =============================================================================


class TestAsyncRetryInnerDeferralBehavior:
    """A deferral raised inside the call stops the loop with the defer vocabulary."""

    @pytest.fixture
    def deferral(self):
        return RateLimitDeferredError(key="inner-provider", not_before=1_700_000_000.0)

    def test_an_inner_deferral_counts_no_attempt(self, scope, observation, deferral):
        tracker, _ = observation

        async def func():
            raise deferral

        _run(_policy(max_attempts=3), func)

        assert scope.attempts == 0
        tracker.record_request.assert_not_called()

    def test_an_inner_deferral_exits_with_the_defer_vocabulary(
        self, observation, deferral
    ):
        calls = {"n": 0}

        async def func():
            calls["n"] += 1
            raise deferral

        result = _run(_policy(max_attempts=3), func)

        assert result.outcome == PolicyOutcome.FAILURE
        assert result.metadata["reason"] == "rate_limit_deferred"
        assert result.metadata["reason"] != "non_retryable"
        assert result.metadata["reason"] != "max_attempts"
        assert result.metadata["not_before"] == deferral.not_before
        assert result.metadata["rate_limit_key"] == "payment"
        assert result.error is deferral
        assert calls["n"] == 1

    def test_an_inner_deferral_is_never_a_provider_429(
        self, scope, observation, deferral, default_singleton_coordinator
    ):
        async def func():
            raise deferral

        _run(_policy(max_attempts=3), func)

        default_singleton_coordinator.aon_rate_limited.assert_not_called()
        assert scope.rate_limited == 0


# =============================================================================
# Behavior — the synthesised exhaustion error
# =============================================================================


class TestAsyncRetryExhaustionMarkBehavior:
    """A result-rejection exhaustion synthesises its own error — and marks it."""

    def test_the_synthesised_exhaustion_error_is_marked_before_it_propagates(
        self, scope, observation
    ):
        async def func():
            return "rejected value"

        result = _run(
            _policy(
                max_attempts=2,
                domain="throttle-sensitive",
                retry_on_result=lambda r: True,
            ),
            func,
        )

        assert result.error is not None
        assert scope.was_classified(result.error) is True

    def test_a_domain_named_throttle_is_not_counted_as_a_fresh_429(
        self, scope, observation
    ):
        _, cascade_service = observation

        async def func():
            return _response(429)

        _run(
            _policy(
                max_attempts=2,
                domain="throttle-sensitive",
                retry_on_result=lambda r: True,
            ),
            func,
        )

        assert scope.rate_limited == 2
        assert cascade_service.record_rate_limit_response.call_count == 2


# =============================================================================
# Behavior — the enclosing breaker's ignore list
# =============================================================================


class _IgnoredRateLimitError(Exception):
    """A client's 429 exception the caller told the breaker not to count."""


def _admitting_async_breaker(**policy_kwargs) -> AsyncCircuitBreakerPolicy:
    cb = MagicMock(spec=CircuitBreakerService)
    cb.is_enabled = True
    cb.should_allow_with_state.return_value = CircuitBreakerDecision(
        allowed=True,
        state=CircuitBreakerStateData(service_name="payment", state="closed"),
    )
    return AsyncCircuitBreakerPolicy(
        CircuitBreakerPolicy(service_name="payment", cb_service=cb, **policy_kwargs)
    )


class TestAsyncRetryObservationIgnoreListBehavior:
    """A 429 type the breaker ignores is ignored at every depth that sees it."""

    def test_an_ignored_429_exception_feeds_no_cascade(self, observation):
        _, cascade = observation
        breaker = _admitting_async_breaker(ignore_exceptions=(_IgnoredRateLimitError,))
        retry = _policy(max_attempts=2)

        async def raise_429():
            raise _IgnoredRateLimitError(_RATE_LIMIT_MESSAGE)

        async def inner():
            return await retry.execute(raise_429)

        asyncio.run(breaker.execute(inner))

        cascade.record_rate_limit_response.assert_not_called()

    def test_the_same_storm_feeds_the_cascade_without_the_ignore_list(
        self, observation
    ):
        """Negative half: the dial is what withholds it, not the composition."""
        _, cascade = observation
        breaker = _admitting_async_breaker()
        retry = _policy(max_attempts=2)

        async def raise_429():
            raise _IgnoredRateLimitError(_RATE_LIMIT_MESSAGE)

        async def inner():
            return await retry.execute(raise_429)

        asyncio.run(breaker.execute(inner))

        assert cascade.record_rate_limit_response.call_count == 2


# =============================================================================
# _anotify_rate_limit_cooldown() — the cascade note leaves the event loop
# =============================================================================


class TestAsyncRetryCascadeNoteLeavesTheLoopBehavior:
    """The 429 cascade note is not pure: it reaches the breaker's tracker and
    can trip the breaker (a repository write plus an event publish), so on this
    stage it runs on a worker thread like the cooldown write beside it.

    Regression: the note ran inline on the loop while only the cooldown write
    was hopped (792 /verify, refuted claim).
    """

    @pytest.mark.parametrize(
        "shape", ["raised", "returned"], ids=["raised_429", "returned_429"]
    )
    def test_the_cascade_note_runs_off_the_loop_thread(self, observation, shape):
        """``record_rate_limit_response`` is reached from a thread that is not the loop's."""
        _tracker, cascade_service = observation
        seen_threads: list[threading.Thread] = []
        cascade_service.record_rate_limit_response.side_effect = lambda *_a, **_k: (
            seen_threads.append(threading.current_thread())
        )
        coordinator = _injected_coordinator()
        policy = _injected_policy(coordinator)

        async def throttled():
            if shape == "raised":
                raise Exception(_RATE_LIMIT_MESSAGE)
            return _response(429)

        token, scope = open_scope("payment")
        try:
            loop_thread = threading.current_thread()
            _run(policy, throttled)
        finally:
            close_scope(token)

        assert scope.rate_limited == 1
        assert seen_threads != []
        assert all(thread is not loop_thread for thread in seen_threads)
