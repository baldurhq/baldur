"""771 — the inline L1→L2 write lane a resident executor task uses.

A task that already occupies a pool thread must not submit its own work and
then wait for it: on a pool sized one or two the inner task cannot start until
the outer one returns, so the wait always times out and three of those
synchronized timeouts quarantine a perfectly healthy L2. The mirror body and
the repair therefore have inline variants that run on the calling thread.

Covers:

- ``_sync_to_l2_inline`` — the shared write body: success routes to
  ``_handle_l2_success``, every failure to ``_handle_l2_error``, nothing
  propagates to the caller.
- ``_resolve_repair_row`` — the freshness + pin-skip decision both repair
  variants share, so they cannot drift apart.
- ``_repair_row_to_l2_inline`` — the same tri-state contract as the
  timeout-bounded repair, with no nested submit.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock, patch

import pytest

from baldur.interfaces.repositories import (
    CircuitBreakerStateData,
    CircuitBreakerStateEnum,
)


@pytest.fixture
def repo(mock_l2_repo):
    """Layered repo with a mock L2, counters zeroed after construction."""
    from baldur.adapters.memory.circuit_breaker import (
        LayeredCircuitBreakerStateRepository,
    )

    r = LayeredCircuitBreakerStateRepository(l2_repo=mock_l2_repo, adapter_type="redis")
    mock_l2_repo.reset_mock()
    r._l2_healthy = True
    r._l2_consecutive_failures = 0
    return r


def _l1_row(repo, service: str, state: str) -> None:
    repo._l1.get_or_create(service)
    repo._l1.update_state(service_name=service, state=state)


# =============================================================================
# _sync_to_l2_inline — the shared write body
# =============================================================================


class TestInlineSyncBehavior:
    """The mirror body, run on the caller's own thread."""

    def test_successful_write_forwards_the_row_fields_and_reports_success(
        self, repo, mock_l2_repo
    ):
        # Given
        row = CircuitBreakerStateData(
            service_name="svc",
            state=CircuitBreakerStateEnum.OPEN.value,
            failure_count=5,
            success_count=1,
        )

        # When
        with patch.object(repo, "_handle_l2_success") as mock_success:
            result = repo._sync_to_l2_inline("svc", row)

        # Then
        assert result is True
        mock_l2_repo.get_or_create.assert_called_once_with("svc")
        mock_l2_repo.update_state.assert_called_once_with(
            service_name="svc",
            state=CircuitBreakerStateEnum.OPEN.value,
            failure_count=5,
            success_count=1,
            opened_at=None,
        )
        mock_success.assert_called_once()

    def test_write_failure_routes_to_the_error_handler_and_returns_false(
        self, repo, mock_l2_repo
    ):
        """Quarantine accounting stays correct: a real L2 failure counts."""
        row = CircuitBreakerStateData(
            service_name="svc", state=CircuitBreakerStateEnum.OPEN.value
        )
        failure = ConnectionError("redis down")
        mock_l2_repo.update_state.side_effect = failure

        with patch.object(repo, "_handle_l2_error") as mock_error:
            result = repo._sync_to_l2_inline("svc", row)

        assert result is False
        mock_error.assert_called_once_with(
            "sync", "svc", failure, CircuitBreakerStateEnum.OPEN.value
        )

    def test_write_failure_does_not_propagate_to_the_caller(self, repo, mock_l2_repo):
        """Fail-open: a resident task must never die on its mirror."""
        mock_l2_repo.get_or_create.side_effect = RuntimeError("boom")
        row = CircuitBreakerStateData(service_name="svc")

        # Must not raise.
        assert repo._sync_to_l2_inline("svc", row) is False

    def test_absent_l2_reports_no_write_without_touching_the_handlers(self, repo):
        repo._l2 = None

        with (
            patch.object(repo, "_handle_l2_success") as mock_success,
            patch.object(repo, "_handle_l2_error") as mock_error,
        ):
            result = repo._sync_to_l2_inline(
                "svc", CircuitBreakerStateData(service_name="svc")
            )

        assert result is False
        mock_success.assert_not_called()
        mock_error.assert_not_called()


# =============================================================================
# _resolve_repair_row — freshness + pin neutrality, decided once
# =============================================================================


class TestRepairRowResolutionBehavior:
    """The branch both repair variants share."""

    def test_absent_row_resolves_to_no_repair(self, repo):
        assert repo._resolve_repair_row("svc") is None

    def test_pinned_row_resolves_to_no_repair(self, repo):
        """The mirror opens with ``get_or_create``, whose default payload says
        "not manually controlled" — repairing a pinned service after the
        durable row was lost would erase the operator's decision from the
        shared store.
        """
        repo._l1.set_manual_control(
            service_name="svc",
            state=CircuitBreakerStateEnum.OPEN.value,
            reason="operator block",
        )

        assert repo._resolve_repair_row("svc") is None

    def test_plain_row_resolves_to_that_row(self, repo):
        _l1_row(repo, "svc", CircuitBreakerStateEnum.HALF_OPEN.value)

        row = repo._resolve_repair_row("svc")

        assert row is not None
        assert row.state == CircuitBreakerStateEnum.HALF_OPEN.value

    def test_row_is_read_fresh_rather_than_taken_from_an_earlier_snapshot(self, repo):
        """Freshness: what the mirror writes is the row as it stands now.

        A snapshot predating an operator's Block would otherwise be written
        back over it, leaving a row that still reports itself manually
        controlled while carrying the pre-Block state.
        """
        # Given — a caller-era snapshot, then the row moves on
        _l1_row(repo, "svc", CircuitBreakerStateEnum.OPEN.value)
        stale_snapshot = repo._l1.get_by_service_name("svc")
        repo._l1.update_state(
            service_name="svc", state=CircuitBreakerStateEnum.HALF_OPEN.value
        )

        # When
        resolved = repo._resolve_repair_row("svc")

        # Then
        assert stale_snapshot.state == CircuitBreakerStateEnum.OPEN.value
        assert resolved.state == CircuitBreakerStateEnum.HALF_OPEN.value


# =============================================================================
# _repair_row_to_l2_inline — tri-state, no nested submit
# =============================================================================


class TestInlineRepairBehavior:
    """The repair a resident task performs on its own thread."""

    def test_successful_mirror_reports_repaired(self, repo, mock_l2_repo):
        _l1_row(repo, "svc", CircuitBreakerStateEnum.HALF_OPEN.value)

        assert repo._repair_row_to_l2_inline("svc") is True
        mock_l2_repo.update_state.assert_called_once()

    def test_failed_mirror_reports_a_failure_rather_than_a_skip(
        self, repo, mock_l2_repo
    ):
        """``False`` and ``None`` are different outcomes downstream: a skip is
        not a failure and must not be reported as one.
        """
        _l1_row(repo, "svc", CircuitBreakerStateEnum.HALF_OPEN.value)
        mock_l2_repo.update_state.side_effect = ConnectionError("redis down")

        assert repo._repair_row_to_l2_inline("svc") is False

    @pytest.mark.parametrize(
        "pin_the_row",
        [False, True],
        ids=["row_absent", "row_pinned"],
    )
    def test_skipped_repair_reports_none_and_writes_nothing(
        self, repo, mock_l2_repo, pin_the_row
    ):
        if pin_the_row:
            repo._l1.set_manual_control(
                service_name="svc",
                state=CircuitBreakerStateEnum.OPEN.value,
                reason="operator block",
            )

        assert repo._repair_row_to_l2_inline("svc") is None
        mock_l2_repo.update_state.assert_not_called()
        mock_l2_repo.get_or_create.assert_not_called()

    def test_mirror_runs_inline_without_submitting_to_the_pool(self, repo):
        """The property the whole variant exists for.

        The timeout-bounded repair submits its mirror; this one must not —
        a task that submits from inside the pool it runs in occupies two
        slots and deadlocks a pool of one.
        """
        _l1_row(repo, "svc", CircuitBreakerStateEnum.HALF_OPEN.value)
        fake_executor = MagicMock(spec=ThreadPoolExecutor)

        with patch.object(repo, "_get_executor", return_value=fake_executor):
            assert repo._repair_row_to_l2_inline("svc") is True

        fake_executor.submit.assert_not_called()

    @pytest.mark.parametrize(
        "pin_the_row",
        [False, True],
        ids=["row_absent", "row_pinned"],
    )
    def test_skip_semantics_match_the_timeout_bounded_repair(self, repo, pin_the_row):
        """Both variants decline the same rows — the shared resolution helper
        is what keeps the two lanes from drifting.
        """
        if pin_the_row:
            repo._l1.set_manual_control(
                service_name="svc",
                state=CircuitBreakerStateEnum.OPEN.value,
                reason="operator block",
            )

        assert repo._repair_row_to_l2_inline("svc") is None
        assert repo._repair_row_to_l2("svc") is None
