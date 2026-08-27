"""A CLOSED success writes state only when there is a failure count to reset.

775 D1/D2. ``record_success`` used to write the row on every CLOSED success.
The write's only semantic is the consecutive-failure reset: every other field
it supplies already equals the stored value, and the ones it omits resolve back
to the stored value in all three adapters. On a row that is already CLOSED with
``failure_count == 0`` it therefore changed nothing but the ``updated_at``
stamp — while costing one repository write and, on the layered repository, a
two-round-trip L2 mirror per healthy request.

Covered here:

- the exit table over row shapes, including the negative assertion that the
  CLOSED branch no longer calls ``update_state`` unconditionally;
- the reset semantics the write does carry, against a real in-memory
  repository rather than a double — a mock cannot show that a failure followed
  by a success still leaves the count at zero;
- the two halves of the read-then-write window, driven by a deterministic seam
  rather than by threads. A trip landing between the read and the write now
  survives when the snapshot carried no failures, and is still erased when it
  carried some. The second case pins behaviour this change does **not** fix,
  so the next reader does not mistake it for a regression.
"""

from __future__ import annotations

import dataclasses
from unittest.mock import MagicMock, patch

import pytest

from baldur.adapters.memory.circuit_breaker import (
    InMemoryCircuitBreakerStateRepository,
)
from baldur.interfaces.repositories import (
    CircuitBreakerCloseAttempt,
    CircuitBreakerStateData,
    CircuitBreakerStateEnum,
    CircuitBreakerStateRepository,
)
from baldur.services.circuit_breaker.config import CircuitBreakerConfig
from baldur.services.circuit_breaker.service import CircuitBreakerService

SERVICE = "payment-api"

# Every repository method ``record_success`` could reach that mutates state.
# The exit table asserts the whole set, not only the one it expects to fire:
# a branch that moved its write to a sibling primitive would otherwise read as
# "no write".
WRITE_METHODS = (
    "update_state",
    "record_success",
    "record_success_with_close_check",
    "record_failure",
    "trip_to_open",
)


def _row(**overrides) -> CircuitBreakerStateData:
    """A state row as ``get_or_create`` would return it."""
    return CircuitBreakerStateData(service_name=SERVICE, **overrides)


@pytest.fixture
def repo() -> MagicMock:
    """Repository double carrying the interface's real signatures."""
    mock = MagicMock(spec=CircuitBreakerStateRepository)
    mock.update_state.return_value = True
    # A spec'd mock would otherwise answer ``attempt.did_close`` with a truthy
    # mock and drive the auto-close side effects on every HALF_OPEN case.
    mock.record_success_with_close_check.return_value = CircuitBreakerCloseAttempt(
        state=_row(state=CircuitBreakerStateEnum.HALF_OPEN.value),
        did_close=False,
    )
    return mock


@pytest.fixture
def service(repo) -> CircuitBreakerService:
    return CircuitBreakerService(
        config=CircuitBreakerConfig(enabled=True, failure_threshold=5),
        repository=repo,
    )


@pytest.fixture
def real_repo() -> InMemoryCircuitBreakerStateRepository:
    """The default L1 adapter, used where the claim is about stored rows."""
    return InMemoryCircuitBreakerStateRepository()


@pytest.fixture
def real_service(real_repo) -> CircuitBreakerService:
    # minimum_calls above the call counts these tests drive keeps the rate
    # trigger out of the picture, so a trip (or its absence) is attributable
    # to the consecutive-failure rule alone.
    return CircuitBreakerService(
        config=CircuitBreakerConfig(
            enabled=True, failure_threshold=5, minimum_calls=10
        ),
        repository=real_repo,
    )


# =============================================================================
# Behavior — the exit table over row shapes
# =============================================================================


class TestRecordSuccessExitBehavior:
    """Which repository write each ``record_success`` exit performs, if any."""

    @pytest.mark.parametrize(
        ("row_kwargs", "expected_write"),
        [
            pytest.param(
                {"state": CircuitBreakerStateEnum.CLOSED.value, "failure_count": 0},
                None,
                id="e6_closed_zero_failures_performs_no_write",
            ),
            pytest.param(
                {"state": CircuitBreakerStateEnum.CLOSED.value, "failure_count": 3},
                "update_state",
                id="e5_closed_with_failures_writes_the_reset",
            ),
            pytest.param(
                {"state": CircuitBreakerStateEnum.HALF_OPEN.value},
                "record_success_with_close_check",
                id="e4_half_open_delegates_the_close_check",
            ),
            pytest.param(
                {"state": CircuitBreakerStateEnum.OPEN.value},
                None,
                id="e7_open_row_performs_no_write",
            ),
            pytest.param(
                {
                    "state": CircuitBreakerStateEnum.CLOSED.value,
                    "failure_count": 3,
                    "manually_controlled": True,
                },
                None,
                id="e3_pinned_row_performs_no_write",
            ),
        ],
    )
    def test_record_success_exit_performs_only_the_write_its_row_shape_earns(
        self, service, repo, row_kwargs, expected_write
    ):
        """One ``record_success`` per prepared row; assert the whole write set.

        The E6 row is the negative assertion this change exists for: before the
        guard it drove ``update_state`` exactly as the E5 row does. The E3 row
        carries failures too, so the pin — not an empty count — is what stops
        its write.
        """
        # Given: the fresh read returns a row of this shape.
        repo.get_or_create.return_value = _row(**row_kwargs)

        # When
        service.record_success(SERVICE)

        # Then: exactly the expected write happened, and no other.
        for name in WRITE_METHODS:
            method = getattr(repo, name)
            if name == expected_write:
                assert method.call_count == 1, f"{name} should have been called once"
            else:
                assert method.call_count == 0, f"{name} should not have been called"

    def test_record_success_exit_e5_forwards_the_consecutive_failure_reset(
        self, service, repo
    ):
        """The surviving write's arguments are the reset, and nothing else."""
        repo.get_or_create.return_value = _row(
            state=CircuitBreakerStateEnum.CLOSED.value, failure_count=3
        )

        service.record_success(SERVICE)

        repo.update_state.assert_called_once_with(
            service_name=SERVICE,
            state=CircuitBreakerStateEnum.CLOSED.value,
            failure_count=0,
        )

    def test_record_success_exit_e4_forwards_the_configured_success_threshold(
        self, repo
    ):
        """The HALF_OPEN delegation is unchanged — assert what it forwards."""
        service = CircuitBreakerService(
            config=CircuitBreakerConfig(enabled=True, success_threshold=4),
            repository=repo,
        )
        repo.get_or_create.return_value = _row(
            state=CircuitBreakerStateEnum.HALF_OPEN.value
        )

        service.record_success(SERVICE)

        repo.record_success_with_close_check.assert_called_once_with(SERVICE, 4)

    def test_record_success_exit_e1_disabled_breaker_touches_no_repository_method(
        self, repo
    ):
        """E1 returns before the read, so not even ``get_or_create`` fires."""
        service = CircuitBreakerService(
            config=CircuitBreakerConfig(enabled=False),
            repository=repo,
        )

        service.record_success(SERVICE)

        assert repo.method_calls == []


# =============================================================================
# Behavior — the semantics the surviving write carries
# =============================================================================


class TestClosedSuccessResetSemanticsBehavior:
    """The consecutive-failure reset, against the real in-memory repository."""

    def test_reset_semantics_success_after_failure_clears_the_stored_count(
        self, real_service, real_repo
    ):
        # Given: one recorded failure.
        real_service.record_failure(SERVICE)
        assert real_repo.get_by_service_name(SERVICE).failure_count == 1

        # When
        real_service.record_success(SERVICE)

        # Then: the count the trip trigger reads is back to zero.
        assert real_repo.get_by_service_name(SERVICE).failure_count == 0

    def test_reset_semantics_second_consecutive_success_performs_no_write(
        self, real_service, real_repo
    ):
        """Absence after presence: the same call writes, then stops writing.

        A bare "no write happened" assertion would also pass against a
        ``record_success`` that had stopped working altogether, so the first
        success is the control.
        """
        real_service.record_failure(SERVICE)

        with patch.object(
            real_repo, "update_state", wraps=real_repo.update_state
        ) as spy:
            # When: the first success has a count to reset.
            real_service.record_success(SERVICE)
            assert spy.call_count == 1

            # And: the second has none.
            real_service.record_success(SERVICE)

            # Then: the row was written once, not twice.
            assert spy.call_count == 1

    def test_reset_semantics_alternating_failures_never_reach_the_threshold(
        self, real_service, real_repo
    ):
        """F,S,F,S,F at ``failure_threshold=5`` stays CLOSED.

        The count means "failures since the last success", so five recorded
        failures interleaved with successes are not five consecutive ones.
        """
        for failed in (True, False, True, False, True):
            if failed:
                real_service.record_failure(SERVICE)
            else:
                real_service.record_success(SERVICE)

        stored = real_repo.get_by_service_name(SERVICE)
        assert stored.state == CircuitBreakerStateEnum.CLOSED.value
        assert stored.failure_count == 1

    def test_reset_semantics_control_five_consecutive_failures_still_trip(
        self, real_service, real_repo
    ):
        """The control for the test above: the threshold is reachable.

        Without it, "did not trip" would also be what a breaker that can never
        trip at all produces.
        """
        for _ in range(5):
            real_service.record_failure(SERVICE)

        assert (
            real_repo.get_by_service_name(SERVICE).state
            == CircuitBreakerStateEnum.OPEN.value
        )


# =============================================================================
# Behavior — a trip landing between the read and the write
# =============================================================================


class TestTripInterleavedWithSuccessBehavior:
    """The read-then-write window, placed deterministically rather than raced.

    A ``threading.Barrier`` can synchronise call *entry*, not a point inside
    the method, so it cannot put a trip between ``record_success``'s fresh read
    and its write. Wrapping ``get_or_create`` can: the seam trips the real
    repository and hands back the pre-trip snapshot, which is exactly the state
    a descheduled thread would resume with.
    """

    @staticmethod
    def _arm_trip_seam(
        repo: InMemoryCircuitBreakerStateRepository, trip_failure_count: int
    ) -> list[bool]:
        """Trip the store on the next read and return the pre-trip snapshot.

        Returns the fired-flag list so the test can assert the interleave
        actually happened — a seam that silently never runs would leave both
        cases asserting the uninteresting outcome.
        """
        original = repo.get_or_create
        fired: list[bool] = []

        def _seam(service_name: str) -> CircuitBreakerStateData:
            row = original(service_name)
            if fired:
                return row
            fired.append(True)
            snapshot = dataclasses.replace(row)
            repo.trip_to_open(service_name, trip_failure_count)
            return snapshot

        repo.get_or_create = _seam
        return fired

    def test_trip_interleaved_with_success_zero_count_snapshot_leaves_it_standing(
        self, real_service, real_repo
    ):
        """The gain: a healthy-traffic success no longer erases a real trip."""
        # Given: a CLOSED row with nothing to reset.
        real_repo.get_or_create(SERVICE)
        fired = self._arm_trip_seam(real_repo, trip_failure_count=5)

        # When: the breaker trips between this call's read and its write.
        real_service.record_success(SERVICE)

        # Then: the interleave happened, and the trip survived it.
        assert fired == [True]
        assert (
            real_repo.get_by_service_name(SERVICE).state
            == CircuitBreakerStateEnum.OPEN.value
        )

    def test_trip_interleaved_with_success_nonzero_count_snapshot_erases_it(
        self, real_service, real_repo
    ):
        """The residue: the surviving write still has no state precondition.

        Pinned deliberately. ``update_state`` is called with ``state="closed"``
        and no precondition, so a snapshot carrying failures overwrites a trip
        committed in between — the half of the window this guard does not
        reach. Fixing it means a state-preserving reset at the repository
        contract, which is a separate change; if that lands, this test is the
        one that should go red.
        """
        # Given: a CLOSED row that does have a count to reset.
        real_repo.get_or_create(SERVICE)
        for _ in range(3):
            real_repo.record_failure(SERVICE)
        fired = self._arm_trip_seam(real_repo, trip_failure_count=5)

        # When
        real_service.record_success(SERVICE)

        # Then: the trip was overwritten by the reset write.
        assert fired == [True]
        stored = real_repo.get_by_service_name(SERVICE)
        assert stored.state == CircuitBreakerStateEnum.CLOSED.value
        assert stored.failure_count == 0
