"""656 D7 — LayeredCircuitBreakerStateRepository.record_failure_with_open_check.

The L2-authoritative routing of the atomic open-check so the cross-process
exactly-one ``CIRCUIT_BREAKER_OPENED`` contract holds across gunicorn
workers / K8s replicas. Symmetric mirror of the close-check router, with
the open-check's own terminal-state set:

- ``state='open'``: writeback L1 to OPEN carrying ``opened_at`` (covers
  both the ``did_open=True`` winner and the ``did_open=False`` race-loser).
- ``state='closed'``: trust L2 — a concurrent quorum of HALF_OPEN successes
  closed the cluster while this worker's trial failed; writeback L1 to
  CLOSED, ``did_open=False``, no re-open.
- ``state in {missing, other}``: stale relative to the caller's HALF_OPEN
  view; record degraded-mode and fall back to L1's atomic re-open path.
- L2 timeout / generic exception / ``_l2_healthy=False`` / ``_l2 is None``:
  degraded-mode + L1 fallback + async L2 sync.
"""

from __future__ import annotations

from concurrent.futures import TimeoutError as FuturesTimeoutError
from unittest.mock import MagicMock, patch

import pytest

from baldur.interfaces.repositories import (
    CircuitBreakerOpenAttempt,
    CircuitBreakerStateData,
    CircuitBreakerStateEnum,
)
from baldur.utils.time import utc_now


def _attempt(state: str, *, did_open: bool, opened_at=None):
    """Build a minimal CircuitBreakerOpenAttempt mirroring the Redis Lua return."""
    state_data = CircuitBreakerStateData(
        service_name="svc",
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
    """Mock L2 with the same spec the production wiring uses."""
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

    return LayeredCircuitBreakerStateRepository(
        l2_repo=l2_mock,
        adapter_type="redis",
    )


def _prime_l1_half_open(repo, service_name="svc"):
    repo._l1.get_or_create(service_name)
    repo._l1.update_state(
        service_name=service_name,
        state=CircuitBreakerStateEnum.HALF_OPEN.value,
    )


# =============================================================================
# Behavior — L2-authoritative routing + L1 writeback (D7 step 2/3)
# =============================================================================


class TestLayeredOpenCheckRoutingBehavior:
    """L2 is the source of truth; L1 mirrors L2's post-state."""

    def test_l2_open_winner_returned_and_l1_writeback_open(self, repo, l2_mock):
        # Given: L1 in HALF_OPEN; L2 reports the re-open winner with opened_at.
        _prime_l1_half_open(repo)
        opened = utc_now()
        l2_mock.record_failure_with_open_check.return_value = _attempt(
            "open", did_open=True, opened_at=opened
        )

        attempt = repo.record_failure_with_open_check("svc")

        assert attempt.did_open is True
        assert attempt.state.state == "open"
        l2_mock.record_failure_with_open_check.assert_called_once_with("svc")

        # L1 writeback reflects the L2-authoritative OPEN with opened_at carried.
        l1_state = repo._l1.get_by_service_name("svc")
        assert l1_state.state == "open"
        assert l1_state.opened_at == opened

    def test_l2_open_race_loser_also_writes_back_l1_open(self, repo, l2_mock):
        # Given: L1 HALF_OPEN; L2 reports a no-write race-loser (did_open=False,
        # state=open) carrying the existing opened_at.
        _prime_l1_half_open(repo)
        opened = utc_now()
        l2_mock.record_failure_with_open_check.return_value = _attempt(
            "open", did_open=False, opened_at=opened
        )

        attempt = repo.record_failure_with_open_check("svc")

        # The race-loser still converges L1 to OPEN (so admission cuts traffic).
        assert attempt.did_open is False
        l1_state = repo._l1.get_by_service_name("svc")
        assert l1_state.state == "open"
        assert l1_state.opened_at == opened

    def test_l2_closed_trust_l2_no_re_open_and_l1_writeback_closed(self, repo, l2_mock):
        # Given: L1 HALF_OPEN; L2 reports CLOSED — a concurrent quorum of
        # HALF_OPEN successes closed the cluster while this trial failed.
        _prime_l1_half_open(repo)
        # Prime an L1 failure count so the closed writeback's reset is visible.
        repo._l1.record_failure("svc")
        l2_mock.record_failure_with_open_check.return_value = _attempt(
            "closed", did_open=False
        )

        attempt = repo.record_failure_with_open_check("svc")

        # Trust-L2 convergence: no re-open, L1 written back to CLOSED.
        assert attempt.did_open is False
        assert attempt.state.state == "closed"
        l1_state = repo._l1.get_by_service_name("svc")
        assert l1_state.state == "closed"
        assert l1_state.failure_count == 0

    @pytest.mark.parametrize("stale_state", ["missing", "unknown"])
    def test_stale_l2_state_falls_back_to_l1_without_writeback(
        self, repo, l2_mock, stale_state
    ):
        # Given: L1 sees HALF_OPEN (a prior try_acquire took the L1-fallback
        # path so L2 never saw the OPEN->HALF_OPEN transition); L2 returns a
        # sentinel not in {open, closed}.
        _prime_l1_half_open(repo)
        l2_mock.record_failure_with_open_check.return_value = _attempt(
            stale_state, did_open=False
        )

        with patch.object(repo, "_record_open_check_degraded_mode") as mock_degraded:
            attempt = repo.record_failure_with_open_check("svc")

        # The wrapper falls back to L1's atomic re-open path; the returned
        # attempt is the L1 decision (HALF_OPEN -> OPEN, did_open=True).
        assert attempt.did_open is True
        assert attempt.state.state == "open"

        # Stale-L2 guard increments the degraded-mode counter.
        mock_degraded.assert_called_once_with("svc")


# =============================================================================
# Behavior — _writeback_open_check_to_l1 branches
# =============================================================================


class TestLayeredOpenCheckWritebackBehavior:
    """``_writeback_open_check_to_l1`` open vs closed branches (D7)."""

    def test_writeback_open_sets_l1_open_with_opened_at(self, repo):
        repo._l1.get_or_create("svc")
        opened = utc_now()

        repo._writeback_open_check_to_l1(
            "svc", _attempt("open", did_open=True, opened_at=opened)
        )

        l1_state = repo._l1.get_by_service_name("svc")
        assert l1_state.state == "open"
        assert l1_state.opened_at == opened

    def test_writeback_closed_resets_counters_and_sets_l1_closed(self, repo):
        repo._l1.get_or_create("svc")
        repo._l1.update_state(
            service_name="svc",
            state=CircuitBreakerStateEnum.OPEN.value,
            opened_at=utc_now(),
        )
        for _ in range(3):
            repo._l1.record_failure("svc")
        assert repo._l1.get_by_service_name("svc").failure_count == 3

        repo._writeback_open_check_to_l1("svc", _attempt("closed", did_open=False))

        l1_state = repo._l1.get_by_service_name("svc")
        assert l1_state.state == "closed"
        assert l1_state.failure_count == 0


# =============================================================================
# Behavior — degraded-mode fall-through: timeout, exception, unhealthy, None L2
# =============================================================================


class TestLayeredOpenCheckDegradedMode:
    """L2 unavailable -> L1 fallback + degraded-mode counter + async L2 sync."""

    def test_l2_timeout_falls_back_to_l1_with_degraded_counter(self, repo, l2_mock):
        _prime_l1_half_open(repo)

        fake_future = MagicMock()
        fake_future.result.side_effect = FuturesTimeoutError()
        fake_executor = MagicMock()
        fake_executor.submit.return_value = fake_future

        with (
            patch.object(repo, "_get_executor", return_value=fake_executor),
            patch.object(repo, "_record_open_check_degraded_mode") as mock_degraded,
            patch.object(repo, "_sync_to_l2_async") as mock_sync,
        ):
            attempt = repo.record_failure_with_open_check("svc")

        # L1-decided attempt: HALF_OPEN -> OPEN.
        assert attempt.did_open is True
        assert attempt.state.state == "open"
        mock_degraded.assert_called_once_with("svc")
        mock_sync.assert_called_once_with("svc", attempt.state)

    def test_l2_generic_exception_falls_back_to_l1_with_degraded_counter(
        self, repo, l2_mock
    ):
        _prime_l1_half_open(repo)

        fake_future = MagicMock()
        fake_future.result.side_effect = ConnectionError("redis down")
        fake_executor = MagicMock()
        fake_executor.submit.return_value = fake_future

        with (
            patch.object(repo, "_get_executor", return_value=fake_executor),
            patch.object(repo, "_record_open_check_degraded_mode") as mock_degraded,
        ):
            attempt = repo.record_failure_with_open_check("svc")

        assert attempt.did_open is True
        assert attempt.state.state == "open"
        mock_degraded.assert_called_once_with("svc")

    def test_l2_unhealthy_skips_l2_call_entirely(self, repo, l2_mock):
        _prime_l1_half_open(repo)
        repo._l2_healthy = False

        with patch.object(repo, "_record_open_check_degraded_mode") as mock_degraded:
            attempt = repo.record_failure_with_open_check("svc")

        # L2 is never asked when _l2_healthy is False.
        l2_mock.record_failure_with_open_check.assert_not_called()
        mock_degraded.assert_called_once_with("svc")
        assert attempt.did_open is True
        assert attempt.state.state == "open"

    def test_l2_none_uses_l1_path_without_executor(self):
        """When ``_l2 is None`` the wrapper delegates straight to L1 without
        submitting to the executor.
        """
        from baldur.adapters.memory.circuit_breaker import (
            LayeredCircuitBreakerStateRepository,
        )

        repo = LayeredCircuitBreakerStateRepository(l2_repo=None)
        _prime_l1_half_open(repo)

        with (
            patch.object(repo, "_get_executor") as mock_get_executor,
            patch.object(repo, "_record_open_check_degraded_mode") as mock_degraded,
        ):
            attempt = repo.record_failure_with_open_check("svc")

        mock_get_executor.assert_not_called()
        mock_degraded.assert_called_once_with("svc")
        assert attempt.did_open is True
        assert attempt.state.state == "open"
