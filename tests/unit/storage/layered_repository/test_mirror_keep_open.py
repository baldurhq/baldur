"""The L1 -> L2 snapshot mirrors pass the store-side keep-open guard.

793 D3. A snapshot mirror writes this worker's whole row over the store's. Its
L1 genuinely is CLOSED, so only the store can tell that the row it is about to
overwrite is a peer's automatic OPEN; the repair lanes therefore pass
``skip_if_pinned=True, keep_open=True`` and the manual-control write-through —
the operator's own write — passes neither. A CLOSED snapshot under the guard
is skipped outright while the L2 backend is degraded: the mirror opens with an
unguarded ``get_or_create`` that would write a default CLOSED row into memory
and the WAL before the guarded update ever runs.

Verification techniques applied:
- Contract: the directives each lane forwards to ``update_state``
- Degraded: a CLOSED snapshot returns ``None`` with ``get_or_create`` never
  called; an OPEN snapshot still mirrors
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from baldur.adapters.resilient.backend import ResilientStorageBackend
from baldur.interfaces.repositories import (
    CircuitBreakerStateData,
    CircuitBreakerStateEnum,
)

SVC = "payment-api"
CLOSED = CircuitBreakerStateEnum.CLOSED.value
OPEN = CircuitBreakerStateEnum.OPEN.value


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
    r._get_timeout_seconds = lambda: 5.0
    return r


def _degrade_l2(mock_l2_repo) -> None:
    """Give the mock L2 a resilient backend that answers from its fallback."""
    backend = MagicMock(spec=ResilientStorageBackend)
    backend.is_degraded = True
    mock_l2_repo._backend = backend


def _row(state: str, failure_count: int = 0) -> CircuitBreakerStateData:
    return CircuitBreakerStateData(
        service_name=SVC, state=state, failure_count=failure_count
    )


class TestMirrorKeepOpenBehavior:
    """Which mirrors carry the guards, and when a mirror stands down."""

    # ------------------------------------------------------------ contract

    def test_inline_repair_passes_both_store_side_guards(self, repo, mock_l2_repo):
        repo._l1.get_or_create(SVC)

        with patch.object(repo, "_handle_l2_success"):
            result = repo._repair_row_to_l2_inline(SVC)

        assert result is True
        kwargs = mock_l2_repo.update_state.call_args.kwargs
        assert kwargs["skip_if_pinned"] is True
        assert kwargs["keep_open"] is True

    def test_timeout_bounded_repair_passes_both_store_side_guards(
        self, repo, mock_l2_repo
    ):
        repo._l1.get_or_create(SVC)

        with patch.object(repo, "_handle_l2_success"):
            result = repo._repair_row_to_l2(SVC)

        assert result is True
        kwargs = mock_l2_repo.update_state.call_args.kwargs
        assert kwargs["skip_if_pinned"] is True
        assert kwargs["keep_open"] is True

    def test_plain_inline_mirror_forwards_the_keep_open_it_is_given(
        self, repo, mock_l2_repo
    ):
        with patch.object(repo, "_handle_l2_success"):
            repo._sync_to_l2_inline(SVC, _row(CLOSED, failure_count=2), keep_open=True)

        kwargs = mock_l2_repo.update_state.call_args.kwargs
        assert kwargs["keep_open"] is True
        assert kwargs["skip_if_pinned"] is False

    def test_manual_control_write_through_passes_neither_guard(
        self, repo, mock_l2_repo
    ):
        """The operator's own write outranks a pin and may close a stored OPEN row."""
        repo._l1.get_or_create(SVC)
        repo._l1.set_manual_control(SVC, CLOSED, reason="operator allow")
        row = repo._l1.get_by_service_name(SVC)

        with patch.object(repo, "_handle_l2_success"):
            repo._write_manual_control_through(SVC, row, repo._pin_fields_write(row))

        kwargs = mock_l2_repo.update_state.call_args.kwargs
        assert kwargs.get("skip_if_pinned", False) is False
        assert kwargs.get("keep_open", False) is False

    def test_update_state_forwards_keep_open_to_l1(self, repo):
        """The layered ``update_state`` threads the directive to its L1 row."""
        repo._l1.hydrate_snapshot(_row(OPEN, failure_count=5))

        result = repo.update_state(
            service_name=SVC, state=CLOSED, failure_count=0, keep_open=True
        )

        assert result is True
        assert repo._l1.get_by_service_name(SVC).state == OPEN

    # ------------------------------------------------------------- degraded

    def test_closed_snapshot_on_a_degraded_backend_is_skipped_inline(
        self, repo, mock_l2_repo
    ):
        """``None``: nothing attempted — not even the mirror's opening ``get_or_create``."""
        _degrade_l2(mock_l2_repo)

        result = repo._sync_to_l2_inline(SVC, _row(CLOSED), keep_open=True)

        assert result is None
        mock_l2_repo.get_or_create.assert_not_called()
        mock_l2_repo.update_state.assert_not_called()

    def test_closed_snapshot_on_a_degraded_backend_is_skipped_with_timeout(
        self, repo, mock_l2_repo
    ):
        _degrade_l2(mock_l2_repo)

        result = repo._sync_to_l2_with_timeout(SVC, _row(CLOSED), keep_open=True)

        assert result is None
        mock_l2_repo.get_or_create.assert_not_called()
        mock_l2_repo.update_state.assert_not_called()

    def test_skipped_mirror_counts_neither_a_success_nor_a_failure(
        self, repo, mock_l2_repo
    ):
        """A skip is not an L2 outcome: the quarantine counter must not move."""
        _degrade_l2(mock_l2_repo)

        with (
            patch.object(repo, "_handle_l2_success") as success,
            patch.object(repo, "_handle_l2_error") as error,
        ):
            repo._sync_to_l2_inline(SVC, _row(CLOSED), keep_open=True)

        success.assert_not_called()
        error.assert_not_called()

    def test_open_snapshot_on_a_degraded_backend_still_mirrors(
        self, repo, mock_l2_repo
    ):
        """An OPEN row can only make the store more restrictive."""
        _degrade_l2(mock_l2_repo)

        with patch.object(repo, "_handle_l2_success"):
            result = repo._sync_to_l2_inline(
                SVC, _row(OPEN, failure_count=5), keep_open=True
            )

        assert result is True
        mock_l2_repo.get_or_create.assert_called_once_with(SVC)
        mock_l2_repo.update_state.assert_called_once()

    def test_closed_snapshot_without_keep_open_on_a_degraded_backend_still_mirrors(
        self, repo, mock_l2_repo
    ):
        """Control: the stand-down is the guarded write's, not every CLOSED write's."""
        _degrade_l2(mock_l2_repo)

        with patch.object(repo, "_handle_l2_success"):
            result = repo._sync_to_l2_inline(SVC, _row(CLOSED))

        assert result is True
        mock_l2_repo.update_state.assert_called_once()

    def test_closed_snapshot_on_a_healthy_backend_mirrors_with_the_guard(
        self, repo, mock_l2_repo
    ):
        """Control: the same guarded write goes through when the backend is live."""
        with patch.object(repo, "_handle_l2_success"):
            result = repo._sync_to_l2_inline(SVC, _row(CLOSED), keep_open=True)

        assert result is True
        assert mock_l2_repo.update_state.call_args.kwargs["keep_open"] is True
