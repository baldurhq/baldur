"""476 — LayeredCircuitBreakerStateRepository.try_acquire_half_open_slot.

Covers:

- D1/C3 L2-first synchronous dispatch (NOT ``_sync_to_l2_async``).
- C1 degraded fallback: L2 timeout / exception / unhealthy → L1 +
  ``half_open_degraded_mode_total`` increment.
- D6/G11 L1 writeback: after L2 returns ``allowed=True``, the L2-decided
  post-state is written back to L1 synchronously so a subsequent
  ``record_*`` doesn't read stale L1=open while L2 says half_open.
- D6 writeback failure: a raising L1 ``update_state`` is caught, logged
  WARNING (``circuit_breaker.l1_writeback_failed``), and the L2 tuple is
  returned unchanged (no exception propagation, L2 decision not rolled
  back).
- D8 stuck-recovery observability: when L2 returns marker
  ``"stuck_recovery"``, ``half_open_stuck_recovery_total`` is incremented.
- Degraded-mode admission over a hydrated HALF_OPEN row, which arrives
  without its window watermark.
- 771 D1 detection wiring at this call site: a rejected ``closed`` answer
  over a non-closed L1 row is handed to the convergence lane, and an
  ordinary rejection the two layers agree on is not. The lane's own logic
  lives in ``test_reject_path_convergence.py``.
"""

from __future__ import annotations

from concurrent.futures import TimeoutError as FuturesTimeoutError
from unittest.mock import MagicMock, patch

import pytest

from tests.factories.time_helpers import freeze_time


@pytest.fixture
def l2_mock():
    """L2 mock that supports the new try_acquire_half_open_slot contract."""
    from baldur.adapters.memory.circuit_breaker import (
        InMemoryCircuitBreakerStateRepository,
    )

    mock = MagicMock(spec=InMemoryCircuitBreakerStateRepository)
    mock.get_all_states.return_value = []
    mock._last_acquire_marker = ""
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


class TestLayeredTryAcquireBehavior:
    """L2-first dispatch + writeback + degraded fallback."""

    def test_l2_success_returns_tuple_verbatim(self, repo, l2_mock):
        l2_mock.try_acquire_half_open_slot.return_value = (True, "open", "half_open")
        l2_mock._last_acquire_marker = "transition"

        allowed, prev_state, new_state = repo.try_acquire_half_open_slot(
            service_name="svc", limit=10, stuck_timeout_seconds=60
        )

        assert (allowed, prev_state, new_state) == (True, "open", "half_open")
        l2_mock.try_acquire_half_open_slot.assert_called_once_with("svc", 10, 60)

    def test_l1_writeback_fires_after_l2_success(self, repo, l2_mock):
        """G11/D6: L1 must reflect L2's post-state so record_* takes the right branch."""
        l2_mock.try_acquire_half_open_slot.return_value = (True, "open", "half_open")
        l2_mock._last_acquire_marker = "transition"

        repo.try_acquire_half_open_slot(
            service_name="svc", limit=10, stuck_timeout_seconds=60
        )

        l1_state = repo._l1.get_by_service_name("svc")
        assert l1_state is not None
        assert l1_state.state == "half_open"
        # success_count is reset on the OPEN→HALF_OPEN writeback (D6).
        assert l1_state.success_count == 0

    def test_l1_writeback_skipped_when_l2_rejects(self, repo, l2_mock):
        """No writeback on rejection — the local L1 must not echo a denial.

        A ``half_open`` rejection is also not a contradiction: the trial is in
        progress and resolves itself, so nothing is handed to the convergence
        lane either.
        """
        repo._l1.get_or_create("svc")  # pre-existing CLOSED L1 state
        l2_mock.try_acquire_half_open_slot.return_value = (
            False,
            "half_open",
            "half_open",
        )
        l2_mock._last_acquire_marker = "rejected"

        with (
            patch.object(
                repo._l1, "update_state", wraps=repo._l1.update_state
            ) as wrapped,
            patch.object(repo, "_submit_reject_convergence") as mock_converge,
        ):
            repo.try_acquire_half_open_slot(
                service_name="svc", limit=10, stuck_timeout_seconds=60
            )

        wrapped.assert_not_called()
        mock_converge.assert_not_called()

    def test_contradicting_rejection_is_handed_to_the_convergence_lane(
        self, repo, l2_mock
    ):
        """771 D1: L2 rejects with ``closed`` while L1 still holds ``open``.

        Every further request on this worker would be rejected on an answer the
        acquire keeps discarding, so the call site hands the contradiction off
        — while returning L2's decision untouched.
        """
        from baldur.interfaces.repositories import CircuitBreakerStateEnum

        repo._l1.get_or_create("svc")
        repo._l1.update_state(
            service_name="svc", state=CircuitBreakerStateEnum.OPEN.value
        )
        l2_mock.try_acquire_half_open_slot.return_value = (False, "closed", "closed")
        l2_mock._last_acquire_marker = "no_op"

        with patch.object(repo, "_submit_reject_convergence") as mock_converge:
            decision = repo.try_acquire_half_open_slot(
                service_name="svc", limit=10, stuck_timeout_seconds=60
            )

        assert decision == (False, "closed", "closed")
        mock_converge.assert_called_once_with("svc")

    def test_l1_writeback_failure_logged_and_l2_tuple_returned(self, repo, l2_mock):
        """D6: writeback raises → WARNING log, L2 tuple returned unchanged."""
        l2_mock.try_acquire_half_open_slot.return_value = (True, "open", "half_open")
        l2_mock._last_acquire_marker = "transition"

        # Inject a raising L1 update_state.
        repo._l1.update_state = MagicMock(  # type: ignore[method-assign]
            side_effect=MemoryError("simulated writeback failure")
        )

        with patch(
            "baldur.adapters.memory.layered_repository.repository_operations.logger"
        ) as mock_logger:
            allowed, prev_state, new_state = repo.try_acquire_half_open_slot(
                service_name="svc", limit=10, stuck_timeout_seconds=60
            )

        # L2 decision is preserved; the layer does NOT roll back.
        assert (allowed, prev_state, new_state) == (True, "open", "half_open")

        warning_calls = [
            call
            for call in mock_logger.warning.call_args_list
            if call.args and call.args[0] == "circuit_breaker.l1_writeback_failed"
        ]
        assert len(warning_calls) == 1, (
            f"expected exactly one l1_writeback_failed WARNING, got {warning_calls}"
        )

    def test_l2_timeout_falls_back_to_l1_and_records_degraded(self, repo, l2_mock):
        """C1: FuturesTimeoutError on L2 → L1 fallback + degraded counter."""
        from baldur.interfaces.repositories import CircuitBreakerStateEnum

        repo._l1.get_or_create("svc")
        repo._l1.update_state(
            service_name="svc",
            state=CircuitBreakerStateEnum.OPEN.value,
        )

        # Simulate L2 timeout via the executor: the future.result() call inside
        # try_acquire raises FuturesTimeoutError.
        fake_future = MagicMock()
        fake_future.result.side_effect = FuturesTimeoutError()
        fake_executor = MagicMock()
        fake_executor.submit.return_value = fake_future

        with (
            patch.object(repo, "_get_executor", return_value=fake_executor),
            patch.object(repo, "_record_half_open_degraded_mode") as mock_degraded,
        ):
            allowed, prev_state, new_state = repo.try_acquire_half_open_slot(
                service_name="svc", limit=10, stuck_timeout_seconds=60
            )

        # L1 fallback: OPEN → HALF_OPEN transition under the L1 RLock.
        assert allowed is True
        assert prev_state == "open"
        assert new_state == "half_open"
        mock_degraded.assert_called_once_with("svc")

    def test_l2_exception_falls_back_to_l1_and_records_degraded(self, repo, l2_mock):
        """C1: arbitrary L2 exception → L1 fallback + degraded counter."""
        from baldur.interfaces.repositories import CircuitBreakerStateEnum

        repo._l1.get_or_create("svc")
        repo._l1.update_state(
            service_name="svc",
            state=CircuitBreakerStateEnum.OPEN.value,
        )

        fake_future = MagicMock()
        fake_future.result.side_effect = ConnectionError("redis down")
        fake_executor = MagicMock()
        fake_executor.submit.return_value = fake_future

        with (
            patch.object(repo, "_get_executor", return_value=fake_executor),
            patch.object(repo, "_record_half_open_degraded_mode") as mock_degraded,
        ):
            allowed, _prev, _new = repo.try_acquire_half_open_slot(
                service_name="svc", limit=10, stuck_timeout_seconds=60
            )

        assert allowed is True
        mock_degraded.assert_called_once_with("svc")

    def test_l2_unhealthy_skips_l2_call_entirely(self, repo, l2_mock):
        """When _l2_healthy=False, L2 is bypassed and L1 takes the call."""
        from baldur.interfaces.repositories import CircuitBreakerStateEnum

        repo._l1.get_or_create("svc")
        repo._l1.update_state(
            service_name="svc",
            state=CircuitBreakerStateEnum.OPEN.value,
        )
        repo._l2_healthy = False

        with patch.object(repo, "_record_half_open_degraded_mode") as mock_degraded:
            allowed, prev_state, new_state = repo.try_acquire_half_open_slot(
                service_name="svc", limit=10, stuck_timeout_seconds=60
            )

        l2_mock.try_acquire_half_open_slot.assert_not_called()
        mock_degraded.assert_called_once_with("svc")
        assert (allowed, prev_state, new_state) == (True, "open", "half_open")

    def test_stuck_recovery_marker_emits_observability_counter(self, repo, l2_mock):
        """D8: marker "stuck_recovery" → half_open_stuck_recovery_total++."""
        l2_mock.try_acquire_half_open_slot.return_value = (
            True,
            "half_open",
            "half_open",
        )
        l2_mock._last_acquire_marker = "stuck_recovery"

        with patch.object(repo, "_record_half_open_stuck_recovery") as mock_stuck:
            repo.try_acquire_half_open_slot(
                service_name="svc", limit=10, stuck_timeout_seconds=60
            )

        mock_stuck.assert_called_once_with("svc")

    def test_non_stuck_marker_does_not_emit_stuck_recovery(self, repo, l2_mock):
        """A successful "transition" / "increment" must not increment stuck."""
        l2_mock.try_acquire_half_open_slot.return_value = (True, "open", "half_open")
        l2_mock._last_acquire_marker = "transition"

        with patch.object(repo, "_record_half_open_stuck_recovery") as mock_stuck:
            repo.try_acquire_half_open_slot(
                service_name="svc", limit=10, stuck_timeout_seconds=60
            )

        mock_stuck.assert_not_called()


class TestLayeredHydratedWatermarkAbsentBehavior:
    """A hydrated HALF_OPEN row admits its window correctly on the L1 lane.

    Both hydration lanes -- the construction-time load and the L1-miss
    lane -- restore ``state`` from the durable snapshot but leave the window
    fields at their layer-local values, because those belong to the atomic
    slot primitives rather than to bulk transfer. A worker can therefore
    hold ``half_open`` with counter 0 and no watermark. When L2 then goes
    unavailable and admission falls back to that row, it must behave like a
    window that has just started -- not like one that stalled.
    """

    @pytest.fixture
    def hydrated_repo(self, repo):
        """A degraded-mode repo whose L1 row came from a snapshot."""
        from baldur.interfaces.repositories import CircuitBreakerStateData

        repo._l1.hydrate_snapshot(
            CircuitBreakerStateData(service_name="svc", state="half_open")
        )
        repo._l2_healthy = False  # L2 down: L1 owns admission (degraded mode)
        return repo

    def test_hydrated_row_carries_no_window_watermark(self, hydrated_repo):
        """The premise every case below rests on: hydration drops the window."""
        l1_state = hydrated_repo._l1.get_by_service_name("svc")

        assert l1_state.state == "half_open"
        assert l1_state.half_open_request_count == 0
        assert l1_state.half_open_window_started_at is None

    def test_first_fallback_acquire_stamps_the_window(self, hydrated_repo, l2_mock):
        """SC3: the trial call that lands on the hydrated row starts its window."""
        allowed, prev_state, new_state = hydrated_repo.try_acquire_half_open_slot(
            service_name="svc", limit=3, stuck_timeout_seconds=60
        )

        assert (allowed, prev_state, new_state) == (True, "half_open", "half_open")
        l2_mock.try_acquire_half_open_slot.assert_not_called()
        l1_state = hydrated_repo._l1.get_by_service_name("svc")
        assert l1_state.half_open_request_count == 1
        assert l1_state.half_open_window_started_at is not None

    def test_hydrated_window_rejects_at_limit_instead_of_recovering(
        self, hydrated_repo
    ):
        """SC4 negative: the healthy window is no longer misread as stalled.

        Driving the hydrated row to the cluster cap through the fallback lane
        and asking once more, still well inside ``stuck_timeout``, must be an
        ordinary rejection. Before the boundary rule the row still had no
        watermark at that point, so the stuck-window auto-reset fired and
        admitted another full window of trial calls.
        """
        limit = 3
        with freeze_time("2026-02-10 10:00:00"):
            granted = [
                hydrated_repo.try_acquire_half_open_slot(
                    service_name="svc", limit=limit, stuck_timeout_seconds=60
                )[0]
                for _ in range(limit)
            ]

            allowed, _prev, _new = hydrated_repo.try_acquire_half_open_slot(
                service_name="svc", limit=limit, stuck_timeout_seconds=60
            )

        assert granted == [True] * limit
        assert allowed is False
        assert hydrated_repo._l1._last_acquire_marker == "rejected"
        assert (
            hydrated_repo._l1.get_by_service_name("svc").half_open_request_count
            == limit
        )
