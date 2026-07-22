"""498 D6 — LayeredCircuitBreakerStateRepository.record_success_with_close_check.

Covers the L2-authoritative routing of the atomic close-check so the
cross-process exactly-one ``CIRCUIT_BREAKER_CLOSED`` contract holds
across gunicorn workers / K8s replicas (not just within a single
process).

- Happy path: L2 returns close/increment → L1 writeback path mirrors
  the L2-authoritative decision. Close-branch resets the L1 sliding
  window + ``opened_at`` (D6 step 3 + D9), so the next ``record_failure``
  doesn't trip the breaker off a stale OPEN-era counter.
- Stale-L2 guard (D6 step 2): L2 returns ``state ∉ {half_open, closed}``
  (its hash was never updated to the caller's HALF_OPEN expectation);
  the wrapper falls back to L1's atomic close-check and bumps the
  ``close_check_degraded_mode`` counter, but does NOT write the stale
  state back to L1.
- Fall-through paths (D6 step 5/6): L2 timeout / generic exception /
  ``_l2_healthy=False`` / ``_l2 is None`` → L1 fallback +
  ``close_check_degraded_mode`` counter increment.
"""

from __future__ import annotations

from concurrent.futures import TimeoutError as FuturesTimeoutError
from unittest.mock import MagicMock, patch

import pytest

from baldur.interfaces.repositories import (
    CircuitBreakerCloseAttempt,
    CircuitBreakerStateData,
    CircuitBreakerStateEnum,
)


def _attempt(state: str, *, did_close: bool, success_count: int = 0):
    """Build a minimal CircuitBreakerCloseAttempt mirroring the Redis Lua return."""
    state_data = CircuitBreakerStateData(
        service_name="svc",
        id=None,
        state=state,
        failure_count=0,
        success_count=success_count,
        last_failure_at=None,
        opened_at=None,
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
    return CircuitBreakerCloseAttempt(state=state_data, did_close=did_close)


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


# =============================================================================
# Behavior — L2-authoritative routing + L1 writeback (D6 step 3)
# =============================================================================


class TestLayeredRecordSuccessWithCloseCheckBehavior:
    """L2 is the source of truth; L1 mirrors L2's post-state."""

    def test_l2_close_attempt_returned_verbatim(self, repo, l2_mock):
        # Given: L2 reports the close-branch winner.
        l2_mock.record_success_with_close_check.return_value = _attempt(
            "closed", did_close=True
        )

        attempt = repo.record_success_with_close_check("svc", success_threshold=2)

        assert attempt.did_close is True
        assert attempt.state.state == "closed"
        l2_mock.record_success_with_close_check.assert_called_once_with("svc", 2)

    def test_l1_writeback_resets_counters_and_opened_at_on_close(self, repo, l2_mock):
        """D6 step-3 close-branch invariant: prime L1 with stale failures +
        opened_at; after the L2 close-branch returns, the L1 counters are
        zeroed and opened_at is None (covers D9).

        Covers both did_close=True (winner) and did_close=False (race-loser /
        post-crash convergence) — both yield state='closed' which must reset
        L1 to avoid a stale OPEN-era counter leaking into the next CLOSED
        ``record_failure`` and prematurely re-tripping the circuit.
        """
        from baldur.utils.time import utc_now

        # Prime L1 with a stale OPEN state + failure-window history.
        repo._l1.get_or_create("svc")
        for _ in range(3):
            repo._l1.record_failure("svc")
        repo._l1.update_state(
            service_name="svc",
            state=CircuitBreakerStateEnum.OPEN.value,
            opened_at=utc_now(),
        )

        # Pre-conditions: stale counters and opened_at are visible.
        assert repo._l1.get_by_service_name("svc").failure_count == 3
        assert repo._l1.get_by_service_name("svc").opened_at is not None

        # L2 reports state=closed (winner branch).
        l2_mock.record_success_with_close_check.return_value = _attempt(
            "closed", did_close=True
        )

        repo.record_success_with_close_check("svc", success_threshold=2)

        # L1 reflects the L2-authoritative close decision and the counters /
        # opened_at are cleared per D6 step 3 + D9.
        l1_state = repo._l1.get_by_service_name("svc")
        assert l1_state.state == "closed"
        assert l1_state.opened_at is None
        assert l1_state.failure_count == 0
        assert l1_state.success_count == 0

    def test_l1_writeback_resets_counters_when_did_close_is_false(self, repo, l2_mock):
        """Race-loser / post-crash convergence (did_close=False, state=closed)
        also resets L1 — both branches converge on the same writeback path.
        """
        from baldur.utils.time import utc_now

        repo._l1.get_or_create("svc")
        for _ in range(3):
            repo._l1.record_failure("svc")
        repo._l1.update_state(
            service_name="svc",
            state=CircuitBreakerStateEnum.OPEN.value,
            opened_at=utc_now(),
        )

        l2_mock.record_success_with_close_check.return_value = _attempt(
            "closed", did_close=False
        )

        repo.record_success_with_close_check("svc", success_threshold=2)

        l1_state = repo._l1.get_by_service_name("svc")
        assert l1_state.state == "closed"
        assert l1_state.opened_at is None
        assert l1_state.failure_count == 0

    def test_half_open_increment_writeback_does_not_reset_counters(self, repo, l2_mock):
        """D6 step-3 else-branch: HALF_OPEN increment writes success_count from
        the L2 attempt, the counters are NOT reset (the HALF_OPEN trial is
        still active).
        """
        repo._l1.get_or_create("svc")
        repo._l1.update_state(
            service_name="svc",
            state=CircuitBreakerStateEnum.HALF_OPEN.value,
        )
        # Seed a non-zero success count that the increment branch must not zero.
        for _ in range(2):
            repo._l1.record_success("svc")

        l2_mock.record_success_with_close_check.return_value = _attempt(
            "half_open", did_close=False, success_count=3
        )

        repo.record_success_with_close_check("svc", success_threshold=5)

        l1_state = repo._l1.get_by_service_name("svc")
        assert l1_state.state == "half_open"
        # Increment branch writes the L2 success_count (3), NOT reset to 0.
        assert l1_state.success_count == 3


# =============================================================================
# Contract — stale-L2 guard (D6 step 2)
# =============================================================================


class TestLayeredRecordSuccessWithCloseCheckContract:
    """Stale-L2 detection: state ∉ {half_open, closed} → L1 fallback."""

    @pytest.mark.parametrize("stale_state", ["open", "missing", "unknown"])
    def test_stale_l2_state_falls_back_to_l1_without_writeback(
        self, repo, l2_mock, stale_state
    ):
        # Given: L1 sees HALF_OPEN (it was set by a prior try_acquire that
        # took the L1-fallback path because L2 was unhealthy at that
        # instant); L2 still reports OPEN/missing/unknown.
        repo._l1.get_or_create("svc")
        repo._l1.update_state(
            service_name="svc",
            state=CircuitBreakerStateEnum.HALF_OPEN.value,
        )

        l2_mock.record_success_with_close_check.return_value = _attempt(
            stale_state, did_close=False
        )

        with patch.object(repo, "_record_close_check_degraded_mode") as mock_degraded:
            attempt = repo.record_success_with_close_check("svc", success_threshold=2)

        # The wrapper falls back to L1's atomic close path; the returned
        # attempt is the L1 decision (HALF_OPEN increment of 1, since L1 was
        # primed at HALF_OPEN with success_count=0).
        assert attempt.state.state == "half_open"

        # L1 was NOT corrupted to the stale state — the guard refused to
        # writeback OPEN/missing/unknown into L1.
        l1_state = repo._l1.get_by_service_name("svc")
        assert l1_state.state == "half_open"

        # Stale-L2 guard increments the degraded-mode counter (D7).
        mock_degraded.assert_called_once_with("svc")


# =============================================================================
# Behavior — fall-through paths: timeout, exception, unhealthy, None L2
# =============================================================================


class TestLayeredRecordSuccessWithCloseCheckFallback:
    """L2 unavailable -> L1 fallback + degraded-mode counter increment."""

    def test_l2_timeout_falls_back_to_l1_with_degraded_counter(self, repo, l2_mock):
        repo._l1.get_or_create("svc")
        repo._l1.update_state(
            service_name="svc",
            state=CircuitBreakerStateEnum.HALF_OPEN.value,
        )

        fake_future = MagicMock()
        fake_future.result.side_effect = FuturesTimeoutError()
        fake_executor = MagicMock()
        fake_executor.submit.return_value = fake_future

        with (
            patch.object(repo, "_get_executor", return_value=fake_executor),
            patch.object(repo, "_record_close_check_degraded_mode") as mock_degraded,
        ):
            attempt = repo.record_success_with_close_check("svc", success_threshold=2)

        # L1-decided attempt (HALF_OPEN with success_count=1 since threshold=2).
        assert attempt.state.state == "half_open"
        assert attempt.did_close is False
        mock_degraded.assert_called_once_with("svc")

    def test_l2_generic_exception_falls_back_to_l1_with_degraded_counter(
        self, repo, l2_mock
    ):
        repo._l1.get_or_create("svc")
        repo._l1.update_state(
            service_name="svc",
            state=CircuitBreakerStateEnum.HALF_OPEN.value,
        )

        fake_future = MagicMock()
        fake_future.result.side_effect = ConnectionError("redis down")
        fake_executor = MagicMock()
        fake_executor.submit.return_value = fake_future

        with (
            patch.object(repo, "_get_executor", return_value=fake_executor),
            patch.object(repo, "_record_close_check_degraded_mode") as mock_degraded,
        ):
            attempt = repo.record_success_with_close_check("svc", success_threshold=2)

        assert attempt.state.state == "half_open"
        mock_degraded.assert_called_once_with("svc")

    def test_l2_unhealthy_skips_l2_call_entirely(self, repo, l2_mock):
        repo._l1.get_or_create("svc")
        repo._l1.update_state(
            service_name="svc",
            state=CircuitBreakerStateEnum.HALF_OPEN.value,
        )
        repo._l2_healthy = False

        with patch.object(repo, "_record_close_check_degraded_mode") as mock_degraded:
            attempt = repo.record_success_with_close_check("svc", success_threshold=2)

        # L2 is never asked when _l2_healthy is False.
        l2_mock.record_success_with_close_check.assert_not_called()
        mock_degraded.assert_called_once_with("svc")
        # L1-authoritative attempt: HALF_OPEN, success_count=1.
        assert attempt.state.state == "half_open"

    def test_l2_none_uses_l1_path_without_executor(self, l2_mock):
        """When ``_l2 is None`` (e.g. memory-only wiring) the wrapper
        delegates straight to L1 without submitting to the executor.
        """
        from baldur.adapters.memory.circuit_breaker import (
            LayeredCircuitBreakerStateRepository,
        )

        repo = LayeredCircuitBreakerStateRepository(l2_repo=None)
        repo._l1.get_or_create("svc")
        repo._l1.update_state(
            service_name="svc",
            state=CircuitBreakerStateEnum.HALF_OPEN.value,
        )

        with (
            patch.object(repo, "_get_executor") as mock_get_executor,
            patch.object(repo, "_record_close_check_degraded_mode") as mock_degraded,
        ):
            attempt = repo.record_success_with_close_check("svc", success_threshold=2)

        mock_get_executor.assert_not_called()
        mock_degraded.assert_called_once_with("svc")
        assert attempt.state.state == "half_open"
