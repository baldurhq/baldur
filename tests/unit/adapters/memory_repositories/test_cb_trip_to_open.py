"""InMemory ``trip_to_open`` and the two new ``update_state`` directives.

The InMemory adapter is both a store in its own right and the L1 half of the
layered repository, so its single-lock override is what makes ``did_open`` a
single-fire gate inside one process — the property the service's event,
audit, metric and shared-error-budget side effects are gated on.

Also covers the directives the record-path mirror needs from ``update_state``:

- ``clear_opened_at``, because ``opened_at=None`` means "keep" at the storage
  boundary, so a row moving to CLOSED would otherwise go on reporting the
  instant it opened;
- ``skip_if_pinned``, the store-side half of pin neutrality, evaluated inside
  the same lock hold that would perform the write.
"""

from __future__ import annotations

import threading
from datetime import timedelta

import pytest

from baldur.adapters.memory.circuit_breaker import (
    InMemoryCircuitBreakerStateRepository,
)
from baldur.interfaces.repositories import (
    CIRCUIT_BREAKER_PINNED_TOKEN,
    CircuitBreakerStateEnum,
)
from baldur.utils.time import utc_now

SVC = "payment.charge"
FAILURE_COUNT = 5


@pytest.fixture
def repo() -> InMemoryCircuitBreakerStateRepository:
    return InMemoryCircuitBreakerStateRepository()


def _seed(repo, state: str) -> None:
    repo.get_or_create(SVC)
    repo.update_state(service_name=SVC, state=state, opened_at=utc_now())


# =============================================================================
# Behavior — trip branch matrix under one lock acquire
# =============================================================================


class TestInMemoryTripToOpenBehavior:
    """Branch contract + single-winner atomicity (see the ABC's contract)."""

    def test_closed_row_transitions_to_open_with_the_callers_failure_count(self, repo):
        # Given: a CLOSED row with recorded failures and a stale success count.
        repo.get_or_create(SVC)
        repo.record_success(SVC)
        for _ in range(FAILURE_COUNT):
            repo.record_failure(SVC)

        attempt = repo.trip_to_open(SVC, FAILURE_COUNT)

        # Then: the whole row is rewritten — the caller's count verbatim, so
        # the durable row and the local one describe the same trip.
        assert attempt.did_open is True
        assert attempt.state.state == CircuitBreakerStateEnum.OPEN.value
        assert attempt.state.failure_count == FAILURE_COUNT
        assert attempt.state.success_count == 0
        assert attempt.state.opened_at is not None

    def test_trip_is_visible_to_an_immediate_read_with_opened_at_set(self, repo):
        # The guarantee the layered router's atomic path inherits: no settle
        # poll, no drain — the write is done when the call returns.
        repo.get_or_create(SVC)

        attempt = repo.trip_to_open(SVC, FAILURE_COUNT)

        stored = repo.get_by_service_name(SVC)
        assert stored.state == CircuitBreakerStateEnum.OPEN.value
        assert stored.opened_at == attempt.state.opened_at
        assert stored.opened_at is not None

    def test_trip_clears_the_half_open_counter_and_watermark(self, repo):
        # Given: a row carrying half-open trial residue.
        repo.get_or_create(SVC)
        repo.update_state(
            service_name=SVC,
            state=CircuitBreakerStateEnum.CLOSED.value,
            half_open_request_count=3,
        )

        attempt = repo.trip_to_open(SVC, FAILURE_COUNT)

        assert attempt.state.half_open_request_count == 0
        assert attempt.state.half_open_window_started_at is None

    def test_missing_row_is_created_and_tripped(self, repo):
        # No get_or_create beforehand: absent is the CLOSED default.
        attempt = repo.trip_to_open(SVC, FAILURE_COUNT)

        assert attempt.did_open is True
        assert attempt.state.state == CircuitBreakerStateEnum.OPEN.value
        assert repo.get_by_service_name(SVC) is not None

    def test_open_row_is_a_race_loser_that_keeps_its_opened_at(self, repo):
        # Given: an already-OPEN row with an older opened_at.
        repo.get_or_create(SVC)
        opened = utc_now() - timedelta(seconds=30)
        repo.update_state(
            service_name=SVC,
            state=CircuitBreakerStateEnum.OPEN.value,
            opened_at=opened,
        )

        attempt = repo.trip_to_open(SVC, FAILURE_COUNT)

        assert attempt.did_open is False
        assert attempt.state.state == CircuitBreakerStateEnum.OPEN.value
        assert attempt.state.opened_at == opened

    def test_second_trip_does_not_restamp_the_first_trips_opened_at(self, repo):
        # Idempotency: the transition happened once, so the row keeps saying
        # when. A restamp would move the recovery clock every failure burst.
        repo.get_or_create(SVC)
        first = repo.trip_to_open(SVC, FAILURE_COUNT)

        second = repo.trip_to_open(SVC, FAILURE_COUNT + 3)

        assert second.did_open is False
        assert second.state.opened_at == first.state.opened_at
        assert repo.get_by_service_name(SVC).failure_count == FAILURE_COUNT

    def test_half_open_row_is_not_clobbered_back_to_open(self, repo):
        _seed(repo, CircuitBreakerStateEnum.HALF_OPEN.value)

        attempt = repo.trip_to_open(SVC, FAILURE_COUNT)

        assert attempt.did_open is False
        assert attempt.state.state == CircuitBreakerStateEnum.HALF_OPEN.value
        assert (
            repo.get_by_service_name(SVC).state
            == CircuitBreakerStateEnum.HALF_OPEN.value
        )

    def test_active_pin_declines_the_write_and_leaves_the_row_untouched(self, repo):
        # Given: an operator's Allow on a CLOSED row.
        expires_at = utc_now() + timedelta(minutes=10)
        repo.set_manual_control(
            SVC,
            CircuitBreakerStateEnum.CLOSED.value,
            controlled_by_id=7,
            reason="deploy window",
            expires_at=expires_at,
        )
        before = repo.get_by_service_name(SVC)

        attempt = repo.trip_to_open(SVC, FAILURE_COUNT)

        # Then: nothing was written, and the caller learns the trip was
        # suppressed by an override rather than lost to a race.
        assert attempt.did_open is False
        assert attempt.state.state == CIRCUIT_BREAKER_PINNED_TOKEN
        assert attempt.state.manual_override_expires_at == expires_at
        after = repo.get_by_service_name(SVC)
        assert after.state == before.state
        assert after.updated_at == before.updated_at
        assert after.manually_controlled is True

    def test_lapsed_pin_trips_and_clears_the_stale_flag_in_the_same_write(self, repo):
        # Given: a CLOSED row whose override expired but whose flag survives
        # — the sweep that clears it runs in one process per host.
        repo.set_manual_control(
            SVC,
            CircuitBreakerStateEnum.CLOSED.value,
            controlled_by_id=7,
            reason="expired window",
            expires_at=utc_now() - timedelta(seconds=1),
        )

        attempt = repo.trip_to_open(SVC, FAILURE_COUNT)

        # Then: the trip lands AND the row re-enters the raw-flag readers'
        # view — the recovery lane filters on `not manually_controlled`.
        assert attempt.did_open is True
        assert attempt.state.state == CircuitBreakerStateEnum.OPEN.value
        stored = repo.get_by_service_name(SVC)
        assert stored.manually_controlled is False
        assert stored.manual_override_expires_at is None
        assert stored.controlled_by_id is None
        # The reason survives, as the expiry sweep leaves it.
        assert stored.control_reason == "expired window"

    def test_concurrent_trips_produce_exactly_one_winner(self, repo):
        # The single-fire gate: N threads that each decided to trip must
        # produce one did_open=True, or the service emits N events and charges
        # the shared error budget N times for one logical transition.
        repo.get_or_create(SVC)
        thread_count = 16
        barrier = threading.Barrier(thread_count)
        results: list[bool] = []
        results_lock = threading.Lock()

        def _trip() -> None:
            barrier.wait()
            attempt = repo.trip_to_open(SVC, FAILURE_COUNT)
            with results_lock:
                results.append(attempt.did_open)

        threads = [threading.Thread(target=_trip) for _ in range(thread_count)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        assert len(results) == thread_count
        assert sum(results) == 1
        # Every loser still sees the row OPEN, so no worker readmits traffic.
        assert repo.get_by_service_name(SVC).state == (
            CircuitBreakerStateEnum.OPEN.value
        )


# =============================================================================
# Behavior — update_state(clear_opened_at=...)
# =============================================================================


class TestInMemoryUpdateStateClearOpenedAtBehavior:
    """``opened_at=None`` means keep; the clear needs its own directive."""

    def test_directive_scrubs_the_open_era_timestamp(self, repo):
        # Given: an OPEN row carrying the instant it opened.
        repo.get_or_create(SVC)
        repo.update_state(
            service_name=SVC,
            state=CircuitBreakerStateEnum.OPEN.value,
            opened_at=utc_now(),
        )

        repo.update_state(
            service_name=SVC,
            state=CircuitBreakerStateEnum.CLOSED.value,
            clear_opened_at=True,
        )

        # Then: the row is internally consistent — closed, and reporting no
        # open. Without the directive it reads closed while still naming the
        # instant it opened, and no reader can tell which half is current.
        stored = repo.get_by_service_name(SVC)
        assert stored.state == CircuitBreakerStateEnum.CLOSED.value
        assert stored.opened_at is None

    def test_without_the_directive_the_timestamp_is_kept(self, repo):
        # Boundary partner: the default must not silently start clearing.
        repo.get_or_create(SVC)
        opened = utc_now()
        repo.update_state(
            service_name=SVC,
            state=CircuitBreakerStateEnum.OPEN.value,
            opened_at=opened,
        )

        repo.update_state(service_name=SVC, state=CircuitBreakerStateEnum.CLOSED.value)

        assert repo.get_by_service_name(SVC).opened_at == opened

    def test_directive_wins_over_a_supplied_timestamp(self, repo):
        repo.get_or_create(SVC)

        repo.update_state(
            service_name=SVC,
            state=CircuitBreakerStateEnum.CLOSED.value,
            opened_at=utc_now(),
            clear_opened_at=True,
        )

        assert repo.get_by_service_name(SVC).opened_at is None


# =============================================================================
# Behavior — update_state(skip_if_pinned=...)
# =============================================================================


class TestInMemoryUpdateStateSkipIfPinnedBehavior:
    """The pin test happens inside the lock hold that would do the write."""

    def test_active_pin_declines_the_write_and_reports_success(self, repo):
        expires_at = utc_now() + timedelta(minutes=10)
        repo.set_manual_control(
            SVC,
            CircuitBreakerStateEnum.OPEN.value,
            reason="operator block",
            expires_at=expires_at,
        )
        before = repo.get_by_service_name(SVC)

        result = repo.update_state(
            service_name=SVC,
            state=CircuitBreakerStateEnum.CLOSED.value,
            failure_count=0,
            skip_if_pinned=True,
        )

        # A declined write is a success for the caller: the store answered and
        # elided by contract, so quarantine accounting must not penalize it.
        assert result is True
        after = repo.get_by_service_name(SVC)
        assert after.state == before.state
        assert after.failure_count == before.failure_count
        assert after.updated_at == before.updated_at

    def test_lapsed_pin_does_not_decline_the_write(self, repo):
        # The negative twin: a raw-flag guard here would stop mirroring for
        # this service permanently, since no automatic transition clears the
        # flag and the sweep may never reach the row.
        repo.set_manual_control(
            SVC,
            CircuitBreakerStateEnum.OPEN.value,
            reason="expired block",
            expires_at=utc_now() - timedelta(seconds=1),
        )

        result = repo.update_state(
            service_name=SVC,
            state=CircuitBreakerStateEnum.CLOSED.value,
            failure_count=0,
            skip_if_pinned=True,
        )

        assert result is True
        assert (
            repo.get_by_service_name(SVC).state == CircuitBreakerStateEnum.CLOSED.value
        )

    def test_unpinned_row_is_written_normally(self, repo):
        repo.get_or_create(SVC)

        result = repo.update_state(
            service_name=SVC,
            state=CircuitBreakerStateEnum.OPEN.value,
            opened_at=utc_now(),
            skip_if_pinned=True,
        )

        assert result is True
        assert repo.get_by_service_name(SVC).state == CircuitBreakerStateEnum.OPEN.value

    def test_without_the_directive_a_pinned_row_is_overwritten(self, repo):
        # Establishes that the guard is what protects the row, not some other
        # property of update_state — the manual-control write-through is the
        # operator's own write and must keep landing.
        repo.set_manual_control(
            SVC,
            CircuitBreakerStateEnum.OPEN.value,
            reason="operator block",
            expires_at=utc_now() + timedelta(minutes=10),
        )

        repo.update_state(service_name=SVC, state=CircuitBreakerStateEnum.CLOSED.value)

        assert (
            repo.get_by_service_name(SVC).state == CircuitBreakerStateEnum.CLOSED.value
        )

    def test_missing_row_still_reports_failure(self, repo):
        # The pin guard must not swallow the pre-existing "no such row"
        # answer, which is False rather than a declined-by-contract True.
        result = repo.update_state(
            service_name="never-created",
            state=CircuitBreakerStateEnum.OPEN.value,
            skip_if_pinned=True,
        )

        assert result is False
