"""The retry stage's share of the outbound 429 observation.

Target: services/retry_handler/policy.py
- ``execute()``: the observation-scope claim, the per-attempt count, and the
  per-attempt 429 observation the breaker stage above cannot see
- the enclosing breaker's ``ignore_exceptions`` reaching those attempts
- the four result branches, including a 429 a client *returned*
- the synthesised exhaustion error, marked so it is classified exactly once

The breaker stage sees one outcome per protected call; this stage sees every
attempt. Every test here must be able to fail because a storm the ladder
overcame was swallowed as a single success — which is what the shared record
exists to prevent.
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from baldur.core.backoff import ConstantBackoff
from baldur.core.execution_mode import (
    ExecutionMode,
    clear_execution_mode_override,
    set_execution_mode,
)
from baldur.services.circuit_breaker.policy import CircuitBreakerPolicy
from baldur.services.circuit_breaker.rate_limit_observation import (
    close_scope,
    open_scope,
)
from baldur.services.circuit_breaker.rate_limit_tracker import RateLimitTracker
from baldur.services.circuit_breaker.service import CircuitBreakerService
from baldur.services.rate_limit_coordinator import RateLimitCoordinator
from baldur.services.rate_limit_coordinator.models import RateLimitResult
from baldur.services.retry_handler.models import RetryPolicyConfig
from baldur.services.retry_handler.policy import RetryPolicy
from baldur.settings.retry import reset_retry_settings

_TRACKER = "baldur.services.circuit_breaker.rate_limit_tracker.get_rate_limit_tracker"
_CB_SERVICE = "baldur.services.circuit_breaker.convenience.get_circuit_breaker_service"

# A message the shared 429 classifier recognises.
_RATE_LIMIT_MESSAGE = "429 too many requests"


def _response(status_code):
    """A returned-value double a client hands back instead of raising."""
    return type("FakeResponse", (), {"status_code": status_code})()


def _policy(**config_kwargs) -> RetryPolicy:
    """A retry policy with no injected coordinator and no real delays."""
    config_kwargs.setdefault("max_attempts", 1)
    config_kwargs.setdefault("domain", "payment")
    return RetryPolicy(
        config=RetryPolicyConfig(**config_kwargs),
        backoff=ConstantBackoff(delay=0.0),
        sleeper=lambda _: None,
    )


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
    """Stand a spec'd mock in for the process-wide coordinator singleton.

    Every test here reaches the default resolution at least once. Left real, it
    builds storage auto-detect and a cluster broadcast per 429 — infrastructure
    this stage's observation behaviour does not depend on.
    """
    coordinator = MagicMock(spec=RateLimitCoordinator)
    coordinator.wait_if_needed.return_value = RateLimitResult(waited=False)
    with patch.object(
        RateLimitCoordinator, "get_instance", autospec=True, return_value=coordinator
    ):
        yield coordinator


def _injected_coordinator():
    """A spec'd coordinator handed to a policy, admitting every attempt."""
    coordinator = MagicMock(spec=RateLimitCoordinator)
    coordinator.wait_if_needed.return_value = RateLimitResult(waited=False)
    return coordinator


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


class TestRetryObservationScopeBehavior:
    """This stage claims coordination in every mode and counts every attempt."""

    def test_the_claim_survives_a_globally_disabled_retry_stage(
        self, monkeypatch, scope, observation
    ):
        """A disabled loop still carries the caller's coordination decision.

        The claim is the first statement of ``execute`` precisely so the two
        early returns below it cannot skip it. Without it, an operator who
        disabled retry would silently get the fleet-wide cooldown from the
        breaker stage that a ``rate_limit_aware=False`` caller opted out of.
        """
        monkeypatch.setenv("BALDUR_RETRY_ENABLED", "false")
        reset_retry_settings()
        try:
            _policy().execute(lambda: "ok")
        finally:
            reset_retry_settings()

        assert scope.coordination_claimed is True

    def test_the_claim_survives_observe_only_mode(self, scope, observation):
        """The other early return takes the same single-attempt path."""
        set_execution_mode(ExecutionMode.shadow())
        try:
            _policy().execute(lambda: "ok")
        finally:
            clear_execution_mode_override()

        assert scope.coordination_claimed is True

    def test_the_claim_is_made_before_the_loop_runs(self, scope, observation):
        """The ordinary path claims it too — one rule, not three."""
        _policy().execute(lambda: "ok")

        assert scope.coordination_claimed is True

    def test_one_attempt_is_counted_for_each_call_of_the_function(
        self, scope, observation
    ):
        """The ladder owns the denominator once it starts making calls.

        Counting the sequence as one call would put a three-attempt storm in the
        denominator once, so the cascade rate would read three times too high.
        """
        tracker, _ = observation
        calls = {"n": 0}

        def flaky():
            calls["n"] += 1
            if calls["n"] < 3:
                raise ConnectionError("connection reset")
            return "ok"

        _policy(max_attempts=3).execute(flaky)

        assert calls["n"] == 3
        assert scope.attempts == 3
        assert tracker.record_request.call_count == 3

    def test_a_cooldown_deferral_counts_no_attempt(
        self, scope, observation, default_singleton_coordinator
    ):
        """A refused call never reached the dependency, so it is not a request.

        Counting it would dilute the cascade rate with the very calls the
        cooldown prevented — the harder the cooldown worked, the milder the
        storm would look.
        """
        tracker, _ = observation
        default_singleton_coordinator.wait_if_needed.return_value = RateLimitResult(
            deferred=True, not_before=time.time() + 300.0
        )
        calls = {"n": 0}

        def func():
            calls["n"] += 1
            return "ok"

        result = _policy(max_attempts=2).execute(func)

        assert calls["n"] == 0
        assert result.metadata["reason"] == "rate_limit_deferred"
        assert scope.attempts == 0
        tracker.record_request.assert_not_called()

    def test_every_attempt_borne_429_is_observed_separately(self, scope, observation):
        """N attempts against a throttling dependency are N cascade observations.

        The breaker stage sees only the sequence's final outcome, so a storm the
        ladder eventually overcomes would otherwise be invisible to the cascade
        — exactly the storm the cascade exists to catch.
        """
        _, cascade_service = observation
        calls = {"n": 0}

        def throttled_then_ok():
            calls["n"] += 1
            if calls["n"] < 3:
                raise Exception(_RATE_LIMIT_MESSAGE)
            return "ok"

        _policy(max_attempts=3).execute(throttled_then_ok)

        assert scope.rate_limited == 2
        assert cascade_service.record_rate_limit_response.call_count == 2

    def test_a_call_with_no_scope_counts_nothing_and_does_not_raise(self, observation):
        """A bare ``@retry`` has no breaker to trip, so it creates no record."""
        tracker, cascade_service = observation

        result = _policy().execute(lambda: "ok")

        assert result.value == "ok"
        tracker.record_request.assert_not_called()
        cascade_service.record_rate_limit_response.assert_not_called()


# =============================================================================
# Behavior — the four result branches
# =============================================================================


class TestRetryResultBorne429Behavior:
    """A client's calling convention is not evidence about the dependency."""

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
        """Accepted or rejected, a returned value passes the classifier once.

        While detection was exception-borne, a returned 429 fed nothing at all —
        even when a result predicate retried on it, so the loop hammered a
        throttled provider with the very coordination it was built to prevent.
        """
        _, cascade_service = observation
        response = _response(status)

        _policy(
            max_attempts=1,
            retry_on_result=(lambda r: rejected),
        ).execute(lambda: response)

        assert scope.rate_limited == expected_observations
        assert (
            cascade_service.record_rate_limit_response.call_count
            == expected_observations
        )

    def test_an_accepted_429_is_never_read_as_a_reset(self, scope, observation):
        """Resetting on a 429 drops the ladder back to its base delay mid-storm."""
        coordinator = _injected_coordinator()
        policy = RetryPolicy(
            config=RetryPolicyConfig(max_attempts=1, domain="payment"),
            backoff=ConstantBackoff(delay=0.0),
            sleeper=lambda _: None,
            rate_limit_coordinator=coordinator,
        )

        policy.execute(lambda: _response(429))

        coordinator.on_rate_limited.assert_called_once()
        coordinator.on_success.assert_not_called()

    def test_a_later_non_429_success_does_reset_the_counter(self, scope, observation):
        """The reset is owed once the storm actually clears."""
        coordinator = _injected_coordinator()
        policy = RetryPolicy(
            config=RetryPolicyConfig(max_attempts=2, domain="payment"),
            backoff=ConstantBackoff(delay=0.0),
            sleeper=lambda _: None,
            rate_limit_coordinator=coordinator,
        )
        calls = {"n": 0}

        def throttled_then_ok():
            calls["n"] += 1
            if calls["n"] == 1:
                raise Exception(_RATE_LIMIT_MESSAGE)
            return "ok"

        policy.execute(throttled_then_ok)

        coordinator.on_rate_limited.assert_called_once()
        coordinator.on_success.assert_called_once_with("payment")

    def test_each_attempt_outcome_is_marked_on_the_scope(self, scope, observation):
        """The mark is what stops the breaker stage classifying the same object.

        The composer re-raises and returns by identity, so the object this loop
        saw is the object that reaches the stage above it.
        """
        response = _response(429)

        _policy(max_attempts=1).execute(lambda: response)

        assert scope.was_classified(response) is True


# =============================================================================
# Behavior — the synthesised exhaustion error
# =============================================================================


class TestRetryExhaustionMarkBehavior:
    """A result-rejection exhaustion synthesises its own error — and marks it."""

    def test_the_synthesised_exhaustion_error_is_marked_before_it_propagates(
        self, scope, observation
    ):
        """Its message carries the domain name, which the heuristic reads.

        A domain literally named ``throttle`` would otherwise reach the breaker
        stage looking like a fresh 429 from the dependency, and be counted as
        one on top of every attempt already counted below.
        """
        result = _policy(
            max_attempts=2,
            domain="throttle-sensitive",
            retry_on_result=lambda r: True,
        ).execute(lambda: "rejected value")

        assert result.error is not None
        assert scope.was_classified(result.error) is True

    def test_a_domain_named_throttle_is_not_counted_as_a_fresh_429(
        self, scope, observation
    ):
        """The mark's whole point, stated as the count it protects.

        Two rejected attempts are two observations of the rejected value — the
        synthesised carrier must not add a third.
        """
        _, cascade_service = observation

        _policy(
            max_attempts=2,
            domain="throttle-sensitive",
            retry_on_result=lambda r: True,
        ).execute(lambda: _response(429))

        assert scope.rate_limited == 2
        assert cascade_service.record_rate_limit_response.call_count == 2


# =============================================================================
# Behavior - the enclosing breaker's ignore list
# =============================================================================


class _IgnoredRateLimitError(Exception):
    """A client's 429 exception the caller told the breaker not to count."""


def _admitting_breaker(**policy_kwargs) -> CircuitBreakerPolicy:
    """A breaker stage that admits every call, wrapping a stubbed service."""
    cb = MagicMock(spec=CircuitBreakerService)
    cb.is_enabled = True
    cb.should_allow_with_state.return_value = SimpleNamespace(
        allowed=True, state=SimpleNamespace(state="closed")
    )
    return CircuitBreakerPolicy(service_name="payment", cb_service=cb, **policy_kwargs)


class TestRetryObservationIgnoreListBehavior:
    """A 429 type the breaker ignores is ignored at every depth that sees it.

    The breaker frame already gated its own cascade record on ``_is_failure``,
    but the retry ladder is where a storm is actually seen: every attempt it
    overcomes is one this stage reports and the frame above never will. Reading
    the dial only at the outer frame therefore left the ignore list bypassable
    by composing a retry stage under it.
    """

    def test_an_ignored_429_exception_feeds_no_cascade(self, observation):
        _, cascade = observation
        breaker = _admitting_breaker(ignore_exceptions=(_IgnoredRateLimitError,))
        retry = _policy(max_attempts=2)

        def raise_429():
            raise _IgnoredRateLimitError(_RATE_LIMIT_MESSAGE)

        breaker.execute(lambda: retry.execute(raise_429))

        cascade.record_rate_limit_response.assert_not_called()

    def test_the_same_storm_feeds_the_cascade_without_the_ignore_list(
        self, observation
    ):
        """Negative half: the dial is what withholds it, not the composition."""
        _, cascade = observation
        breaker = _admitting_breaker()
        retry = _policy(max_attempts=2)

        def raise_429():
            raise _IgnoredRateLimitError(_RATE_LIMIT_MESSAGE)

        breaker.execute(lambda: retry.execute(raise_429))

        assert cascade.record_rate_limit_response.call_count == 2

    def test_the_ignored_attempts_still_count_as_requests(self, observation):
        """The denominator is unfiltered: the breaker frame counts them too."""
        tracker, _ = observation
        breaker = _admitting_breaker(ignore_exceptions=(_IgnoredRateLimitError,))
        retry = _policy(max_attempts=2)

        def raise_429():
            raise _IgnoredRateLimitError(_RATE_LIMIT_MESSAGE)

        breaker.execute(lambda: retry.execute(raise_429))

        assert tracker.record_request.call_count == 2
