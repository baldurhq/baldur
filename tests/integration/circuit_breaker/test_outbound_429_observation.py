"""Outbound 429 observation across the composed resilience chain.

The exactly-once properties this design turns on are not visible from any one
stage. Three collaborators share mutable state through a single per-call record
and a process-wide tracker:

    1. ``CircuitBreakerPolicy`` opens the record around the business call and
       classifies the sequence's *final* outcome.
    2. ``RetryPolicy`` runs inside it and sees *every* attempt, mutating the
       same record in place and claiming the fleet-wide cooldown for the call.
    3. ``CircuitBreakerService.record_rate_limit_response`` is the single
       writer of the tracker's 429 counter, and the cascade verdict is a ratio
       of that counter to a denominator both stages write.

A unit test of any one stage can assert its own calls but not the ratio, so
"N attempts produce N observations and N requests", "one coordinator notify per
429" and "a deferral is counted by neither" are asserted here, over the real
``PolicyComposer`` chain.

Mock-based — no infra. The repository is the real in-memory implementation and
the tracker is the real in-process one, so the counter semantics under test are
the shipped ones. Only the coordinator (a network client) and the audit write
are stood in for.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from baldur.adapters.memory.circuit_breaker import (
    InMemoryCircuitBreakerStateRepository,
)
from baldur.core.backoff import ConstantBackoff
from baldur.interfaces.resilience_policy import PolicyOutcome
from baldur.resilience.policies.composer import PolicyComposer
from baldur.services.circuit_breaker.config import CircuitBreakerConfig
from baldur.services.circuit_breaker.policy import CircuitBreakerPolicy
from baldur.services.circuit_breaker.rate_limit_tracker import (
    RateLimitTracker,
    get_rate_limit_tracker,
    reset_rate_limit_tracker,
)
from baldur.services.circuit_breaker.service import CircuitBreakerService
from baldur.services.event_bus import EventType
from baldur.services.rate_limit_coordinator import RateLimitCoordinator
from baldur.services.rate_limit_coordinator.models import (
    RateLimitDeferredError,
    RateLimitResult,
)
from baldur.services.retry_handler.models import RetryPolicyConfig
from baldur.services.retry_handler.policy import RetryPolicy

SERVICE = "payment-api"

# A message the shared 429 classifier recognises.
_RATE_LIMIT_MESSAGE = "429 too many requests"


class _Throttled(Exception):
    """A client that raises on 429."""

    def __init__(self):
        super().__init__(_RATE_LIMIT_MESSAGE)


def _throttled_response():
    """A client that hands the 429 back as a value."""
    return type("FakeResponse", (), {"status_code": 429})()


def _cascade_config(**overrides) -> CircuitBreakerConfig:
    """A breaker whose cascade fires on a small, exactly-countable storm."""
    base = {
        "enabled": True,
        "failure_threshold": 100,
        "recovery_timeout": 60,
        "success_threshold": 1,
        "rate_limit_cascade_threshold": 3,
        "rate_limit_cascade_window_seconds": 60,
        "rate_limit_cascade_rate": 50.0,
        "rate_limit_cascade_minimum_calls": 3,
    }
    base.update(overrides)
    return CircuitBreakerConfig(**base)


class _Chain:
    """The real composed chain: breaker outside, retry ladder inside.

    Everything under test is production code — the composer's nesting, both
    policies, the real in-memory repository and the real tracker. The audit
    write and the burn-rate multiplier reach outside the circuit breaker and
    are the only things stood in for.
    """

    def __init__(self, config: CircuitBreakerConfig, *, max_attempts: int = 3) -> None:
        self.repository = InMemoryCircuitBreakerStateRepository()
        self.service = CircuitBreakerService(config=config, repository=self.repository)
        self.breaker = CircuitBreakerPolicy(
            service_name=SERVICE, cb_service=self.service
        )
        self.retry = RetryPolicy(
            config=RetryPolicyConfig(max_attempts=max_attempts, domain=SERVICE),
            backoff=ConstantBackoff(delay=0.0),
            sleeper=lambda _: None,
        )
        self.composer: PolicyComposer = (
            PolicyComposer().add(self.breaker).add(self.retry)
        )
        self.events: list[tuple[object, dict]] = []

    def _capture(self, event_type, data=None, **kwargs):
        self.events.append((event_type, data or kwargs.get("data") or {}))

    def execute(self, func):
        """Run ``func`` through the chain with the audit surfaces stood in for."""
        with (
            patch.object(self.service, "_log_circuit_open_audit"),
            patch.object(self.service, "_apply_burn_rate_multiplier"),
            patch.object(self.service, "_emit_event", side_effect=self._capture),
            patch(
                "baldur.services.circuit_breaker.convenience"
                ".get_circuit_breaker_service",
                return_value=self.service,
            ),
        ):
            return self.composer.execute(func)

    def cascade_counts(self) -> tuple[int, int]:
        """``(429s, requests)`` currently in the cascade window."""
        tracker = get_rate_limit_tracker()
        window = self.service.config.rate_limit_cascade_window_seconds
        return (
            tracker.get_rate_limit_count(SERVICE, window),
            tracker.get_request_count(SERVICE, window),
        )

    def state(self) -> str:
        return self.repository.get_or_create(SERVICE).state


@pytest.fixture(autouse=True)
def live_tracker():
    """A real, empty process tracker for each test.

    The cascade verdict is a ratio over this object, so a shared one would let a
    neighbouring test's counts decide this test's trip.
    """
    reset_rate_limit_tracker()
    yield
    reset_rate_limit_tracker()


@pytest.fixture(autouse=True)
def coordinator():
    """Stand a spec'd mock in for the fleet-wide cooldown client."""
    stub = MagicMock(spec=RateLimitCoordinator)
    stub.wait_if_needed.return_value = RateLimitResult(waited=False)
    with patch.object(
        RateLimitCoordinator, "get_instance", autospec=True, return_value=stub
    ):
        yield stub


# =============================================================================
# Exactly-once across the chain
# =============================================================================


class TestComposedChainObservationBehavior:
    """One 429 answer produces one observation, whichever stage saw it first."""

    def test_a_storm_the_ladder_overcomes_is_counted_per_attempt(self):
        """The breaker stage sees one success; the cascade must see the storm.

        This is the whole reason the record exists. Before it, a retry ladder
        that recovered on its last attempt reported a plain success to the
        breaker, so the 429s that preceded it were invisible to the cascade.
        """
        chain = _Chain(_cascade_config(rate_limit_cascade_threshold=99))
        calls = {"n": 0}

        def throttled_then_ok():
            calls["n"] += 1
            if calls["n"] < 3:
                raise _Throttled()
            return "ok"

        result = chain.execute(throttled_then_ok)

        assert result.outcome == PolicyOutcome.SUCCESS
        assert chain.cascade_counts() == (2, 3)

    def test_the_final_outcome_is_classified_once_not_twice(self):
        """The last attempt's 429 is the object the breaker stage also sees.

        The composer re-raises by identity, so without the scope's identity mark
        the terminal 429 would be counted by the ladder and again by the breaker
        — every exhausted storm inflating the numerator by exactly one.
        """
        chain = _Chain(_cascade_config(rate_limit_cascade_threshold=99))

        def always_throttled():
            raise _Throttled()

        result = chain.execute(always_throttled)

        assert result.outcome == PolicyOutcome.FAILURE
        assert chain.cascade_counts() == (3, 3)

    def test_one_coordinator_notify_is_sent_per_observed_429(self, coordinator):
        """The retry stage claims the call, so the breaker installs no second one.

        Two notifies per 429 would advance the consecutive counter twice and
        double the cooldown ladder's climb rate for the whole fleet.
        """
        chain = _Chain(_cascade_config(rate_limit_cascade_threshold=99))

        def always_throttled():
            raise _Throttled()

        chain.execute(always_throttled)

        assert coordinator.on_rate_limited.call_count == 3

    def test_a_returned_429_travels_the_same_chain_as_a_raised_one(self):
        """A client that hands the 429 back is classified at both depths alike."""
        chain = _Chain(_cascade_config(rate_limit_cascade_threshold=99), max_attempts=1)

        result = chain.execute(_throttled_response)

        assert result.outcome == PolicyOutcome.SUCCESS
        assert chain.cascade_counts() == (1, 1)

    def test_a_breaker_only_call_is_observed_by_the_breaker_stage(self):
        """With no retry stage below it, the breaker owns both counts itself."""
        chain = _Chain(_cascade_config(rate_limit_cascade_threshold=99))
        chain.composer = PolicyComposer().add(chain.breaker)

        def always_throttled():
            raise _Throttled()

        chain.execute(always_throttled)

        assert chain.cascade_counts() == (1, 1)

    def test_a_cooldown_deferral_is_counted_by_neither_side_of_the_rate(
        self, coordinator
    ):
        """The provider was never contacted, so it is no evidence about it.

        Counted as a request it would dilute the rate; counted as a breaker
        failure it would trip the breaker on a healthy dependency using nothing
        but Baldur's own refusal to call.
        """
        coordinator.wait_if_needed.return_value = RateLimitResult(
            deferred=True, not_before=1.0
        )
        chain = _Chain(_cascade_config(rate_limit_cascade_threshold=99))
        calls = {"n": 0}

        def never_reached():
            calls["n"] += 1
            return "ok"

        result = chain.execute(never_reached)

        assert calls["n"] == 0
        assert isinstance(result.error, RateLimitDeferredError)
        assert chain.cascade_counts() == (0, 0)
        assert chain.repository.get_or_create(SERVICE).failure_count == 0


# =============================================================================
# The cascade trip's own lifecycle
# =============================================================================


class TestComposedChainCascadeLifecycleBehavior:
    """A storm trips the breaker, and the trip recovers like any automatic OPEN."""

    def test_an_outbound_storm_trips_the_breaker_on_a_retry_less_call(self):
        """The behaviour every non-Django framework previously never got.

        The cascade was advertised as the breaker's second trip trigger, but its
        only production entry point was the Django middleware — so on Flask,
        FastAPI and every ``@protected`` outbound call it was dead.
        """
        chain = _Chain(_cascade_config(), max_attempts=1)

        def always_throttled():
            raise _Throttled()

        for _ in range(3):
            chain.execute(always_throttled)

        assert chain.cascade_counts() == (3, 3)
        assert chain.state() == "open"

    def test_the_cascade_trip_is_an_automatic_row_that_recovers(self):
        """It takes the automatic trip primitive, not the operator's pin.

        Borrowing the manual pin meant a storm that passed in seconds held the
        breaker shut for the pin's full TTL and ignored recovery entirely.
        """
        chain = _Chain(_cascade_config(recovery_timeout=0), max_attempts=1)

        def always_throttled():
            raise _Throttled()

        for _ in range(3):
            chain.execute(always_throttled)
        assert chain.state() == "open"

        row = chain.repository.get_or_create(SERVICE)
        assert row.manually_controlled is False
        assert row.manual_override_expires_at is None

        # A due recovery_timeout admits the probe an operator pin would refuse.
        chain.execute(lambda: "recovered")

        assert chain.state() == "closed"

    def test_a_tripped_breaker_rejects_without_observing_anything_further(self):
        """A rejected call made no request, so it moves neither side of the rate."""
        chain = _Chain(_cascade_config(), max_attempts=1)

        def always_throttled():
            raise _Throttled()

        for _ in range(3):
            chain.execute(always_throttled)
        counts_at_trip = chain.cascade_counts()

        result = chain.execute(always_throttled)

        assert result.outcome == PolicyOutcome.REJECTED
        assert chain.cascade_counts() == counts_at_trip

    def test_a_mixed_traffic_denominator_keeps_the_rate_below_the_threshold(self):
        """The denominator is real traffic, which is what makes the rate mean anything.

        With the request write duplicated, a pure storm capped at 50% and the
        top half of the setting's range was unreachable; with it missing, any
        single 429 read as 100%. Both failures are only visible as a ratio.
        """
        chain = _Chain(_cascade_config(rate_limit_cascade_rate=90.0), max_attempts=1)

        def always_throttled():
            raise _Throttled()

        for _ in range(7):
            chain.execute(lambda: "ok")
        for _ in range(3):
            chain.execute(always_throttled)

        assert chain.cascade_counts() == (3, 10)
        assert chain.state() == "closed"

    def test_a_real_tracker_counts_one_request_per_call(self):
        """Guards the seam the ratio above depends on, in isolation."""
        chain = _Chain(_cascade_config(rate_limit_cascade_threshold=99), max_attempts=1)

        for _ in range(5):
            chain.execute(lambda: "ok")

        assert isinstance(get_rate_limit_tracker(), RateLimitTracker)
        assert chain.cascade_counts() == (0, 5)


# =============================================================================
# Which of the three OPEN triggers fires, with nothing tuned
# =============================================================================


def _breaker_only_chain(config: CircuitBreakerConfig) -> _Chain:
    """The chain with no retry stage below it: the breaker owns both counts."""
    chain = _Chain(config, max_attempts=1)
    chain.composer = PolicyComposer().add(chain.breaker)
    return chain


def _opened_triggers(chain: _Chain) -> list[str]:
    """The ``trigger`` label of every OPEN this chain emitted, in order."""
    return [
        data.get("trigger")
        for event_type, data in chain.events
        if event_type == EventType.CIRCUIT_BREAKER_OPENED
    ]


class TestShippedDefaultTriggerBandBehavior:
    """Every threshold left at its shipped value, so the band is the real one."""

    def test_an_interleaved_storm_trips_through_the_cascade(self):
        """The cascade's own band: under five in a row AND under the failure rate.

        Three triggers watch one breaker and the first to fire owns the trip, so
        "the cascade opens breakers" is only proven inside the band the other two
        leave open. A pure storm reaches ``failure_threshold`` at call five, long
        before the cascade's twenty-call minimum sample exists; anything at or
        above ``failure_rate_threshold`` reaches that at call ten. One 429 in
        every four sits below both, and reaches the cascade's floor at call 40.
        """
        chain = _breaker_only_chain(CircuitBreakerConfig(enabled=True))

        def one_in_four(n: int) -> str:
            if n % 4 == 0:
                raise _Throttled()
            return "ok"

        for n in range(1, 11):
            chain.execute(lambda n=n: one_in_four(n))

        # Negative: the failure-rate trigger evaluates from ``minimum_calls`` on.
        assert chain.state() == "closed"

        for n in range(11, 41):
            chain.execute(lambda n=n: one_in_four(n))

        assert chain.cascade_counts() == (10, 40)
        assert chain.state() == "open"
        assert _opened_triggers(chain) == ["rate_limit_cascade"]

        row = chain.repository.get_or_create(SERVICE)
        assert row.manually_controlled is False
        assert row.manual_override_expires_at is None

    def test_a_pure_storm_trips_on_the_failure_count_first(self):
        """Five consecutive 429s are five ordinary failures before they are a storm.

        The 429s are still observed on the way — the tracker holds one request
        and one rate-limit entry per call — but the trip itself is the ordinary
        automatic one, and the calls the OPEN then rejects observe nothing.
        """
        chain = _breaker_only_chain(CircuitBreakerConfig(enabled=True))

        def always_throttled():
            raise _Throttled()

        for _ in range(5):
            chain.execute(always_throttled)

        assert chain.state() == "open"
        assert _opened_triggers(chain) == ["auto"]
        assert chain.cascade_counts() == (5, 5)

        result = chain.execute(always_throttled)

        assert result.outcome == PolicyOutcome.REJECTED
        assert chain.cascade_counts() == (5, 5)
