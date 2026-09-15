"""A HALF_OPEN close the store could not decide is not performed locally.

793 D3. ``record_success_with_close_check`` routes the trial success to L2,
the authority on the close. When L2 fails transiently while it is still
healthy — a timeout, an exception, or an answer that is neither HALF_OPEN nor
CLOSED — the wrapper no longer closes on L1: nothing would mirror a local close
while L2 is failing, and drift reconciliation runs only on the quarantine
edge, so a close at blip time would leave this worker CLOSED against the
store's OPEN until a restart. The trial success is not credited
(``did_close=False``, the L1 row unchanged); the next trial re-earns it. Only
a quarantined L2, or a store nobody named, hands the decision to L1.

Verification techniques applied:
- Error path (parametrized): timeout / exception / stale answer while L2 is
  healthy -> ``did_close=False``, L1 untouched, degraded metric recorded
- State transition: the third consecutive failure quarantines L2 and that
  very call falls back to L1's close; an ``UnconfiguredStoreError`` falls
  back without counting a failure
"""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from unittest.mock import MagicMock, patch

import pytest

from baldur.core.exceptions import UnconfiguredStoreError
from baldur.interfaces.repositories import (
    CircuitBreakerCloseAttempt,
    CircuitBreakerStateData,
    CircuitBreakerStateEnum,
)

SVC = "payment-api"
CLOSED = CircuitBreakerStateEnum.CLOSED.value
OPEN = CircuitBreakerStateEnum.OPEN.value
HALF_OPEN = CircuitBreakerStateEnum.HALF_OPEN.value
SUCCESS_THRESHOLD = 1


@pytest.fixture
def repo(mock_l2_repo):
    from baldur.adapters.memory.circuit_breaker import (
        LayeredCircuitBreakerStateRepository,
    )

    r = LayeredCircuitBreakerStateRepository(l2_repo=mock_l2_repo, adapter_type="redis")
    mock_l2_repo.reset_mock()
    r._l2_healthy = True
    r._l2_consecutive_failures = 0
    r._l1.hydrate_snapshot(
        CircuitBreakerStateData(service_name=SVC, state=HALF_OPEN, failure_count=5)
    )
    return r


def _executor_raising(exc: BaseException) -> MagicMock:
    """An executor whose submitted future raises ``exc`` on ``result()``."""
    future = MagicMock(spec=Future)
    future.result.side_effect = exc
    executor = MagicMock(spec=ThreadPoolExecutor)
    executor.submit.return_value = future
    return executor


def _stale_attempt(state: str) -> CircuitBreakerCloseAttempt:
    return CircuitBreakerCloseAttempt(
        state=CircuitBreakerStateData(service_name=SVC, state=state),
        did_close=False,
    )


class TestUncreditedCloseBehavior:
    """Where the close decision goes when L2 cannot make it."""

    @pytest.mark.parametrize(
        "l2_failure",
        [FuturesTimeoutError(), ConnectionError("redis blip")],
        ids=["timeout", "exception"],
    )
    def test_transient_l2_failure_leaves_l1_half_open_and_credits_nothing(
        self, repo, l2_failure
    ):
        """The gain: no local close while the store is merely unreachable."""
        before = repo._l1.get_by_service_name(SVC)

        with (
            patch.object(
                repo, "_get_executor", return_value=_executor_raising(l2_failure)
            ),
            patch.object(repo, "_record_close_check_degraded_mode") as degraded,
        ):
            attempt = repo.record_success_with_close_check(SVC, SUCCESS_THRESHOLD)

        assert attempt.did_close is False
        assert attempt.state.state == HALF_OPEN
        after = repo._l1.get_by_service_name(SVC)
        assert after.state == HALF_OPEN
        assert after.success_count == before.success_count
        assert after.failure_count == before.failure_count
        degraded.assert_called_once_with(SVC)
        # One failure counted; L2 still healthy.
        assert repo._l2_consecutive_failures == 1
        assert repo._l2_healthy is True

    @pytest.mark.parametrize(
        "stale_state", [OPEN, CLOSED + "_corrupt"], ids=["open", "unknown"]
    )
    def test_stale_l2_answer_leaves_l1_half_open_and_credits_nothing(
        self, repo, mock_l2_repo, stale_state
    ):
        """The store never saw HALF_OPEN: a local close would be one it did not decide."""
        mock_l2_repo.record_success_with_close_check.return_value = _stale_attempt(
            stale_state
        )

        with patch.object(repo, "_record_close_check_degraded_mode") as degraded:
            attempt = repo.record_success_with_close_check(SVC, SUCCESS_THRESHOLD)

        assert attempt.did_close is False
        assert attempt.state.state == HALF_OPEN
        assert repo._l1.get_by_service_name(SVC).state == HALF_OPEN
        degraded.assert_called_once_with(SVC)

    def test_l2_decided_close_is_credited_and_written_back(self, repo, mock_l2_repo):
        """Control: an L2 that answers CLOSED closes L1 too."""
        mock_l2_repo.record_success_with_close_check.return_value = (
            CircuitBreakerCloseAttempt(
                state=CircuitBreakerStateData(service_name=SVC, state=CLOSED),
                did_close=True,
            )
        )

        attempt = repo.record_success_with_close_check(SVC, SUCCESS_THRESHOLD)

        assert attempt.did_close is True
        assert repo._l1.get_by_service_name(SVC).state == CLOSED

    def test_third_consecutive_failure_quarantines_l2_and_falls_back_to_l1(self, repo):
        """State transition: the call that crosses the edge closes on L1.

        Two blips are uncredited; the third quarantines L2, and from then on
        the process is in L1-only mode by design — that very call takes the
        L1 close.
        """
        executor = _executor_raising(ConnectionError("redis down"))
        with (
            patch.object(repo, "_get_executor", return_value=executor),
            patch.object(repo, "_record_close_check_degraded_mode"),
        ):
            first = repo.record_success_with_close_check(SVC, SUCCESS_THRESHOLD)
            second = repo.record_success_with_close_check(SVC, SUCCESS_THRESHOLD)
            assert (first.did_close, second.did_close) == (False, False)
            assert repo._l2_healthy is True

            third = repo.record_success_with_close_check(SVC, SUCCESS_THRESHOLD)

        assert repo._l2_healthy is False
        assert third.did_close is True
        assert repo._l1.get_by_service_name(SVC).state == CLOSED

    def test_quarantined_l2_on_entry_falls_back_to_l1_without_dialing(
        self, repo, mock_l2_repo
    ):
        repo._l2_healthy = False

        with patch.object(repo, "_record_close_check_degraded_mode") as degraded:
            attempt = repo.record_success_with_close_check(SVC, SUCCESS_THRESHOLD)

        mock_l2_repo.record_success_with_close_check.assert_not_called()
        assert attempt.did_close is True
        assert repo._l1.get_by_service_name(SVC).state == CLOSED
        degraded.assert_called_once_with(SVC)

    def test_unconfigured_store_falls_back_to_l1_without_counting_a_failure(
        self, repo, mock_l2_repo
    ):
        """No store was named: the process memory is the store, and it closes."""
        mock_l2_repo.record_success_with_close_check.side_effect = (
            UnconfiguredStoreError(
                service=SVC, operation="record_success_with_close_check"
            )
        )

        with patch.object(repo, "_record_close_check_degraded_mode"):
            attempt = repo.record_success_with_close_check(SVC, SUCCESS_THRESHOLD)

        assert attempt.did_close is True
        assert repo._l1.get_by_service_name(SVC).state == CLOSED
        assert repo._l2_consecutive_failures == 0
        assert repo._l2_healthy is True
