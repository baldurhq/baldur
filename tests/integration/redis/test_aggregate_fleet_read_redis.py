"""The system-wide failure rate's fleet term, end to end over a real Redis L2.

793 D1. Two worker processes share one breaker store: each is a
``CircuitBreakerService`` over its own layered repository (L1 in-memory, L2 the
real Redis adapter). One worker trips a name; the other never touched it. The
reading worker's fleet aggregate counts the peer's trip at the floor
(``max(failure_count, failure_threshold)``) for as long as the store row is
open or half-open, without hydrating the row into its own L1; its process-only
aggregate does not see it; and the term ends when the tripping worker closes
the name.

Test categories:
    A. The cross-worker OPEN term: counted at the floor, not hydrated.
    B. The term's lifetime: HALF_OPEN still counts, a close ends it.
    C. The process-only read never dials the store.

All tests require a running Redis instance (``requires_redis`` auto-skip).
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

import pytest

from baldur.adapters.memory.layered_repository import (
    LayeredCircuitBreakerStateRepository,
    reset_layered_repository_executor,
)
from baldur.interfaces.repositories import (
    CircuitBreakerStateData,
    CircuitBreakerStateEnum,
)
from baldur.services.circuit_breaker.config import CircuitBreakerConfig
from baldur.services.circuit_breaker.service import CircuitBreakerService
from baldur.utils.time import utc_now

pytestmark = pytest.mark.requires_redis

NAME = "shared-dependency"
FAILURE_THRESHOLD = 5
OPEN = CircuitBreakerStateEnum.OPEN.value
HALF_OPEN = CircuitBreakerStateEnum.HALF_OPEN.value
CLOSED = CircuitBreakerStateEnum.CLOSED.value


@pytest.fixture(autouse=True)
def _reset_redis_unavailable_flag():
    from baldur.adapters.redis import _redis_state

    state = _redis_state()
    state.unavailable = False
    state.fail_time = 0.0
    yield
    state.unavailable = False
    state.fail_time = 0.0


def _worker(store) -> CircuitBreakerService:
    """One worker process: its own L1, the shared store as L2."""
    layered = LayeredCircuitBreakerStateRepository(l2_repo=store, adapter_type="redis")
    layered._get_timeout_seconds = lambda: 5.0
    return CircuitBreakerService(
        config=CircuitBreakerConfig(
            enabled=True,
            failure_threshold=FAILURE_THRESHOLD,
            success_threshold=1,
            minimum_calls=10,
            recovery_timeout=60,
        ),
        repository=layered,
    )


@pytest.fixture
def workers(redis_circuit_breaker_repository):
    """``(tripping, reading)`` — two workers over one store."""
    tripping = _worker(redis_circuit_breaker_repository)
    reading = _worker(redis_circuit_breaker_repository)
    yield tripping, reading
    reset_layered_repository_executor()


def _trip(worker: CircuitBreakerService) -> None:
    with (
        patch.object(worker, "_log_circuit_open_audit"),
        patch.object(worker, "_apply_burn_rate_multiplier"),
    ):
        for _ in range(FAILURE_THRESHOLD):
            worker.record_failure(NAME)
    reset_layered_repository_executor()


# =============================================================================
# A. The cross-worker OPEN term
# =============================================================================


class TestFleetReadCountsAPeersTrip:
    """The reading worker sees a trip it never took."""

    def test_peers_trip_is_counted_at_the_floor_without_hydration(
        self, workers, redis_circuit_breaker_repository
    ):
        """
        Purpose:
            The fleet term: a name tripped in another worker counts here.
        Expected:
            - the store row is OPEN (the trip reached L2)
            - the reader's fleet evidence has one open circuit at the floor
            - the reader's L1 still has no row for the name
        """
        tripping, reading = workers
        _trip(tripping)
        assert redis_circuit_breaker_repository.get_by_service_name(NAME).state == OPEN

        evidence = reading.get_aggregate_failure_evidence()

        assert evidence.fleet_read is True
        assert evidence.open_circuits == 1
        assert (evidence.failures, evidence.total_calls) == (
            FAILURE_THRESHOLD,
            FAILURE_THRESHOLD,
        )
        assert evidence.rate == 1.0
        assert reading.repository._l1.get_by_service_name(NAME) is None

    def test_tripping_worker_counts_its_own_evidence_not_the_floor(self, workers):
        """The floor is a no-op where the window already holds the trip."""
        tripping, _ = workers
        for _ in range(3):
            tripping.record_success(NAME)
        _trip(tripping)

        evidence = tripping.get_aggregate_failure_evidence()

        assert evidence.open_circuits == 1
        assert (evidence.failures, evidence.total_calls) == (FAILURE_THRESHOLD, 8)

    def test_healthy_traffic_in_the_reader_dilutes_the_peers_trip(self, workers):
        """The two terms add: local successes and the peer's floor share one rate."""
        tripping, reading = workers
        _trip(tripping)
        reading.repository._l1.get_or_create("local-only")
        for _ in range(FAILURE_THRESHOLD):
            reading.record_success("local-only")

        evidence = reading.get_aggregate_failure_evidence()

        assert (evidence.failures, evidence.total_calls) == (
            FAILURE_THRESHOLD,
            FAILURE_THRESHOLD * 2,
        )
        assert evidence.rate == 0.5


# =============================================================================
# B. The term's lifetime
# =============================================================================


class TestFleetReadTermLifetime:
    """Open and half-open count; a close ends the term."""

    def test_half_open_store_row_still_counts(
        self, workers, redis_circuit_breaker_repository
    ):
        tripping, reading = workers
        _trip(tripping)
        redis_circuit_breaker_repository.update_state(
            NAME, state=HALF_OPEN, opened_at=utc_now() - timedelta(seconds=120)
        )

        evidence = reading.get_aggregate_failure_evidence()

        assert evidence.open_circuits == 1
        assert evidence.failures == FAILURE_THRESHOLD

    def test_close_in_the_tripping_worker_ends_the_term_for_the_reader(
        self, workers, redis_circuit_breaker_repository
    ):
        """
        Purpose:
            The term lasts exactly as long as the store row is non-CLOSED.
        Expected:
            - before the close the reader counts one open circuit
            - after the tripping worker's trial success closes the name, the
              reader's next fleet read counts none
        """
        tripping, reading = workers
        _trip(tripping)
        assert reading.get_aggregate_failure_evidence().open_circuits == 1

        # The store's recovery timeout elapses; the trial succeeds and closes.
        redis_circuit_breaker_repository.update_state(
            NAME, state=OPEN, opened_at=utc_now() - timedelta(seconds=120)
        )
        tripping.repository._l1.hydrate_snapshot(
            CircuitBreakerStateData(
                service_name=NAME,
                state=OPEN,
                failure_count=FAILURE_THRESHOLD,
                opened_at=utc_now() - timedelta(seconds=120),
            )
        )
        assert tripping.should_allow(NAME) is True
        tripping.record_success(NAME)
        reset_layered_repository_executor()
        assert (
            redis_circuit_breaker_repository.get_by_service_name(NAME).state == CLOSED
        )

        evidence = reading.get_aggregate_failure_evidence()

        assert evidence.open_circuits == 0
        assert evidence == type(evidence)(
            failures=0, total_calls=0, open_circuits=0, fleet_read=True
        )


# =============================================================================
# C. The process-only read
# =============================================================================


class TestProcessOnlyReadIgnoresTheStore:
    """``fleet=False`` is this worker's rows alone."""

    def test_process_only_read_does_not_see_the_peers_trip(self, workers):
        tripping, reading = workers
        _trip(tripping)

        evidence = reading.get_aggregate_failure_evidence(fleet=False)

        assert evidence.fleet_read is False
        assert evidence.open_circuits == 0
        assert (evidence.failures, evidence.total_calls) == (0, 0)

    def test_process_only_read_never_asks_the_store(self, workers):
        _, reading = workers

        with patch.object(
            reading.repository,
            "get_cluster_states",
            wraps=reading.repository.get_cluster_states,
        ) as cluster_read:
            reading.get_aggregate_failure_evidence(fleet=False)

        cluster_read.assert_not_called()
