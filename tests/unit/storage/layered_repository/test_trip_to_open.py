"""L2-authoritative routing of the CLOSED->OPEN automatic trip.

The trip used to be an ordinary ``update_state``: an L1 write plus a
fire-and-forget mirror racing the five failure records that produced it, so a
genuine trip could be erased from the shared store by its own record path.
Routing the state write to L2's atomic primitive makes the durable row the
outcome of one single-winner decision and the local row a writeback of it.

Branches covered here, mirroring the shipped open-check router:

- ``state='open'``: writeback L1 to OPEN carrying the store's ``opened_at``
  (winner and race-loser alike), then nudge the mirror.
- ``state='half_open'``: a peer tripped and the cluster progressed to recovery
  testing — join the trial regime rather than clobber the store back to OPEN.
- ``state='pinned'``: an override in force declined the write; the remote row
  is delivered whole and the nudge is deliberately skipped.
- anything else, plus L2 timeout / exception / unhealthy / absent: the
  degraded-mode counter and an L1 fallback, where the guarantees relax to
  per-worker best-effort.
"""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from datetime import timedelta
from unittest.mock import MagicMock, patch

import pytest
from structlog.testing import capture_logs

from baldur.interfaces.repositories import (
    CIRCUIT_BREAKER_PINNED_TOKEN,
    CircuitBreakerOpenAttempt,
    CircuitBreakerStateData,
    CircuitBreakerStateEnum,
    pinned_trip_attempt,
)
from baldur.utils.time import utc_now

SVC = "svc"
FAILURE_COUNT = 5


def _attempt(state: str, *, did_open: bool, opened_at=None):
    """Build a minimal attempt mirroring what an L2 adapter returns."""
    state_data = CircuitBreakerStateData(
        service_name=SVC,
        id=None,
        state=state,
        failure_count=0,
        success_count=0,
        last_failure_at=None,
        opened_at=opened_at,
        manually_controlled=False,
        controlled_by_id=None,
        control_reason="",
        manual_override_expires_at=None,
        half_open_request_count=0,
        half_open_window_started_at=None,
        metadata={},
        created_at=None,
        updated_at=None,
    )
    return CircuitBreakerOpenAttempt(state=state_data, did_open=did_open)


@pytest.fixture
def l2_mock():
    from baldur.adapters.memory.circuit_breaker import (
        InMemoryCircuitBreakerStateRepository,
    )

    mock = MagicMock(spec=InMemoryCircuitBreakerStateRepository)
    mock.get_all_states.return_value = []
    return mock


@pytest.fixture
def repo(l2_mock):
    from baldur.adapters.memory.circuit_breaker import (
        LayeredCircuitBreakerStateRepository,
    )

    return LayeredCircuitBreakerStateRepository(l2_repo=l2_mock, adapter_type="redis")


def _prime_l1_closed(repo, failure_count: int = FAILURE_COUNT) -> None:
    """L1 in the state a tripping worker's record path leaves behind."""
    repo._l1.get_or_create(SVC)
    for _ in range(failure_count):
        repo._l1.record_failure(SVC)


# =============================================================================
# Behavior — L2-authoritative routing + L1 writeback
# =============================================================================


class TestLayeredTripRoutingBehavior:
    """L2 decides; L1 mirrors the decision."""

    def test_l2_winner_is_returned_and_written_back_to_l1(self, repo, l2_mock):
        _prime_l1_closed(repo)
        opened = utc_now()
        l2_mock.trip_to_open.return_value = _attempt(
            "open", did_open=True, opened_at=opened
        )

        with patch.object(repo, "_sync_to_l2_async"):
            attempt = repo.trip_to_open(SVC, FAILURE_COUNT)

        assert attempt.did_open is True
        assert attempt.state.state == "open"
        l2_mock.trip_to_open.assert_called_once_with(SVC, FAILURE_COUNT)
        l1_state = repo._l1.get_by_service_name(SVC)
        assert l1_state.state == "open"
        assert l1_state.opened_at == opened

    def test_l1_writeback_leaves_the_record_paths_failure_count_alone(
        self, repo, l2_mock
    ):
        # The count this trip reports was written by the record_failure that
        # preceded it; the writeback must not zero it.
        _prime_l1_closed(repo)
        l2_mock.trip_to_open.return_value = _attempt(
            "open", did_open=True, opened_at=utc_now()
        )

        with patch.object(repo, "_sync_to_l2_async"):
            repo.trip_to_open(SVC, FAILURE_COUNT)

        assert repo._l1.get_by_service_name(SVC).failure_count == FAILURE_COUNT

    def test_race_loser_still_converges_l1_to_open(self, repo, l2_mock):
        # A peer won the transition. This worker emits nothing, but its local
        # row must still reject traffic — otherwise the loser readmits.
        _prime_l1_closed(repo)
        opened = utc_now() - timedelta(seconds=5)
        l2_mock.trip_to_open.return_value = _attempt(
            "open", did_open=False, opened_at=opened
        )

        with patch.object(repo, "_sync_to_l2_async"):
            attempt = repo.trip_to_open(SVC, FAILURE_COUNT)

        assert attempt.did_open is False
        l1_state = repo._l1.get_by_service_name(SVC)
        assert l1_state.state == "open"
        assert l1_state.opened_at == opened

    def test_half_open_joins_the_trial_regime_instead_of_clobbering(
        self, repo, l2_mock
    ):
        # Recency over restrictiveness: clobbering back to OPEN would revert a
        # legitimate OPEN->HALF_OPEN transition a peer already made.
        _prime_l1_closed(repo)
        repo._l1.record_success(SVC)
        l2_mock.trip_to_open.return_value = _attempt("half_open", did_open=False)

        with patch.object(repo, "_sync_to_l2_async"):
            attempt = repo.trip_to_open(SVC, FAILURE_COUNT)

        assert attempt.did_open is False
        l1_state = repo._l1.get_by_service_name(SVC)
        assert l1_state.state == "half_open"
        assert l1_state.success_count == 0

    def test_every_written_back_branch_nudges_the_mirror_after_the_writeback(
        self, repo, l2_mock
    ):
        # The nudge is what makes an in-flight stale mirror re-run against
        # post-trip L1. Issued before the writeback it would re-run against
        # the pre-trip row and lose the ordering argument entirely.
        _prime_l1_closed(repo)
        l2_mock.trip_to_open.return_value = _attempt(
            "open", did_open=True, opened_at=utc_now()
        )
        observed_states: list[str] = []

        def _record_state(service_name):
            observed_states.append(repo._l1.get_by_service_name(service_name).state)

        with patch.object(repo, "_sync_to_l2_async", side_effect=_record_state):
            repo.trip_to_open(SVC, FAILURE_COUNT)

        assert observed_states == ["open"]

    @pytest.mark.parametrize("returned_state", ["closed", "missing", "corrupted"])
    def test_unrecognized_l2_state_falls_back_to_l1_with_the_degraded_counter(
        self, repo, l2_mock, returned_state
    ):
        _prime_l1_closed(repo)
        l2_mock.trip_to_open.return_value = _attempt(returned_state, did_open=False)

        with (
            patch.object(repo, "_record_trip_degraded_mode") as mock_degraded,
            patch.object(repo, "_sync_to_l2_async"),
        ):
            attempt = repo.trip_to_open(SVC, FAILURE_COUNT)

        # Protection is kept locally when the store cannot answer usefully.
        assert attempt.did_open is True
        assert attempt.state.state == "open"
        assert repo._l1.get_by_service_name(SVC).state == "open"
        mock_degraded.assert_called_once_with(SVC)


# =============================================================================
# Behavior — degraded fall-through: timeout, exception, unhealthy, absent L2
# =============================================================================


class TestLayeredTripDegradedModeBehavior:
    """L2 unavailable -> degraded-mode counter + L1 fallback + mirror nudge."""

    def test_l2_timeout_falls_back_to_l1_with_the_degraded_counter(self, repo):
        _prime_l1_closed(repo)
        fake_future = MagicMock(spec=Future)
        fake_future.result.side_effect = FuturesTimeoutError()
        fake_executor = MagicMock(spec=ThreadPoolExecutor)
        fake_executor.submit.return_value = fake_future

        with (
            patch.object(repo, "_get_executor", return_value=fake_executor),
            patch.object(repo, "_record_trip_degraded_mode") as mock_degraded,
            patch.object(repo, "_sync_to_l2_async") as mock_sync,
        ):
            attempt = repo.trip_to_open(SVC, FAILURE_COUNT)

        assert attempt.did_open is True
        assert attempt.state.state == "open"
        mock_degraded.assert_called_once_with(SVC)
        mock_sync.assert_called_once_with(SVC)

    def test_l2_exception_falls_back_to_l1_with_the_degraded_counter(self, repo):
        _prime_l1_closed(repo)
        fake_future = MagicMock(spec=Future)
        fake_future.result.side_effect = ConnectionError("redis down")
        fake_executor = MagicMock(spec=ThreadPoolExecutor)
        fake_executor.submit.return_value = fake_future

        with (
            patch.object(repo, "_get_executor", return_value=fake_executor),
            patch.object(repo, "_record_trip_degraded_mode") as mock_degraded,
            patch.object(repo, "_sync_to_l2_async"),
        ):
            attempt = repo.trip_to_open(SVC, FAILURE_COUNT)

        assert attempt.did_open is True
        assert repo._l1.get_by_service_name(SVC).state == "open"
        mock_degraded.assert_called_once_with(SVC)

    def test_quarantined_l2_is_never_asked(self, repo, l2_mock):
        _prime_l1_closed(repo)
        repo._l2_healthy = False

        with (
            patch.object(repo, "_record_trip_degraded_mode") as mock_degraded,
            patch.object(repo, "_sync_to_l2_async"),
        ):
            attempt = repo.trip_to_open(SVC, FAILURE_COUNT)

        l2_mock.trip_to_open.assert_not_called()
        mock_degraded.assert_called_once_with(SVC)
        assert attempt.did_open is True

    def test_absent_l2_uses_the_l1_path_without_the_executor(self):
        from baldur.adapters.memory.circuit_breaker import (
            LayeredCircuitBreakerStateRepository,
        )

        repo = LayeredCircuitBreakerStateRepository(l2_repo=None)
        _prime_l1_closed(repo)

        with (
            patch.object(repo, "_get_executor") as mock_get_executor,
            patch.object(repo, "_record_trip_degraded_mode") as mock_degraded,
            patch.object(repo, "_sync_to_l2_async"),
        ):
            attempt = repo.trip_to_open(SVC, FAILURE_COUNT)

        mock_get_executor.assert_not_called()
        mock_degraded.assert_called_once_with(SVC)
        assert attempt.did_open is True


# =============================================================================
# Behavior — the declined-by-pin branch
# =============================================================================


class TestPinnedTripHydrationBehavior:
    """A store row an operator pinned is delivered whole, never written over."""

    def test_pinned_row_is_hydrated_into_l1_with_its_pin_fields(self, repo, l2_mock):
        # Given: the store declined the trip; the remote row carries an Allow.
        _prime_l1_closed(repo)
        expires_at = utc_now() + timedelta(minutes=10)
        l2_mock.trip_to_open.return_value = pinned_trip_attempt(SVC, expires_at)
        remote = CircuitBreakerStateData(
            service_name=SVC,
            state=CircuitBreakerStateEnum.CLOSED.value,
            manually_controlled=True,
            controlled_by_id=7,
            control_reason="deploy window",
            manual_override_expires_at=expires_at,
        )
        l2_mock.get_by_service_name.return_value = remote

        attempt = repo.trip_to_open(SVC, FAILURE_COUNT)

        # Then: the worker enforces the operator's decision from its next
        # request — a per-field state copy would leave it unpinned and free to
        # re-trip and mirror an OPEN over the override.
        assert attempt.did_open is False
        assert attempt.state.state == CIRCUIT_BREAKER_PINNED_TOKEN
        assert attempt.state.manual_override_expires_at == expires_at
        l1_state = repo._l1.get_by_service_name(SVC)
        assert l1_state.manually_controlled is True
        assert l1_state.manual_override_expires_at == expires_at
        assert l1_state.state == CircuitBreakerStateEnum.CLOSED.value

    def test_pinned_branch_does_not_nudge_the_mirror(self, repo, l2_mock):
        # Mirroring is exactly what the override forbids: the nudged write
        # would carry this worker's own state to the pinned store row.
        _prime_l1_closed(repo)
        l2_mock.trip_to_open.return_value = pinned_trip_attempt(SVC, None)
        l2_mock.get_by_service_name.return_value = CircuitBreakerStateData(
            service_name=SVC,
            state=CircuitBreakerStateEnum.CLOSED.value,
            manually_controlled=True,
        )

        with patch.object(repo, "_sync_to_l2_async") as mock_sync:
            repo.trip_to_open(SVC, FAILURE_COUNT)

        mock_sync.assert_not_called()

    def test_pinned_branch_never_opens_l1(self, repo, l2_mock):
        # The inversion this branch exists to prevent: a local OPEN written
        # here would land over the operator's Allow.
        _prime_l1_closed(repo)
        l2_mock.trip_to_open.return_value = pinned_trip_attempt(
            SVC, utc_now() + timedelta(minutes=10)
        )
        l2_mock.get_by_service_name.return_value = CircuitBreakerStateData(
            service_name=SVC,
            state=CircuitBreakerStateEnum.CLOSED.value,
            manually_controlled=True,
            manual_override_expires_at=utc_now() + timedelta(minutes=10),
        )

        repo.trip_to_open(SVC, FAILURE_COUNT)

        assert repo._l1.get_by_service_name(SVC).state != "open"

    def test_vanished_remote_row_warns_and_still_declines(self, repo, l2_mock):
        # The override was lifted concurrently. The verdict stands — falling
        # into the L1 fallback here is what would write OPEN over an Allow
        # that may still be in force on another worker's view.
        _prime_l1_closed(repo)
        expires_at = utc_now() + timedelta(minutes=10)
        l2_mock.trip_to_open.return_value = pinned_trip_attempt(SVC, expires_at)
        l2_mock.get_by_service_name.return_value = None

        with capture_logs() as caplog:
            attempt = repo.trip_to_open(SVC, FAILURE_COUNT)

        assert attempt.did_open is False
        assert attempt.state.state == CIRCUIT_BREAKER_PINNED_TOKEN
        assert attempt.state.manual_override_expires_at == expires_at
        assert repo._l1.get_by_service_name(SVC).state != "open"
        assert any(
            entry.get("event") == "circuit_breaker.trip_pin_hydration_skipped"
            and entry.get("log_level") == "warning"
            for entry in caplog
        )

    def test_failed_hydration_warns_and_still_declines(self, repo, l2_mock):
        _prime_l1_closed(repo)
        expires_at = utc_now() + timedelta(minutes=10)
        l2_mock.trip_to_open.return_value = pinned_trip_attempt(SVC, expires_at)
        l2_mock.get_by_service_name.side_effect = ConnectionError("redis down")

        with capture_logs() as caplog:
            attempt = repo.trip_to_open(SVC, FAILURE_COUNT)

        assert attempt.did_open is False
        assert attempt.state.state == CIRCUIT_BREAKER_PINNED_TOKEN
        assert repo._l1.get_by_service_name(SVC).state != "open"
        assert any(
            entry.get("event") == "circuit_breaker.trip_pin_hydration_failed"
            and entry.get("log_level") == "warning"
            for entry in caplog
        )

    def test_local_pin_is_not_overwritten_by_the_hydration(self, repo, l2_mock):
        # This worker already holds an override of its own; the delivery must
        # not replace it with the remote's.
        repo._l1.get_or_create(SVC)
        local_expires = utc_now() + timedelta(hours=1)
        repo._l1.set_manual_control(
            SVC,
            CircuitBreakerStateEnum.OPEN.value,
            reason="local block",
            expires_at=local_expires,
        )
        l2_mock.trip_to_open.return_value = pinned_trip_attempt(SVC, None)
        l2_mock.get_by_service_name.return_value = CircuitBreakerStateData(
            service_name=SVC,
            state=CircuitBreakerStateEnum.CLOSED.value,
            manually_controlled=True,
            control_reason="remote allow",
        )

        repo.trip_to_open(SVC, FAILURE_COUNT)

        l1_state = repo._l1.get_by_service_name(SVC)
        assert l1_state.control_reason == "local block"
        assert l1_state.manual_override_expires_at == local_expires
