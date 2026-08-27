"""775 — the removed success write is the removed L2 mirror.

The point of skipping ``update_state`` on a CLOSED, zero-failure success is not
the L1 write it saves; it is the ``get_or_create`` + Lua-CAS ``update_state``
pair the layered repository then submits to L2 on every healthy request. This
file asserts that at the seam that actually carries the cost.

The absence assertion is worthless on its own. The mirror runs on a shared
``ThreadPoolExecutor``, so "the L2 double was never called" is also what a test
observes when the executor simply has not run yet. Every claim here is
therefore framed as absence *between* two observed presences, and the executor
is drained (never with ``cancel_futures``, which would cancel exactly the
queued mirror the test is looking for) before the absence is read.

The second class covers the other side of the same removal: a live healthy
service's L2 row stops being re-stamped, so the daily stale-key sweep will
eventually delete it. An absent shared row is not a lost breaker — it
reconstructs as CLOSED.
"""

from __future__ import annotations

import threading
from unittest.mock import MagicMock

import pytest

from baldur.interfaces.repositories import CircuitBreakerStateEnum
from baldur.services.circuit_breaker.config import CircuitBreakerConfig
from baldur.services.circuit_breaker.service import CircuitBreakerService
from baldur.settings.circuit_breaker import get_circuit_breaker_settings

SVC = "payment.charge"

# Bounded so a mirror lane that never runs fails the test instead of hanging
# it. Generous relative to an in-process mock call.
MIRROR_TIMEOUT_SECONDS = 5.0

# Enough healthy successes that a per-success mirror could not plausibly be
# missed by the drain, without making the test slow.
HEALTHY_SUCCESS_COUNT = 25


def _drain_l2_executor() -> None:
    """Wait out every submitted mirror task, then drop the shared pool.

    ``cancel_futures=True`` is deliberately not passed: it would cancel a
    queued mirror task, which is precisely the write this file asserts the
    absence of — the drain would manufacture the result.
    """
    from baldur.adapters.memory.layered_repository.base import LayeredRepositoryBase

    executor = LayeredRepositoryBase._executor
    if executor is not None:
        executor.shutdown(wait=True)
        LayeredRepositoryBase._executor = None


@pytest.fixture
def mirror_event() -> threading.Event:
    """Set from the L2 double the moment a mirror write lands."""
    return threading.Event()


@pytest.fixture
def l2_mock(mirror_event) -> MagicMock:
    from baldur.adapters.memory.circuit_breaker import (
        InMemoryCircuitBreakerStateRepository,
    )

    mock = MagicMock(spec=InMemoryCircuitBreakerStateRepository)
    mock.get_all_states.return_value = []

    def _signal_mirror(*_args, **_kwargs) -> bool:
        mirror_event.set()
        return True

    mock.update_state.side_effect = _signal_mirror
    return mock


@pytest.fixture
def layered_repo(l2_mock):
    from baldur.adapters.memory.circuit_breaker import (
        LayeredCircuitBreakerStateRepository,
    )

    repo = LayeredCircuitBreakerStateRepository(l2_repo=l2_mock, adapter_type="redis")
    # The constructor's initial load already spoke to L2; the claims below are
    # about the record path only.
    l2_mock.reset_mock()
    return repo


@pytest.fixture
def service(layered_repo) -> CircuitBreakerService:
    # Thresholds far above what these tests drive: a trip would write state for
    # its own reasons and confuse the mirror accounting.
    return CircuitBreakerService(
        config=CircuitBreakerConfig(
            enabled=True, failure_threshold=50, minimum_calls=1000
        ),
        repository=layered_repo,
    )


# =============================================================================
# Behavior — healthy successes stop reaching L2
# =============================================================================


class TestLayeredHealthySuccessNoMirrorBehavior:
    """The L2 mirror follows the write, so removing the write removes it."""

    def test_layered_no_mirror_on_healthy_success_between_two_real_ones(
        self, service, layered_repo, l2_mock, mirror_event
    ):
        """Presence, then absence, then presence again — one mirror lane.

        The middle phase is the claim; the two outer phases are what make it
        evidence rather than a race the test happened to win. The same method,
        ``record_success``, produces the mirror in phase 1 and none in phase 2:
        the only difference is whether the row had a failure count to reset.
        """
        # Given: the default-off admission posture. With cluster state
        # propagation on, get_or_create reads L2 and the traffic asserted
        # below would come from the read path rather than the mirror.
        assert get_circuit_breaker_settings().cluster_state_propagation_enabled is False

        # Phase 1 — presence. The failure's own mirror is drained away first,
        # so what the wait below observes is the mirror the *success* produced
        # when it had a count to reset.
        service.record_failure(SVC)
        _drain_l2_executor()
        l2_mock.reset_mock()
        mirror_event.clear()

        service.record_success(SVC)
        assert mirror_event.wait(MIRROR_TIMEOUT_SECONDS), (
            "a success that writes state did not mirror, so the absence "
            "asserted below would prove nothing"
        )
        _drain_l2_executor()
        l2_mock.reset_mock()
        mirror_event.clear()

        # The row is now CLOSED with nothing left to reset.
        assert layered_repo._l1.get_by_service_name(SVC).failure_count == 0

        # Phase 2 — absence. N healthy successes on that row.
        for _ in range(HEALTHY_SUCCESS_COUNT):
            service.record_success(SVC)
        _drain_l2_executor()

        assert l2_mock.method_calls == [], (
            f"L2 saw traffic on a healthy success stream: {l2_mock.method_calls}"
        )

        # Phase 3 — presence again. The lane is dormant, not broken: give the
        # row a count to reset and the next success mirrors as before.
        service.record_failure(SVC)
        _drain_l2_executor()
        l2_mock.reset_mock()
        mirror_event.clear()

        service.record_success(SVC)
        assert mirror_event.wait(MIRROR_TIMEOUT_SECONDS), (
            "the mirror lane did not recover for a success that writes state"
        )
        _drain_l2_executor()
        l2_mock.update_state.assert_called()


# =============================================================================
# Behavior — an absent shared row is not a lost breaker
# =============================================================================


class TestL2RowAbsentReconstructsClosedBehavior:
    """The second ``updated_at`` consumer: the daily stale-key sweep.

    A live healthy service's row no longer advances its stamp, so the sweep
    that deletes rows past the retention window will eventually take it. Every
    outcome of that deletion has to be benign, and this is the one that matters
    for a booting worker.
    """

    def test_l2_row_absent_reconstructs_closed_after_the_stale_key_sweep(self):
        from baldur.adapters.memory.circuit_breaker import (
            InMemoryCircuitBreakerStateRepository,
            LayeredCircuitBreakerStateRepository,
        )

        # Given: the shared store holds a healthy service's CLOSED row.
        l2 = InMemoryCircuitBreakerStateRepository()
        l2.get_or_create(SVC)

        # Control: while the row is there, a booting worker hydrates it — so
        # the absence below is the deletion's doing, not a dead load path.
        with_row = LayeredCircuitBreakerStateRepository(
            l2_repo=l2, adapter_type="redis"
        )
        assert with_row._l1.get_by_service_name(SVC) is not None
        _drain_l2_executor()

        # When: the sweep deletes the row, and another worker boots.
        assert l2.delete_state(SVC) is True
        booted = LayeredCircuitBreakerStateRepository(l2_repo=l2, adapter_type="redis")

        # Then: nothing hydrated for this service ...
        assert booted._l1.get_by_service_name(SVC) is None

        # ... and the first request reconstructs it locally as a clean CLOSED
        # breaker rather than failing or inheriting a stale verdict.
        row = booted.get_or_create(SVC)
        assert row.state == CircuitBreakerStateEnum.CLOSED.value
        assert row.failure_count == 0
        assert row.manually_controlled is False
        assert booted._l1.get_by_service_name(SVC) is not None
