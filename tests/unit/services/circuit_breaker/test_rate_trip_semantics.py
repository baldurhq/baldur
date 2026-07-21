"""Unit tests for the service-level failure-rate trip semantics (719).

The circuit breaker's rate trigger now decides on real evidence: CLOSED-state
successes enter the denominator through the service's outcome window, so
``failure_rate_threshold`` discriminates between a service failing 30% of its
calls and one failing 60%. These tests drive ``CircuitBreakerService`` end to
end over a repository rather than the predicate in isolation.

Verification techniques applied:
- Equivalence partitioning: below-threshold vs at-or-above-threshold traffic
- Boundary analysis: the count trigger fires at exactly ``failure_threshold``
- Dependency interaction: the 490 D4 fast path performs zero repository calls
- State transition: the window clear sites (auto trip, recovery close, the
  three manual-control paths)
- Negative assertion: the aggregate failure rate is a rate, not a near-binary
  signal
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from baldur.adapters.memory.circuit_breaker import (
    InMemoryCircuitBreakerStateRepository,
)
from baldur.interfaces.repositories import CircuitBreakerStateData
from baldur.services.circuit_breaker.config import CircuitBreakerConfig
from baldur.services.circuit_breaker.service import CircuitBreakerService

SERVICE = "payment-gateway"


class _CountingRepository:
    """Repository proxy that records every method invoked on it.

    Wraps a real repository so behavior is unchanged; the ``calls`` list is the
    assertion surface for the hot-path budget.
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.calls: list[str] = []

    def __getattr__(self, name: str) -> Any:
        attribute = getattr(self._inner, name)
        if callable(attribute):

            def _recorded(*args: Any, **kwargs: Any) -> Any:
                self.calls.append(name)
                return attribute(*args, **kwargs)

            return _recorded

        self.calls.append(name)
        return attribute


class _CumulativeCounterRepository(InMemoryCircuitBreakerStateRepository):
    """Stand-in for the Redis/SQL backends' counter semantics.

    ``fakeredis`` is not a project dependency, so the second backend leg uses
    the contract the Redis and SQL repositories share instead: plain cumulative
    counters where a reset written by ``update_state`` sticks. Post-de-windowing
    the in-memory repository already behaves this way; subclassing states the
    intent of the parity leg explicitly rather than asserting the same object
    twice.
    """


def _config(**overrides) -> CircuitBreakerConfig:
    base = {
        "enabled": True,
        "failure_threshold": 5,
        "failure_rate_threshold": 50.0,
        "sliding_window_size": 100,
        "minimum_calls": 10,
    }
    base.update(overrides)
    return CircuitBreakerConfig(**base)


def _service(config: CircuitBreakerConfig, repository: Any = None):
    """Build a service over a repository, with trip side effects stubbed out.

    The audit write and the burn-rate multiplier reach outside the circuit
    breaker; the trip decision itself is what these tests assert on.
    """
    return CircuitBreakerService(
        config=config,
        repository=repository or InMemoryCircuitBreakerStateRepository(),
    )


def _drive(service: CircuitBreakerService, pattern: str) -> None:
    """Replay a call sequence: ``f`` records a failure, ``s`` a success."""
    with (
        patch.object(service, "_log_circuit_open_audit"),
        patch.object(service, "_apply_burn_rate_multiplier"),
    ):
        for outcome in pattern:
            if outcome == "f":
                service.record_failure(SERVICE)
            else:
                service.record_success(SERVICE)


def _interleave(total: int, failures: int) -> str:
    """Spread ``failures`` failures evenly across ``total`` calls.

    Even spreading matters: a run of consecutive failures would trip the count
    trigger and mask whichever way the rate trigger decided.
    """
    pattern = []
    emitted = 0
    for index in range(total):
        # Emit a failure when the running quota says one is due.
        due = (index + 1) * failures // total
        if due > emitted:
            pattern.append("f")
            emitted = due
        else:
            pattern.append("s")
    return "".join(pattern)


# =============================================================================
# Behavior — rate discrimination (719 SC #1)
# =============================================================================


class TestCircuitBreakerRateTripBehavior:
    """A real denominator makes ``failure_rate_threshold`` discriminate.

    Before the outcome window the denominator held failures only, so the
    observed rate was ~100% whenever any failure existed and both sequences
    below would have been treated identically.
    """

    @pytest.mark.parametrize(
        "repository_factory",
        [InMemoryCircuitBreakerStateRepository, _CumulativeCounterRepository],
        ids=["memory_backend", "cumulative_counter_backend"],
    )
    def test_thirty_percent_failures_do_not_trip_a_fifty_percent_threshold(
        self, repository_factory
    ):
        """Mixed traffic below the threshold stays closed.

        The count trigger is held out of reach so only the rate gate decides —
        with failures spread evenly, no run of 5 consecutive failures occurs.
        """
        service = _service(
            _config(failure_threshold=1000, failure_rate_threshold=50.0),
            repository_factory(),
        )

        _drive(service, _interleave(total=100, failures=30))

        assert service.get_state(SERVICE) == "closed"
        failures, total = service.get_window_evidence(SERVICE)
        assert (failures, total) == (30, 100)

    @pytest.mark.parametrize(
        "repository_factory",
        [InMemoryCircuitBreakerStateRepository, _CumulativeCounterRepository],
        ids=["memory_backend", "cumulative_counter_backend"],
    )
    def test_sixty_percent_failures_trip_a_fifty_percent_threshold(
        self, repository_factory
    ):
        """Mixed traffic above the threshold opens the circuit."""
        service = _service(
            _config(failure_threshold=1000, failure_rate_threshold=50.0),
            repository_factory(),
        )

        _drive(service, _interleave(total=100, failures=60))

        assert service.get_state(SERVICE) == "open"

    def test_rate_trip_survives_interleaved_successes(self):
        """A success no longer buys the dependency an indefinite reprieve.

        On the cumulative-counter backends any success resets the consecutive
        counter, so before the window a 60%-failing service could never reach
        the count trigger either — it was unprotected by both triggers.
        """
        service = _service(_config(failure_threshold=1000, minimum_calls=10))

        _drive(service, "fsffsffsffsffsffsf")

        assert service.get_state(SERVICE) == "open"

    def test_healthy_traffic_never_trips(self):
        """A service with no failures stays closed across a full window."""
        service = _service(_config())

        _drive(service, "s" * 200)

        assert service.get_state(SERVICE) == "closed"
        assert service.get_window_evidence(SERVICE) == (0, 100)

    def test_rate_trigger_disabled_leaves_only_the_count_trigger(self):
        """``failure_rate_threshold=0`` keeps a 60%-failing service closed."""
        service = _service(
            _config(failure_threshold=1000, failure_rate_threshold=0.0),
        )

        _drive(service, _interleave(total=100, failures=60))

        assert service.get_state(SERVICE) == "closed"

    def test_rate_trip_below_minimum_calls_is_withheld(self):
        """Too little traffic makes the rate estimate noise — no trip."""
        service = _service(
            _config(failure_threshold=1000, minimum_calls=10),
        )

        # 4 of 8 calls fail: 50% concentration, but only 8 observations.
        _drive(service, "fsfsfsfs")

        assert service.get_state(SERVICE) == "closed"
        assert service.get_window_evidence(SERVICE) == (4, 8)


# =============================================================================
# Behavior — count trigger at exactly failure_threshold (719 D4)
# =============================================================================


class TestCircuitBreakerCountTripBehavior:
    """``minimum_calls`` no longer gates the consecutive-failure trigger."""

    def test_circuit_opens_at_exactly_failure_threshold(self):
        """Five consecutive failures open a breaker configured for five."""
        service = _service(_config(failure_threshold=5, minimum_calls=10))

        _drive(service, "ffff")
        assert service.get_state(SERVICE) == "closed"

        _drive(service, "f")
        assert service.get_state(SERVICE) == "open"

    def test_trip_is_not_withheld_until_minimum_calls(self):
        """Negative assertion: the old 10-failure floor no longer applies.

        ``minimum_calls=10`` previously gated both triggers, so an operator
        setting ``failure_threshold=5`` silently got 10.
        """
        service = _service(_config(failure_threshold=5, minimum_calls=10))

        _drive(service, "fffff")

        assert service.get_state(SERVICE) == "open"
        state = service.get_or_create_state(SERVICE)
        assert state.failure_count == 5

    def test_a_success_resets_the_consecutive_count(self):
        """The count trigger is consecutive: an interleaved success restarts it."""
        service = _service(
            _config(failure_threshold=5, failure_rate_threshold=0.0),
        )

        _drive(service, "ffffsffff")

        assert service.get_state(SERVICE) == "closed"


# =============================================================================
# Behavior — the 490 D4 hot-path budget stays at zero repository calls
# =============================================================================


class TestRecordSuccessHotPathBudget:
    """A steady-state CLOSED success must not reach the repository.

    The naive fix for the denominator was a repository write per success, which
    would have put a Redis round trip back on every protected call. The window
    append is memory-local; this test is what stops that regressing.
    """

    @staticmethod
    def _clean_hint() -> CircuitBreakerStateData:
        return CircuitBreakerStateData(
            service_name=SERVICE,
            state="closed",
            failure_count=0,
            success_count=0,
            manually_controlled=False,
        )

    def test_fast_path_success_performs_zero_repository_calls(self):
        """With a clean CLOSED hint, no repository method is invoked."""
        repository = _CountingRepository(InMemoryCircuitBreakerStateRepository())
        service = _service(_config(), repository)

        service.record_success(SERVICE, hint_state=self._clean_hint())

        assert repository.calls == []

    def test_fast_path_success_still_enters_the_rate_denominator(self):
        """The skipped repository write does not skip the evidence.

        Without this, the fast path would silently reintroduce the gap: the
        busiest, healthiest services would contribute nothing to the window.
        """
        repository = _CountingRepository(InMemoryCircuitBreakerStateRepository())
        service = _service(_config(), repository)

        for _ in range(20):
            service.record_success(SERVICE, hint_state=self._clean_hint())

        assert service.get_window_evidence(SERVICE) == (0, 20)
        assert repository.calls == []

    def test_slow_path_success_does_reach_the_repository(self):
        """Control: without a clean hint the reset write still happens.

        Pins that the zero-call assertion above measures the fast path rather
        than a proxy that never records anything.
        """
        repository = _CountingRepository(InMemoryCircuitBreakerStateRepository())
        service = _service(_config(), repository)

        service.record_success(SERVICE)

        assert repository.calls != []


# =============================================================================
# Behavior — window lifecycle clear sites (719 D9)
# =============================================================================


class TestOutcomeWindowLifecycleBehavior:
    """Every observed state transition starts a fresh CLOSED period.

    Outcomes recorded before a trip or a recovery say nothing about the rate
    after it; carrying them over would let stale evidence re-trip a breaker the
    operator just closed.
    """

    @staticmethod
    def _manual(service: CircuitBreakerService):
        """Run a manual-control call with the system-enabled gate satisfied."""
        return patch(
            "baldur.services.circuit_breaker.manual_control._is_system_enabled",
            return_value=True,
        )

    def test_auto_trip_clears_the_window(self):
        """The CLOSED -> OPEN trip drops the evidence it decided on."""
        service = _service(_config(failure_threshold=5))

        _drive(service, "fffff")

        assert service.get_state(SERVICE) == "open"
        assert service.get_window_evidence(SERVICE) == (0, 0)

    def test_half_open_recovery_close_clears_the_window(self):
        """A recovery back to CLOSED starts without pre-trip evidence."""
        service = _service(_config(failure_threshold=5, success_threshold=2))
        _drive(service, "ffsss")  # accumulate evidence without tripping
        assert service.get_window_evidence(SERVICE)[1] == 5

        # Move to HALF_OPEN, then satisfy the close threshold.
        service.repository.update_state(service_name=SERVICE, state="half_open")
        _drive(service, "ss")

        assert service.get_state(SERVICE) == "closed"
        assert service.get_window_evidence(SERVICE) == (0, 0)

    def test_force_open_clears_the_window(self):
        """A manual open discards the evidence for the closed period it ends."""
        service = _service(_config())
        _drive(service, "fsss")
        assert service.get_window_evidence(SERVICE) == (1, 4)

        with self._manual(service):
            result = service.force_open(SERVICE, reason="maintenance")

        assert result.success is True
        assert service.get_window_evidence(SERVICE) == (0, 0)

    def test_force_close_clears_the_window(self):
        """A manual close starts the new closed period without evidence."""
        service = _service(_config())
        _drive(service, "fsss")

        with self._manual(service):
            result = service.force_close(SERVICE, reason="recovered")

        assert result.success is True
        assert service.get_window_evidence(SERVICE) == (0, 0)

    def test_reset_clears_the_window(self):
        """``reset`` clears the rate evidence alongside the repository counters."""
        service = _service(_config())
        _drive(service, "ffss")

        with self._manual(service):
            result = service.reset(SERVICE, reason="operator reset")

        assert result.success is True
        assert service.get_window_evidence(SERVICE) == (0, 0)

    def test_clear_is_scoped_to_the_transitioning_service(self):
        """A trip on one service leaves every peer's evidence intact."""
        service = _service(_config(failure_threshold=5))
        _drive(service, "fffff")
        with (
            patch.object(service, "_log_circuit_open_audit"),
            patch.object(service, "_apply_burn_rate_multiplier"),
        ):
            for _ in range(4):
                service.record_success("peer-service")

        assert service.get_window_evidence(SERVICE) == (0, 0)
        assert service.get_window_evidence("peer-service") == (0, 4)


# =============================================================================
# Behavior — aggregate failure rate is a rate again (719 D6)
# =============================================================================


class TestAggregateFailureRateBehavior:
    """``get_aggregate_failure_rate`` reads the outcome windows.

    Sourced from repository counters it was near-binary — ~1.0 whenever any
    circuit held failures — which could make the capacity-reservation safety
    valve assert CRITICAL and block emergency recovery during any transient
    burst.
    """

    def test_mixed_traffic_yields_the_observed_fraction(self):
        """Negative assertion: 1 failure in 10 calls reads 0.1, not ~1.0."""
        service = _service(_config(failure_threshold=1000))

        _drive(service, "f" + "s" * 9)

        assert service.get_aggregate_failure_rate() == pytest.approx(0.1)

    def test_no_recorded_calls_yields_zero(self):
        """No evidence is reported as 0.0, the documented empty case."""
        service = _service(_config())

        assert service.get_aggregate_failure_rate() == 0.0

    def test_rate_is_the_mean_across_every_tracked_service(self):
        """One failing service among healthy ones is diluted, not dominant."""
        service = _service(_config(failure_threshold=1000))

        _drive(service, "f" * 5 + "s" * 5)
        with (
            patch.object(service, "_log_circuit_open_audit"),
            patch.object(service, "_apply_burn_rate_multiplier"),
        ):
            for _ in range(10):
                service.record_success("healthy-service")

        # 5 failures over 20 observed calls.
        assert service.get_aggregate_failure_rate() == pytest.approx(0.25)

    def test_all_failing_traffic_yields_one(self):
        """The upper bound is still reachable when every call fails."""
        service = _service(_config(failure_threshold=1000, failure_rate_threshold=0.0))

        _drive(service, "f" * 10)

        assert service.get_aggregate_failure_rate() == pytest.approx(1.0)

    def test_get_window_evidence_reports_the_pair_the_trip_decided_on(self):
        """The single read method exposes exactly the failures/total pair."""
        service = _service(_config(failure_threshold=1000))

        _drive(service, "ffsssss")

        assert service.get_window_evidence(SERVICE) == (2, 7)
