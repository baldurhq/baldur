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
- Contract: the tripped share's defaults and ``measurable_rate`` at its
  boundaries — ``None`` over zero measurable calls, unlike ``rate``
- Equivalence over row shapes for the tripped share: a floored name is held
  with its window after the lift; a name this process holds open on its own
  row is held even where the shared store already reads it pinned; a pin on
  either row drops the floor only, never the window
- State partition of ``_tripped_row`` / ``_held_on_own_row``: which row makes
  a name count, and when an override on either row makes it count as none
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

    def test_tripped_share_defaults_to_nothing_held(self):
        """A reader built on the four original fields sees an empty share."""
        evidence = AggregateFailureEvidence(
            failures=3, total_calls=10, open_circuits=0, fleet_read=True
        )

        assert (
            evidence.tripped_failures,
            evidence.tripped_calls,
            evidence.tripped_names,
        ) == (0, 0, ())
        assert evidence.measurable_calls == 10
        assert evidence.measurable_rate == pytest.approx(0.3)

    @pytest.mark.parametrize(
        ("failures", "total_calls", "tripped_failures", "tripped_calls", "expected"),
        [
            (0, 0, 0, 0, None),
            (5, 5, 5, 5, None),
            (10, 20, 8, 8, 2 / 12),
            (8, 20, 8, 8, 0.0),
            (3, 10, 0, 0, 0.3),
        ],
        ids=[
            "nothing_observed",
            "every_call_held",
            "held_share_removed",
            "only_the_held_share_failed",
            "nothing_held",
        ],
    )
    def test_measurable_rate_is_the_rate_without_the_tripped_share(
        self, failures, total_calls, tripped_failures, tripped_calls, expected
    ):
        """Boundary: ``None`` over zero measurable calls, the fraction otherwise."""
        evidence = AggregateFailureEvidence(
            failures=failures,
            total_calls=total_calls,
            open_circuits=1,
            fleet_read=True,
            tripped_failures=tripped_failures,
            tripped_calls=tripped_calls,
        )

        if expected is None:
            assert evidence.measurable_rate is None
        else:
            assert evidence.measurable_rate == pytest.approx(expected)
        assert evidence.measurable_calls == total_calls - tripped_calls

    def test_measurable_rate_over_zero_calls_is_none_where_rate_is_zero(self):
        """Negative: the two properties disagree exactly on the empty reading.

        ``rate`` keeps its observed ``0.0`` (a consumer renders it with
        ``total_calls``); ``measurable_rate`` is read where a pass on no
        evidence would lift a hold, so the empty reading is not a number.
        """
        evidence = AggregateFailureEvidence(
            failures=FAILURE_THRESHOLD,
            total_calls=FAILURE_THRESHOLD,
            open_circuits=1,
            fleet_read=True,
            tripped_failures=FAILURE_THRESHOLD,
            tripped_calls=FAILURE_THRESHOLD,
            tripped_names=(LOCAL,),
        )

        assert evidence.rate == 1.0
        assert evidence.measurable_rate is None


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
        assert evidence.tripped_names == ()
        assert evidence.measurable_rate == pytest.approx(evidence.rate)

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
        # The floored name is held: its lifted window is the whole share.
        assert evidence.tripped_names == (LOCAL,)
        assert (evidence.tripped_failures, evidence.tripped_calls) == (
            expected_floor,
            expected_floor,
        )
        assert evidence.measurable_rate is None

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
        assert evidence.tripped_names == ()

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
        assert evidence.tripped_names == (LOCAL,)

    def test_tripped_share_is_the_held_windows_and_leaves_the_rest_measurable(self):
        """Equivalence: a held name's refusals move to the share; the rate is unchanged.

        Negative: ``failures`` / ``total_calls`` / ``open_circuits`` read
        exactly as they did before the share existed — the share is carved
        out of the totals, not added to them.
        """
        repo = _StubCBRepo()
        service = _service(repo)
        repo.hydrate_snapshot(_row(LOCAL, CircuitBreakerStateEnum.OPEN.value))
        held_row = repo.get_by_service_name(LOCAL)
        for _ in range(FAILURE_THRESHOLD + 2):
            service.record_rejection(LOCAL, held_row)
        repo.get_or_create(PEER)
        _seed_window(service, PEER, failures=1, successes=9)

        evidence = service.get_aggregate_failure_evidence()

        assert (evidence.failures, evidence.total_calls) == (FAILURE_THRESHOLD + 3, 17)
        assert evidence.open_circuits == 1
        assert (evidence.tripped_failures, evidence.tripped_calls) == (
            FAILURE_THRESHOLD + 2,
            FAILURE_THRESHOLD + 2,
        )
        assert evidence.tripped_names == (LOCAL,)
        assert evidence.measurable_calls == 10
        assert evidence.measurable_rate == pytest.approx(0.1)

    def test_peer_open_cluster_row_is_held_at_its_floor(self):
        """A trip a peer committed is held with the floor as its whole window."""
        repo = _StubCBRepo(
            cluster_rows=[
                _row(PEER, CircuitBreakerStateEnum.OPEN.value, failure_count=8)
            ]
        )
        service = _service(repo)
        repo.get_or_create(LOCAL)
        _seed_window(service, LOCAL, failures=2, successes=8)

        evidence = service.get_aggregate_failure_evidence()

        assert (evidence.tripped_failures, evidence.tripped_calls) == (8, 8)
        assert evidence.tripped_names == (PEER,)
        assert evidence.measurable_rate == pytest.approx(0.2)

    def test_tripped_names_are_sorted(self):
        """Two held names read in one order whatever the set iteration gave."""
        repo = _StubCBRepo(
            cluster_rows=[_row("zeta", CircuitBreakerStateEnum.OPEN.value)]
        )
        service = _service(repo)
        repo.hydrate_snapshot(_row("alpha", CircuitBreakerStateEnum.HALF_OPEN.value))

        evidence = service.get_aggregate_failure_evidence()

        assert evidence.tripped_names == ("alpha", "zeta")
        assert evidence.open_circuits == 2

    def test_pinned_closed_cluster_row_drops_the_floor_but_the_stale_local_open_row_is_still_held(
        self,
    ):
        """A force-close landed on another worker; this mirror still reads OPEN.

        The pin drops the floor only: the refusals this process recorded on
        its stale row stay in ``rate`` (negative: the window is not dropped)
        and, because this process still refuses on that row, the name is
        held — in ``tripped_names`` with its window in the share.
        """
        repo = _StubCBRepo(
            cluster_rows=[
                _row(
                    LOCAL,
                    CircuitBreakerStateEnum.CLOSED.value,
                    manually_controlled=True,
                    manual_override_expires_at=utc_now() + timedelta(minutes=10),
                )
            ]
        )
        service = _service(repo)
        repo.hydrate_snapshot(
            _row(LOCAL, CircuitBreakerStateEnum.OPEN.value, failure_count=3)
        )
        stale_row = repo.get_by_service_name(LOCAL)
        for _ in range(2):
            service.record_rejection(LOCAL, stale_row)
        repo.get_or_create(PEER)
        _seed_window(service, PEER, failures=0, successes=10)

        evidence = service.get_aggregate_failure_evidence()

        assert evidence.open_circuits == 0
        assert (evidence.failures, evidence.total_calls) == (2, 12)
        assert (evidence.tripped_failures, evidence.tripped_calls) == (2, 2)
        assert evidence.tripped_names == (LOCAL,)
        assert evidence.measurable_rate == pytest.approx(0.0)
        assert evidence.measurable_calls == 10

    def test_pinned_open_cluster_row_drops_the_floor_of_the_local_open_row_too(self):
        """An operator's Block in the shared store is not a trip on this mirror either."""
        repo = _StubCBRepo(
            cluster_rows=[
                _row(
                    LOCAL,
                    CircuitBreakerStateEnum.OPEN.value,
                    failure_count=9,
                    manually_controlled=True,
                    manual_override_expires_at=utc_now() + timedelta(minutes=10),
                )
            ]
        )
        service = _service(repo)
        repo.hydrate_snapshot(
            _row(LOCAL, CircuitBreakerStateEnum.OPEN.value, failure_count=6)
        )

        evidence = service.get_aggregate_failure_evidence()

        assert evidence.open_circuits == 0
        assert (evidence.failures, evidence.total_calls) == (0, 0)
        assert evidence.tripped_names == (LOCAL,)

    def test_lapsed_pin_on_the_cluster_row_no_longer_drops_the_local_floor(self):
        """Control for the either-row pin rule: an expired shared-store pin counts by state again."""
        repo = _StubCBRepo(
            cluster_rows=[
                _row(
                    LOCAL,
                    CircuitBreakerStateEnum.CLOSED.value,
                    manually_controlled=True,
                    manual_override_expires_at=utc_now() - timedelta(seconds=1),
                )
            ]
        )
        service = _service(repo)
        repo.hydrate_snapshot(
            _row(LOCAL, CircuitBreakerStateEnum.OPEN.value, failure_count=6)
        )

        evidence = service.get_aggregate_failure_evidence()

        assert evidence.open_circuits == 1
        assert evidence.failures == 6
        assert evidence.tripped_names == (LOCAL,)


# =============================================================================
# Behavior — which row makes a name count
# =============================================================================


def _pinned(name: str, state: str, *, lapsed: bool = False, **overrides):
    delta = timedelta(seconds=-1) if lapsed else timedelta(minutes=10)
    return _row(
        name,
        state,
        manually_controlled=True,
        manual_override_expires_at=utc_now() + delta,
        **overrides,
    )


_OPEN = CircuitBreakerStateEnum.OPEN.value
_HALF_OPEN = CircuitBreakerStateEnum.HALF_OPEN.value
_CLOSED = CircuitBreakerStateEnum.CLOSED.value


class TestTrippedRowPartitionBehavior:
    """``_tripped_row`` and ``_held_on_own_row``: pure reads over the two rows."""

    @pytest.mark.parametrize(
        ("local_row", "cluster_row", "expected"),
        [
            pytest.param(None, None, None, id="no_rows"),
            pytest.param(
                _row(LOCAL, _CLOSED), _row(LOCAL, _CLOSED), None, id="both_closed"
            ),
            pytest.param(_row(LOCAL, _OPEN, 6), None, "local", id="local_open_alone"),
            pytest.param(
                _row(LOCAL, _OPEN, 6),
                _row(LOCAL, _OPEN, 9),
                "local",
                id="local_open_wins",
            ),
            pytest.param(
                _row(LOCAL, _CLOSED),
                _row(LOCAL, _HALF_OPEN, 9),
                "cluster",
                id="cluster_decides",
            ),
            pytest.param(
                _row(LOCAL, _OPEN, 6),
                _pinned(LOCAL, _CLOSED),
                None,
                id="cluster_pin_drops_local",
            ),
            pytest.param(
                _pinned(LOCAL, _CLOSED),
                _row(LOCAL, _OPEN, 9),
                None,
                id="local_pin_drops_cluster",
            ),
            pytest.param(
                _pinned(LOCAL, _OPEN, failure_count=6), None, None, id="local_block"
            ),
            pytest.param(
                _pinned(LOCAL, _OPEN, lapsed=True, failure_count=6),
                None,
                "local",
                id="lapsed_local_pin_counts",
            ),
            pytest.param(
                _row(LOCAL, _OPEN, 6),
                _pinned(LOCAL, _CLOSED, lapsed=True),
                "local",
                id="lapsed_cluster_pin_counts",
            ),
        ],
    )
    def test_tripped_row_picks_the_first_non_closed_row_unless_either_is_pinned(
        self, local_row, cluster_row, expected
    ):
        """State partition: an override in force on either row is not a trip."""
        row = CircuitBreakerService._tripped_row(local_row, cluster_row)

        if expected is None:
            assert row is None
        else:
            assert row is (local_row if expected == "local" else cluster_row)

    @pytest.mark.parametrize(
        ("local_row", "expected"),
        [
            pytest.param(None, False, id="no_row"),
            pytest.param(_row(LOCAL, _CLOSED), False, id="closed"),
            pytest.param(_row(LOCAL, _OPEN, 6), True, id="open"),
            pytest.param(_row(LOCAL, _HALF_OPEN, 6), True, id="half_open"),
            pytest.param(_pinned(LOCAL, _OPEN, failure_count=6), False, id="block"),
            pytest.param(
                _pinned(LOCAL, _OPEN, lapsed=True, failure_count=6),
                True,
                id="lapsed_block",
            ),
        ],
    )
    def test_held_on_own_row_is_a_non_closed_row_under_no_override(
        self, local_row, expected
    ):
        """This process refuses on its own row unless the operator pinned it."""
        assert CircuitBreakerService._held_on_own_row(local_row) is expected

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
