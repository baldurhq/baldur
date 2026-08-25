"""Trip side effects gate on ``did_open``; the outcome window clears anyway.

A CLOSED->OPEN trip now writes through an atomic primitive that names one
winner per transition, so the side effects that describe a *cluster* event —
the ``CIRCUIT_BREAKER_OPENED`` emission, the audit record, the state-change
metric, and the shared error-budget burn — belong to that winner alone. They
used to run on every tripping worker, which charged one cluster-wide budget
N times for one logical trip.

Two things deliberately do not follow that gate:

- the outcome window clear, because the evidence was consumed by *this*
  worker's own decision regardless of who won the write;
- the suppressed-trip log line, because an operator's override swallowing a
  real failure burst has to be visible somewhere.
"""

from __future__ import annotations

import threading
from datetime import timedelta
from typing import Any
from unittest.mock import MagicMock, Mock, patch

from structlog.testing import capture_logs

from baldur.adapters.memory.circuit_breaker import (
    InMemoryCircuitBreakerStateRepository,
)
from baldur.interfaces.repositories import (
    CircuitBreakerOpenAttempt,
    CircuitBreakerStateData,
    CircuitBreakerStateEnum,
    CircuitBreakerStateRepository,
    pinned_trip_attempt,
)
from baldur.services.circuit_breaker.config import CircuitBreakerConfig
from baldur.services.circuit_breaker.service import CircuitBreakerService
from baldur.utils.time import utc_now

SERVICE = "payment-gateway"
FAILURE_THRESHOLD = 5


def _config(**overrides: Any) -> CircuitBreakerConfig:
    base: dict[str, Any] = {
        "enabled": True,
        "failure_threshold": FAILURE_THRESHOLD,
        "success_threshold": 2,
        "minimum_calls": 1,
        "sliding_window_size": 100,
    }
    base.update(overrides)
    return CircuitBreakerConfig(**base)


def _attempt(state: str, *, did_open: bool):
    return CircuitBreakerOpenAttempt(
        state=CircuitBreakerStateData(
            service_name=SERVICE,
            state=state,
            opened_at=utc_now() if state == "open" else None,
        ),
        did_open=did_open,
    )


def _service_over_stubbed_repo(trip_attempt: CircuitBreakerOpenAttempt):
    """Service whose repository answers the trip with a fixed verdict.

    ``record_failure`` returns a CLOSED row at the threshold so the decision
    path always reaches the trip; only the primitive's verdict varies.
    """
    closed_at_threshold = CircuitBreakerStateData(
        service_name=SERVICE,
        state=CircuitBreakerStateEnum.CLOSED.value,
        failure_count=FAILURE_THRESHOLD,
    )
    repo = Mock(spec=CircuitBreakerStateRepository)
    repo.get_or_create.return_value = closed_at_threshold
    repo.record_failure.return_value = closed_at_threshold
    repo.update_state.return_value = True
    repo.trip_to_open.return_value = trip_attempt

    service = CircuitBreakerService(config=_config(), repository=repo)
    service._emit_event = MagicMock(spec=service._emit_event)
    return service, repo


def _opened_emits(service) -> list:
    from baldur.services.event_bus import EventType

    return [
        call
        for call in service._emit_event.call_args_list
        if call[0][0] == EventType.CIRCUIT_BREAKER_OPENED
    ]


# =============================================================================
# Behavior — did_open gates the cluster-logical side effects
# =============================================================================


class TestTripSideEffectGatingBehavior:
    """Only the worker that performed the transition reports it."""

    def test_winner_runs_every_cluster_logical_side_effect(self):
        service, repo = _service_over_stubbed_repo(_attempt("open", did_open=True))

        with (
            patch.object(service, "_log_circuit_open_audit") as mock_audit,
            patch.object(service, "_apply_burn_rate_multiplier") as mock_burn,
        ):
            service.record_failure(SERVICE)

        repo.trip_to_open.assert_called_once_with(SERVICE, FAILURE_THRESHOLD)
        assert len(_opened_emits(service)) == 1
        mock_audit.assert_called_once()
        mock_burn.assert_called_once()

    def test_race_loser_runs_none_of_them(self):
        # The store says a peer already opened this circuit. Emitting here
        # would double-count the transition and, for the shared error budget,
        # charge it twice for one logical trip.
        service, _repo = _service_over_stubbed_repo(_attempt("open", did_open=False))

        with (
            patch.object(service, "_log_circuit_open_audit") as mock_audit,
            patch.object(service, "_apply_burn_rate_multiplier") as mock_burn,
        ):
            service.record_failure(SERVICE)

        assert _opened_emits(service) == []
        mock_audit.assert_not_called()
        mock_burn.assert_not_called()

    def test_half_open_verdict_runs_none_of_them(self):
        # The cluster already progressed to recovery testing; this worker's
        # trip decided nothing.
        service, _repo = _service_over_stubbed_repo(
            _attempt("half_open", did_open=False)
        )

        with (
            patch.object(service, "_log_circuit_open_audit"),
            patch.object(service, "_apply_burn_rate_multiplier"),
        ):
            service.record_failure(SERVICE)

        assert _opened_emits(service) == []

    def test_winner_emits_the_window_denominators_the_evaluator_replays(self):
        # The config-shadow evaluator replays the trip decision from these
        # fields, and the journal subscriber stores event data verbatim.
        service, _repo = _service_over_stubbed_repo(_attempt("open", did_open=True))

        with (
            patch.object(service, "_log_circuit_open_audit"),
            patch.object(service, "_apply_burn_rate_multiplier"),
        ):
            service.record_failure(SERVICE)

        data = _opened_emits(service)[0][1]["data"]
        assert data["service_name"] == SERVICE
        assert data["previous_state"] == "closed"
        assert data["trigger"] == "auto"
        assert data["consecutive_failure_count"] == FAILURE_THRESHOLD
        assert "window_failure_count" in data
        assert "window_total_calls" in data

    def test_outcome_window_clears_even_for_a_race_loser(self):
        # Unconditional by design: the window evidence was consumed by this
        # worker's own decision, whoever won the cluster write. Leaving it
        # would let the same calls re-decide the next trip.
        service, _repo = _service_over_stubbed_repo(_attempt("open", did_open=False))

        with (
            patch.object(service, "_log_circuit_open_audit"),
            patch.object(service, "_apply_burn_rate_multiplier"),
        ):
            service.record_failure(SERVICE)

        assert service.get_window_evidence(SERVICE) == (0, 0)


# =============================================================================
# Behavior — the declined-by-pin verdict
# =============================================================================


class TestTripBlockedByPinBehavior:
    """A suppressed trip is gated, but not silent."""

    def test_pinned_verdict_logs_a_warning_with_the_override_expiry(self):
        expires_at = utc_now() + timedelta(minutes=10)
        service, _repo = _service_over_stubbed_repo(
            pinned_trip_attempt(SERVICE, expires_at)
        )

        with (
            patch.object(service, "_log_circuit_open_audit"),
            patch.object(service, "_apply_burn_rate_multiplier"),
            capture_logs() as caplog,
        ):
            service.record_failure(SERVICE)

        # The local-pin skip's DEBUG line only covers calls made after the
        # pinned row reached this worker, so without this line the first burst
        # under a peer's override is invisible.
        blocked = [
            entry
            for entry in caplog
            if entry.get("event") == "circuit_breaker.trip_blocked"
        ]
        assert len(blocked) == 1
        assert blocked[0]["log_level"] == "warning"
        assert blocked[0]["service_name"] == SERVICE
        assert blocked[0]["manual_override_expires_at"] == expires_at.isoformat()

    def test_open_ended_override_reports_no_expiry(self):
        service, _repo = _service_over_stubbed_repo(pinned_trip_attempt(SERVICE, None))

        with (
            patch.object(service, "_log_circuit_open_audit"),
            patch.object(service, "_apply_burn_rate_multiplier"),
            capture_logs() as caplog,
        ):
            service.record_failure(SERVICE)

        blocked = next(
            entry
            for entry in caplog
            if entry.get("event") == "circuit_breaker.trip_blocked"
        )
        assert blocked["manual_override_expires_at"] is None

    def test_pinned_verdict_runs_no_cluster_logical_side_effect(self):
        service, _repo = _service_over_stubbed_repo(
            pinned_trip_attempt(SERVICE, utc_now() + timedelta(minutes=10))
        )

        with (
            patch.object(service, "_log_circuit_open_audit") as mock_audit,
            patch.object(service, "_apply_burn_rate_multiplier") as mock_burn,
        ):
            service.record_failure(SERVICE)

        assert _opened_emits(service) == []
        mock_audit.assert_not_called()
        mock_burn.assert_not_called()

    def test_pinned_verdict_still_clears_the_outcome_window(self):
        service, _repo = _service_over_stubbed_repo(pinned_trip_attempt(SERVICE, None))

        with (
            patch.object(service, "_log_circuit_open_audit"),
            patch.object(service, "_apply_burn_rate_multiplier"),
        ):
            service.record_failure(SERVICE)

        assert service.get_window_evidence(SERVICE) == (0, 0)


# =============================================================================
# Behavior — single-fire under a real concurrent trip
# =============================================================================


class TestConcurrentTripSingleFireBehavior:
    """Two threads past the threshold produce one emission, two OPEN rows."""

    def test_concurrent_record_failure_emits_exactly_one_opened_event(self):
        # Given: a service over the InMemory repository, whose trip primitive
        # is the real single-lock override.
        repo = InMemoryCircuitBreakerStateRepository()
        service = CircuitBreakerService(config=_config(), repository=repo)
        emitted: list[Any] = []
        emit_lock = threading.Lock()

        def _capture(event_type, data=None, **kwargs):
            from baldur.services.event_bus import EventType

            if event_type == EventType.CIRCUIT_BREAKER_OPENED:
                with emit_lock:
                    emitted.append(data)

        service._emit_event = _capture

        # Prime the row to one failure short of the threshold so both threads
        # cross it in the same instant.
        repo.get_or_create(SERVICE)
        for _ in range(FAILURE_THRESHOLD - 1):
            repo.record_failure(SERVICE)

        thread_count = 8
        barrier = threading.Barrier(thread_count)

        def _fail() -> None:
            barrier.wait()
            service.record_failure(SERVICE)

        with (
            patch.object(service, "_log_circuit_open_audit"),
            patch.object(service, "_apply_burn_rate_multiplier"),
        ):
            threads = [threading.Thread(target=_fail) for _ in range(thread_count)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)

        # Exactly one worker reports the transition...
        assert len(emitted) == 1
        # ...and the row every thread now reads is OPEN, so none readmits.
        assert repo.get_by_service_name(SERVICE).state == (
            CircuitBreakerStateEnum.OPEN.value
        )
        assert repo.get_by_service_name(SERVICE).opened_at is not None


# =============================================================================
# Behavior — the primitive is what the trip calls
# =============================================================================


class TestTripCallSiteBehavior:
    """The state write moved to the primitive; the decision did not."""

    def test_trip_uses_the_atomic_primitive_not_a_plain_update_state(self):
        # A plain update_state here is precisely the fire-and-forget write
        # whose mirror could erase the trip from the shared store.
        service, repo = _service_over_stubbed_repo(_attempt("open", did_open=True))

        with (
            patch.object(service, "_log_circuit_open_audit"),
            patch.object(service, "_apply_burn_rate_multiplier"),
        ):
            service.record_failure(SERVICE)

        repo.trip_to_open.assert_called_once_with(SERVICE, FAILURE_THRESHOLD)
        assert not any(
            call.kwargs.get("state") == "open"
            for call in repo.update_state.call_args_list
        )

    def test_no_trip_is_attempted_below_the_threshold(self):
        closed_below = CircuitBreakerStateData(
            service_name=SERVICE,
            state=CircuitBreakerStateEnum.CLOSED.value,
            failure_count=1,
        )
        repo = Mock(spec=CircuitBreakerStateRepository)
        repo.get_or_create.return_value = closed_below
        repo.record_failure.return_value = closed_below
        service = CircuitBreakerService(
            config=_config(minimum_calls=100), repository=repo
        )
        service._emit_event = MagicMock(spec=service._emit_event)

        service.record_failure(SERVICE)

        repo.trip_to_open.assert_not_called()
