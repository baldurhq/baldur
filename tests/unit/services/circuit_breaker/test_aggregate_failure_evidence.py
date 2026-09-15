"""The system-wide failure rate: refused calls and tripped breakers count.

793 D1/D12. ``get_aggregate_failure_evidence`` measures the share of protected
calls that did not succeed. Two terms: each name's outcome window (admitted
CLOSED calls by their result, refused calls as failures), and a floor for every
open / half-open name — this process's own rows or the shared store's — at the
failures that tripped it. The rate and its denominator travel together so a
consumer can tell an observed zero from an empty reading, and a shared store
that cannot be read propagates instead of reading as ``0.0``.

Verification techniques applied:
- Contract: ``AggregateFailureEvidence.rate`` at its two boundaries, frozen
- Boundary: a hydrated OPEN / HALF_OPEN row with an empty window is floored at
  ``max(failure_count, failure_threshold)``; after N refusals at ``max(N, w)``
- Equivalence: the union of cluster and L1 rows counts a peer name L1 never
  held, and the read does not hydrate it
- Error path: ``unreached_default_store`` falls back to the process view with
  ``fleet_read=False``; every other reason re-raises; ``fleet=False`` never
  dials the store
- Exclusion: a pin-active row is neither floored nor counted as open
- State transition: the aggregate walk clears a name whose L1 row went CLOSED
  with no local traffic
- Error path: ``get_aggregate_failure_rate`` propagates, never ``0.0``
"""

from __future__ import annotations

import dataclasses
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
from baldur.services.circuit_breaker.config import (
    AggregateFailureEvidence,
    CircuitBreakerConfig,
)
from baldur.services.circuit_breaker.exceptions import (
    UNREACHED_DEFAULT_STORE_REASON,
    CircuitBreakerStateUnavailableError,
)
from baldur.services.circuit_breaker.service import CircuitBreakerService
from baldur.utils.time import utc_now

FAILURE_THRESHOLD = 5
WINDOW_SIZE = 100
LOCAL = "local-api"
PEER = "peer-api"


class _StubCBRepo(InMemoryCircuitBreakerStateRepository):
    """The in-memory adapter with a scriptable cluster read.

    ``get_cluster_states`` answers the hand-built peer rows, or raises the
    typed error with the given reason; every local operation is the real
    adapter's, so the L1 rows the aggregate walks are real rows.
    """

    def __init__(self, cluster_rows=None, *, raise_reason: str | None = None):
        super().__init__()
        self._cluster_rows = list(cluster_rows or [])
        self._raise_reason = raise_reason
        self.cluster_reads = 0

    def get_cluster_states(self) -> list[CircuitBreakerStateData]:
        self.cluster_reads += 1
        if self._raise_reason is not None:
            raise CircuitBreakerStateUnavailableError(
                "get_cluster_states", self._raise_reason
            )
        return list(self._cluster_rows)


def _row(name: str, state: str, failure_count: int = 0, **overrides):
    return CircuitBreakerStateData(
        service_name=name,
        state=state,
        failure_count=failure_count,
        opened_at=utc_now() if state != CircuitBreakerStateEnum.CLOSED.value else None,
        **overrides,
    )


def _service(repo) -> CircuitBreakerService:
    return CircuitBreakerService(
        config=CircuitBreakerConfig(
            enabled=True,
            failure_threshold=FAILURE_THRESHOLD,
            sliding_window_size=WINDOW_SIZE,
            minimum_calls=10,
        ),
        repository=repo,
    )


def _seed_window(
    service: CircuitBreakerService, name: str, failures: int, successes: int
):
    window = service._outcome_window
    for _ in range(failures):
        window.record_failure(name, WINDOW_SIZE)
    for _ in range(successes):
        window.record_success(name, WINDOW_SIZE)


# =============================================================================
# Contract — the evidence value
# =============================================================================


class TestAggregateFailureEvidenceContract:
    """The value the readers consume."""

    def test_rate_over_zero_calls_is_an_observed_zero(self):
        evidence = AggregateFailureEvidence(
            failures=0, total_calls=0, open_circuits=0, fleet_read=True
        )

        assert evidence.rate == 0.0

    def test_rate_with_every_call_failed_is_one(self):
        evidence = AggregateFailureEvidence(
            failures=7, total_calls=7, open_circuits=1, fleet_read=True
        )

        assert evidence.rate == 1.0

    def test_rate_is_the_failure_fraction(self):
        evidence = AggregateFailureEvidence(
            failures=1, total_calls=4, open_circuits=0, fleet_read=False
        )

        assert evidence.rate == 0.25

    def test_evidence_is_frozen(self):
        evidence = AggregateFailureEvidence(
            failures=1, total_calls=4, open_circuits=0, fleet_read=False
        )

        with pytest.raises(dataclasses.FrozenInstanceError):
            evidence.failures = 2  # type: ignore[misc]

    def test_evidence_uses_slots(self):
        """No ``__dict__``: the value is allocated like a tuple."""
        evidence = AggregateFailureEvidence(
            failures=0, total_calls=0, open_circuits=0, fleet_read=False
        )

        assert not hasattr(evidence, "__dict__")


# =============================================================================
# Behavior — the aggregate read
# =============================================================================


class TestAggregateFailureEvidenceBehavior:
    """The two terms, their union, their exclusions and their error path."""

    def test_no_rows_and_no_traffic_reads_zero_over_zero(self):
        service = _service(_StubCBRepo())

        evidence = service.get_aggregate_failure_evidence()

        assert evidence == AggregateFailureEvidence(
            failures=0, total_calls=0, open_circuits=0, fleet_read=True
        )

    def test_closed_traffic_is_summed_across_names(self):
        """The in-process term alone: the per-name windows, summed."""
        repo = _StubCBRepo()
        service = _service(repo)
        repo.get_or_create("a")
        repo.get_or_create("b")
        _seed_window(service, "a", failures=2, successes=8)
        _seed_window(service, "b", failures=0, successes=10)

        evidence = service.get_aggregate_failure_evidence()

        assert (evidence.failures, evidence.total_calls) == (2, 20)
        assert evidence.open_circuits == 0
        assert evidence.rate == pytest.approx(0.1)

    @pytest.mark.parametrize(
        "state",
        [CircuitBreakerStateEnum.OPEN.value, CircuitBreakerStateEnum.HALF_OPEN.value],
        ids=["open", "half_open"],
    )
    @pytest.mark.parametrize(
        ("failure_count", "expected_floor"),
        [
            (0, FAILURE_THRESHOLD),
            (FAILURE_THRESHOLD - 2, FAILURE_THRESHOLD),
            (FAILURE_THRESHOLD, FAILURE_THRESHOLD),
            (FAILURE_THRESHOLD + 3, FAILURE_THRESHOLD + 3),
        ],
        ids=[
            "count_zero",
            "count_below_threshold",
            "count_at_threshold",
            "count_above",
        ],
    )
    @pytest.mark.parametrize("fleet", [True, False], ids=["fleet", "process_only"])
    def test_hydrated_non_closed_row_with_empty_window_is_floored(
        self, state, failure_count, expected_floor, fleet
    ):
        """Boundary: ``max(failure_count, failure_threshold)`` failed calls.

        The row arrived by hydration — this process holds no evidence for the
        trip — so the reading is never the pre-fix ``(0, 0)``.
        """
        repo = _StubCBRepo()
        service = _service(repo)
        repo.hydrate_snapshot(_row(LOCAL, state, failure_count=failure_count))

        evidence = service.get_aggregate_failure_evidence(fleet=fleet)

        assert (evidence.failures, evidence.total_calls) == (
            expected_floor,
            expected_floor,
        )
        assert evidence.open_circuits == 1
        assert evidence.rate == 1.0
        assert evidence.fleet_read is fleet

    @pytest.mark.parametrize(
        ("refusals", "expected_failures"),
        [
            (1, FAILURE_THRESHOLD),
            (FAILURE_THRESHOLD, FAILURE_THRESHOLD),
            (FAILURE_THRESHOLD + 4, FAILURE_THRESHOLD + 4),
        ],
        ids=["below_floor", "at_floor", "above_floor"],
    )
    @pytest.mark.parametrize("fleet", [True, False], ids=["fleet", "process_only"])
    def test_refusals_on_a_hydrated_open_row_count_at_max_of_n_and_floor(
        self, refusals, expected_failures, fleet
    ):
        """Boundary: N refusals read as ``max(N, w)`` — the floor never double-counts."""
        repo = _StubCBRepo()
        service = _service(repo)
        repo.hydrate_snapshot(_row(LOCAL, CircuitBreakerStateEnum.OPEN.value))
        row = repo.get_by_service_name(LOCAL)
        for _ in range(refusals):
            service.record_rejection(LOCAL, row)

        evidence = service.get_aggregate_failure_evidence(fleet=fleet)

        assert (evidence.failures, evidence.total_calls) == (
            expected_failures,
            expected_failures,
        )
        assert evidence.open_circuits == 1

    def test_the_process_that_tripped_the_name_is_not_lifted(self):
        """The floor is a no-op where the window already holds the trip."""
        repo = _StubCBRepo()
        service = _service(repo)
        repo.get_or_create(LOCAL)
        _seed_window(service, LOCAL, failures=FAILURE_THRESHOLD, successes=5)
        repo.trip_to_open(LOCAL, FAILURE_THRESHOLD)

        evidence = service.get_aggregate_failure_evidence()

        assert (evidence.failures, evidence.total_calls) == (FAILURE_THRESHOLD, 10)
        assert evidence.open_circuits == 1

    def test_cluster_row_for_a_name_l1_never_held_is_counted(self):
        """Equivalence: the union of cluster and local rows — a peer's trip counts."""
        repo = _StubCBRepo(
            cluster_rows=[
                _row(PEER, CircuitBreakerStateEnum.OPEN.value, failure_count=8)
            ]
        )
        service = _service(repo)
        repo.get_or_create(LOCAL)
        _seed_window(service, LOCAL, failures=0, successes=10)

        evidence = service.get_aggregate_failure_evidence()

        assert (evidence.failures, evidence.total_calls) == (8, 18)
        assert evidence.open_circuits == 1
        assert evidence.fleet_read is True

    def test_cluster_read_does_not_hydrate_the_peer_row_into_l1(self):
        """The aggregate is a read: a layered L1 stays without the peer's row."""
        from baldur.adapters.memory.circuit_breaker import (
            LayeredCircuitBreakerStateRepository,
        )

        l2 = _StubCBRepo(
            cluster_rows=[
                _row(PEER, CircuitBreakerStateEnum.OPEN.value, failure_count=3)
            ]
        )
        with (
            patch(
                "baldur.adapters.memory.layered_repository.drift_operations."
                "DriftOperationsMixin._schedule_drift_reconciliation",
                return_value=None,
            ),
            patch(
                "baldur.adapters.memory.layered_repository.base."
                "LayeredRepositoryBase._ensure_l2_warmup_once",
                return_value=None,
            ),
        ):
            layered = LayeredCircuitBreakerStateRepository(
                l2_repo=l2, adapter_type="redis"
            )
            service = _service(layered)

            evidence = service.get_aggregate_failure_evidence()

        assert evidence.open_circuits == 1
        assert (evidence.failures, evidence.total_calls) == (
            FAILURE_THRESHOLD,
            FAILURE_THRESHOLD,
        )
        assert layered._l1.get_by_service_name(PEER) is None

    def test_local_non_closed_row_outranks_the_cluster_row_for_the_weight(self):
        """The weight comes from the row that makes the name non-CLOSED here."""
        repo = _StubCBRepo(
            cluster_rows=[
                _row(LOCAL, CircuitBreakerStateEnum.OPEN.value, failure_count=9)
            ]
        )
        service = _service(repo)
        repo.hydrate_snapshot(
            _row(LOCAL, CircuitBreakerStateEnum.OPEN.value, failure_count=6)
        )

        evidence = service.get_aggregate_failure_evidence()

        assert evidence.failures == 6
        assert evidence.open_circuits == 1

    def test_closed_local_row_beside_a_peer_open_cluster_row_takes_the_peer_weight(
        self,
    ):
        """A CLOSED L1 row is not the trip; the cluster row's own count is."""
        repo = _StubCBRepo(
            cluster_rows=[
                _row(LOCAL, CircuitBreakerStateEnum.OPEN.value, failure_count=9)
            ]
        )
        service = _service(repo)
        repo.get_or_create(LOCAL)

        evidence = service.get_aggregate_failure_evidence()

        assert evidence.failures == 9
        assert evidence.open_circuits == 1

    def test_unreached_default_store_falls_back_to_the_process_view(self):
        """Error path: nobody named a store, so this process's view is the cluster."""
        repo = _StubCBRepo(raise_reason=UNREACHED_DEFAULT_STORE_REASON)
        service = _service(repo)
        repo.hydrate_snapshot(_row(LOCAL, CircuitBreakerStateEnum.OPEN.value))

        evidence = service.get_aggregate_failure_evidence()

        assert evidence.fleet_read is False
        assert evidence.open_circuits == 1
        assert evidence.failures == FAILURE_THRESHOLD

    @pytest.mark.parametrize(
        "reason",
        [
            "l2_quarantined",
            "l2_timeout",
            "l2_absent",
            "degraded_backend",
            "partial_scan",
        ],
    )
    def test_every_other_unavailable_reason_re_raises(self, reason):
        """A store that cannot be read never reads as a healthy process view."""
        service = _service(_StubCBRepo(raise_reason=reason))

        with pytest.raises(CircuitBreakerStateUnavailableError) as excinfo:
            service.get_aggregate_failure_evidence()

        assert excinfo.value.reason == reason

    def test_process_only_read_never_dials_the_store(self):
        """``fleet=False`` reads this process's rows alone, even on a raising store."""
        repo = _StubCBRepo(raise_reason="l2_quarantined")
        service = _service(repo)
        repo.hydrate_snapshot(_row(LOCAL, CircuitBreakerStateEnum.OPEN.value))

        evidence = service.get_aggregate_failure_evidence(fleet=False)

        assert repo.cluster_reads == 0
        assert evidence.fleet_read is False
        assert evidence.open_circuits == 1

    def test_pin_active_open_row_is_neither_floored_nor_counted(self):
        """Exclusion: an operator's Block is not an observation about the dependency."""
        repo = _StubCBRepo()
        service = _service(repo)
        repo.hydrate_snapshot(
            _row(
                LOCAL,
                CircuitBreakerStateEnum.OPEN.value,
                failure_count=4,
                manually_controlled=True,
                manual_override_expires_at=utc_now() + timedelta(minutes=10),
            )
        )

        evidence = service.get_aggregate_failure_evidence()

        assert evidence == AggregateFailureEvidence(
            failures=0, total_calls=0, open_circuits=0, fleet_read=True
        )

    def test_pin_active_cluster_row_is_excluded_too(self):
        repo = _StubCBRepo(
            cluster_rows=[
                _row(
                    PEER,
                    CircuitBreakerStateEnum.OPEN.value,
                    failure_count=4,
                    manually_controlled=True,
                    manual_override_expires_at=utc_now() + timedelta(minutes=10),
                )
            ]
        )
        service = _service(repo)

        evidence = service.get_aggregate_failure_evidence()

        assert evidence.open_circuits == 0
        assert evidence.failures == 0

    def test_lapsed_pin_on_an_open_row_is_a_trip_again(self):
        """Control for the exclusion: a pin past its expiry no longer excludes."""
        repo = _StubCBRepo()
        service = _service(repo)
        repo.hydrate_snapshot(
            _row(
                LOCAL,
                CircuitBreakerStateEnum.OPEN.value,
                manually_controlled=True,
                manual_override_expires_at=utc_now() - timedelta(seconds=1),
            )
        )

        evidence = service.get_aggregate_failure_evidence()

        assert evidence.open_circuits == 1
        assert evidence.failures == FAILURE_THRESHOLD

    def test_aggregate_walk_clears_a_name_whose_l1_row_went_closed(self):
        """State transition: a peer's close reaching L1 clears the window here.

        The name had refusals recorded while this process saw it OPEN and no
        traffic since; the row is now CLOSED (a peer's close, hydrated). The
        aggregate read observes every fresh row it holds, so the refusals are
        gone within one read.
        """
        repo = _StubCBRepo()
        service = _service(repo)
        repo.hydrate_snapshot(_row(LOCAL, CircuitBreakerStateEnum.OPEN.value))
        service._outcome_window.observe_state(LOCAL, CircuitBreakerStateEnum.OPEN.value)
        service.record_rejection(LOCAL, repo.get_by_service_name(LOCAL))
        assert service.get_window_evidence(LOCAL) == (1, 1)
        repo.hydrate_snapshot(_row(LOCAL, CircuitBreakerStateEnum.CLOSED.value))

        evidence = service.get_aggregate_failure_evidence()

        assert evidence == AggregateFailureEvidence(
            failures=0, total_calls=0, open_circuits=0, fleet_read=True
        )
        assert service.get_window_evidence(LOCAL) == (0, 0)

    def test_aggregate_walk_clears_the_closed_name_exactly_once(self):
        """A success admitted after the observed close survives the next read."""
        repo = _StubCBRepo()
        service = _service(repo)
        repo.hydrate_snapshot(_row(LOCAL, CircuitBreakerStateEnum.OPEN.value))
        service._outcome_window.observe_state(LOCAL, CircuitBreakerStateEnum.OPEN.value)
        repo.hydrate_snapshot(_row(LOCAL, CircuitBreakerStateEnum.CLOSED.value))
        service.get_aggregate_failure_evidence()
        service._outcome_window.record_success(LOCAL, WINDOW_SIZE)

        evidence = service.get_aggregate_failure_evidence()

        assert (evidence.failures, evidence.total_calls) == (0, 1)


# =============================================================================
# Behavior — the rate view propagates
# =============================================================================


class TestAggregateFailureRatePropagationBehavior:
    """``get_aggregate_failure_rate`` is the evidence's rate, errors included."""

    def test_rate_is_the_evidence_rate(self):
        repo = _StubCBRepo()
        service = _service(repo)
        repo.get_or_create(LOCAL)
        _seed_window(service, LOCAL, failures=3, successes=9)

        assert service.get_aggregate_failure_rate() == pytest.approx(0.25)
        assert service.get_aggregate_failure_rate(fleet=False) == pytest.approx(0.25)

    def test_unavailable_store_propagates_and_never_reads_zero(self):
        """Negative: the pre-fix ``0.0`` on unavailability never occurs."""
        service = _service(_StubCBRepo(raise_reason="l2_quarantined"))

        with pytest.raises(CircuitBreakerStateUnavailableError):
            service.get_aggregate_failure_rate()

    def test_process_only_rate_ignores_the_unavailable_store(self):
        repo = _StubCBRepo(raise_reason="l2_quarantined")
        service = _service(repo)
        repo.get_or_create(LOCAL)
        _seed_window(service, LOCAL, failures=1, successes=1)

        assert service.get_aggregate_failure_rate(fleet=False) == pytest.approx(0.5)
        assert repo.cluster_reads == 0
