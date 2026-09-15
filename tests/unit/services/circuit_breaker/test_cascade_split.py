"""The 429 cascade is observed per attempt and decided once.

793 D4. ``record_rate_limit_response`` used to count a 429 and decide the
cascade in one step, so a breaker stage composed over a retry stage decided
on an inner attempt — before the breaker had recorded the call — and the
evidence pair a cascade trip carried was one call short. The step is now
split: ``record_rate_limit_observation`` counts, ``evaluate_rate_limit_cascade``
decides, and the one-shot method composes the two for a caller with no
breaker stage of its own. The tracker is not cleared on a transition: its
evidence is time-bounded by the cascade window, and a storm inside that window
is still a storm after a close.

Verification techniques applied:
- Equivalence: the composed call equals observation followed by evaluation,
  on the tracker and on the verdict
- State transition: a storm that tripped once trips again after a peer's
  close — the tracker kept its counts
- Exit inventory: disabled / not detected / pin / observe-only / frozen /
  ``did_open=False`` / ``did_open=True``, each pinned by the trip primitive's
  call count and the returned verdict
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

import pytest

from baldur.adapters.memory.circuit_breaker import (
    InMemoryCircuitBreakerStateRepository,
)
from baldur.interfaces.repositories import (
    CircuitBreakerStateData,
    CircuitBreakerStateEnum,
)
from baldur.services.circuit_breaker.config import CircuitBreakerConfig
from baldur.services.circuit_breaker.service import CircuitBreakerService
from baldur.utils.time import utc_now
from tests.factories import InMemoryRateLimitTracker, dry_run_active

SERVICE = "rate-limited-api"
CASCADE_THRESHOLD = 3
_TRACKER = "baldur.services.circuit_breaker.protection.get_rate_limit_tracker"
_FREEZE_GATE = "baldur.services.circuit_breaker.service.should_allow_cb_state_change"


def _config(**overrides) -> CircuitBreakerConfig:
    base = {
        "enabled": True,
        "failure_threshold": 100,
        "minimum_calls": 1000,
        "rate_limit_cascade_threshold": CASCADE_THRESHOLD,
        "rate_limit_cascade_window_seconds": 60,
        "rate_limit_cascade_rate": 10.0,
        "rate_limit_cascade_minimum_calls": 1,
    }
    base.update(overrides)
    return CircuitBreakerConfig(**base)


@pytest.fixture
def repo() -> InMemoryCircuitBreakerStateRepository:
    return InMemoryCircuitBreakerStateRepository()


@pytest.fixture
def tracker() -> InMemoryRateLimitTracker:
    """A tracker with the denominator already written, patched into the mixin."""
    tracker = InMemoryRateLimitTracker()
    tracker._requests[SERVICE] = 10
    with patch(_TRACKER, return_value=tracker):
        yield tracker


@pytest.fixture
def service(repo, tracker) -> CircuitBreakerService:
    svc = CircuitBreakerService(config=_config(), repository=repo)
    repo.get_or_create(SERVICE)
    with (
        patch.object(svc, "_log_circuit_open_audit"),
        patch.object(svc, "_apply_burn_rate_multiplier"),
    ):
        yield svc


def _storm(tracker: InMemoryRateLimitTracker, count: int = CASCADE_THRESHOLD) -> None:
    tracker._rate_limits[SERVICE] = count


class TestCascadeSplitBehavior:
    """Observation, evaluation, and the composed one-shot."""

    # ---------------------------------------------------------- equivalence

    def test_observation_counts_without_deciding(self, service, repo, tracker):
        _storm(tracker, CASCADE_THRESHOLD - 1)

        with patch.object(repo, "trip_to_open", wraps=repo.trip_to_open) as trip:
            result = service.record_rate_limit_observation(SERVICE)

        assert result is None
        assert tracker._rate_limits[SERVICE] == CASCADE_THRESHOLD
        trip.assert_not_called()
        assert (
            repo.get_by_service_name(SERVICE).state
            == CircuitBreakerStateEnum.CLOSED.value
        )

    def test_evaluation_decides_without_counting(self, service, repo, tracker):
        _storm(tracker)

        result = service.evaluate_rate_limit_cascade(SERVICE)

        assert result is not None
        assert result.success is True
        assert tracker._rate_limits[SERVICE] == CASCADE_THRESHOLD
        assert (
            repo.get_by_service_name(SERVICE).state
            == CircuitBreakerStateEnum.OPEN.value
        )

    def test_composed_response_equals_observation_then_evaluation(self, repo, tracker):
        """Two services over the same storm: the one-shot and the split agree."""
        composed = CircuitBreakerService(config=_config(), repository=repo)
        split = CircuitBreakerService(
            config=_config(), repository=InMemoryCircuitBreakerStateRepository()
        )
        split.repository.get_or_create(SERVICE)
        repo.get_or_create(SERVICE)
        _storm(tracker, CASCADE_THRESHOLD - 1)

        with (
            patch.object(composed, "_log_circuit_open_audit"),
            patch.object(composed, "_apply_burn_rate_multiplier"),
            patch.object(split, "_log_circuit_open_audit"),
            patch.object(split, "_apply_burn_rate_multiplier"),
        ):
            composed_result = composed.record_rate_limit_response(SERVICE)
            count_after_composed = tracker._rate_limits[SERVICE]
            _storm(tracker, CASCADE_THRESHOLD - 1)
            split.record_rate_limit_observation(SERVICE)
            split_result = split.evaluate_rate_limit_cascade(SERVICE)

        assert (
            count_after_composed == tracker._rate_limits[SERVICE] == CASCADE_THRESHOLD
        )
        assert composed_result is not None
        assert split_result is not None
        assert composed_result.new_state == split_result.new_state == "open"
        assert composed_result.message == split_result.message

    # ----------------------------------------------------- state transition

    def test_storm_inside_the_window_trips_again_after_a_close(
        self, service, repo, tracker
    ):
        """The tracker is not cleared on a transition: the storm outlives the close."""
        _storm(tracker)
        first = service.evaluate_rate_limit_cascade(SERVICE)
        assert first is not None
        # A peer closed the name (hydrated here); the window's 429s remain.
        repo.hydrate_snapshot(
            CircuitBreakerStateData(
                service_name=SERVICE, state=CircuitBreakerStateEnum.CLOSED.value
            )
        )

        second = service.evaluate_rate_limit_cascade(SERVICE)

        assert second is not None
        assert second.success is True
        assert (
            repo.get_by_service_name(SERVICE).state
            == CircuitBreakerStateEnum.OPEN.value
        )
        assert tracker._rate_limits[SERVICE] == CASCADE_THRESHOLD

    # ------------------------------------------------------- exit inventory

    def test_disabled_breaker_neither_reads_the_tracker_nor_trips(self, repo, tracker):
        service = CircuitBreakerService(config=_config(enabled=False), repository=repo)
        _storm(tracker)

        with patch.object(repo, "trip_to_open", wraps=repo.trip_to_open) as trip:
            assert service.record_rate_limit_observation(SERVICE) is None
            assert service.evaluate_rate_limit_cascade(SERVICE) is None
            assert service.record_rate_limit_response(SERVICE) is None

        trip.assert_not_called()
        # The observation half never wrote either: the seeded count stands.
        assert tracker._rate_limits[SERVICE] == CASCADE_THRESHOLD

    def test_storm_below_the_threshold_is_not_a_cascade(self, service, repo, tracker):
        _storm(tracker, CASCADE_THRESHOLD - 1)

        with patch.object(repo, "trip_to_open", wraps=repo.trip_to_open) as trip:
            result = service.evaluate_rate_limit_cascade(SERVICE)

        assert result is None
        trip.assert_not_called()

    def test_pin_active_row_blocks_the_trip(self, service, repo, tracker):
        repo.set_manual_control(
            SERVICE,
            CircuitBreakerStateEnum.CLOSED.value,
            reason="operator allow",
            expires_at=utc_now() + timedelta(minutes=10),
        )
        _storm(tracker)

        with patch.object(repo, "trip_to_open", wraps=repo.trip_to_open) as trip:
            result = service.evaluate_rate_limit_cascade(SERVICE)

        assert result is None
        trip.assert_not_called()
        assert (
            repo.get_by_service_name(SERVICE).state
            == CircuitBreakerStateEnum.CLOSED.value
        )

    def test_observe_only_mode_suppresses_the_trip(self, service, repo, tracker):
        _storm(tracker)

        with (
            dry_run_active(),
            patch.object(repo, "trip_to_open", wraps=repo.trip_to_open) as trip,
        ):
            result = service.evaluate_rate_limit_cascade(SERVICE)

        assert result is None
        trip.assert_not_called()
        assert (
            repo.get_by_service_name(SERVICE).state
            == CircuitBreakerStateEnum.CLOSED.value
        )

    def test_freeze_withholds_the_trip(self, service, repo, tracker):
        _storm(tracker)

        with (
            patch(_FREEZE_GATE, return_value=False),
            patch.object(repo, "trip_to_open", wraps=repo.trip_to_open) as trip,
        ):
            result = service.evaluate_rate_limit_cascade(SERVICE)

        assert result is None
        trip.assert_not_called()
        assert (
            repo.get_by_service_name(SERVICE).state
            == CircuitBreakerStateEnum.CLOSED.value
        )

    def test_already_open_row_is_a_race_loser_that_owes_nothing(
        self, service, repo, tracker
    ):
        """``did_open=False``: the 429 stays recorded, no verdict, no backoff step."""
        repo.hydrate_snapshot(
            CircuitBreakerStateData(
                service_name=SERVICE,
                state=CircuitBreakerStateEnum.OPEN.value,
                opened_at=utc_now(),
            )
        )
        _storm(tracker)

        with patch.object(repo, "trip_to_open", wraps=repo.trip_to_open) as trip:
            result = service.evaluate_rate_limit_cascade(SERVICE)

        assert result is None
        trip.assert_called_once()
        assert tracker.get_backoff_level(SERVICE) == 0

    def test_cascade_trip_returns_the_verdict_and_steps_the_backoff(
        self, service, repo, tracker
    ):
        """``did_open=True``: the one exit that returns a result."""
        _storm(tracker)

        with patch.object(repo, "trip_to_open", wraps=repo.trip_to_open) as trip:
            result = service.evaluate_rate_limit_cascade(SERVICE)

        trip.assert_called_once_with(SERVICE, 0)
        assert result is not None
        assert result.success is True
        assert (result.previous_state, result.new_state) == ("closed", "open")
        assert tracker.get_backoff_level(SERVICE) == 1
