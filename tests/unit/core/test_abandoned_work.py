"""Work scopes — a keyed call's hold on the work a timeout abandoned.

A scope keeps only the pieces still running and a summary of its own finished
work; it settles exactly once, when it is closed and nothing it holds runs.
Pieces recorded after the close (work abandoned inside abandoned work) extend
the hold; a settled scope refuses every piece. A piece is the call's own work
only when recorded with the very origin object the scope was opened with, and
``None`` is never own work.

Verification techniques applied:
- State transition: open -> closed (holding) -> settled; settled refuses.
- Concurrency race: the close and the last piece's end race under a barrier;
  the scope settles exactly once.
- Branch coverage: own vs other (identity, ``None``), success / exception /
  cancel, the nested close only on the current chain, a foreign-context reset.
- Contract: ``WorkSummary`` defaults and ``own_succeeded``.
"""

from __future__ import annotations

import contextvars
import threading
from concurrent.futures import Future
from typing import Any

import pytest
from structlog.testing import capture_logs

from baldur.core.abandoned_work import (
    WorkSummary,
    close_work_scope,
    current_work_scope,
    open_work_scope,
    record_abandoned,
)

_WAIT_S = 5.0


class _SettleSpy:
    """Records every summary a scope settles with."""

    def __init__(self) -> None:
        self.summaries: list[WorkSummary] = []

    def __call__(self, summary: WorkSummary) -> None:
        self.summaries.append(summary)


class _AlwaysEqual:
    """Equal to everything, identical to nothing else."""

    def __eq__(self, other: object) -> bool:
        return True

    __hash__ = object.__hash__


@pytest.fixture(autouse=True)
def _no_scope_leaks():
    """Every test starts and ends with no scope current."""
    assert current_work_scope() is None
    yield
    assert current_work_scope() is None


def _finish(future: Future[Any], outcome: str) -> None:
    if outcome == "returned":
        future.set_result("done")
    elif outcome == "raised":
        future.set_exception(ValueError("declined"))
    else:
        assert future.cancel() is True


class TestWorkSummaryContract:
    """How a scope's own abandoned work ended."""

    def test_defaults_report_no_own_work(self):
        summary = WorkSummary()

        assert summary.own_finished is False
        assert summary.own_failed is False
        assert summary.own_succeeded is False

    @pytest.mark.parametrize(
        ("own_finished", "own_failed", "own_succeeded"),
        [(True, False, True), (True, True, False), (False, False, False)],
        ids=["finished_clean", "finished_failed", "no_own_work"],
    )
    def test_own_succeeded_requires_finished_and_not_failed(
        self, own_finished, own_failed, own_succeeded
    ):
        summary = WorkSummary(own_finished=own_finished, own_failed=own_failed)

        assert summary.own_succeeded is own_succeeded


class TestAbandonedWorkScopeBehavior:
    """Open, record, fold, close and settle."""

    @pytest.mark.parametrize(
        ("outcome", "expected"),
        [
            ("returned", WorkSummary(own_finished=True, own_failed=False)),
            ("raised", WorkSummary(own_finished=True, own_failed=True)),
            ("cancelled", WorkSummary(own_finished=True, own_failed=True)),
        ],
        ids=["returned", "raised", "cancelled"],
    )
    def test_own_piece_outcome_decides_summary(self, outcome, expected):
        """A held scope settles, once, with how its own piece ended."""
        # Given — a scope holding its own running piece, closed.
        origin = object()
        scope, token = open_work_scope(origin=origin)
        piece: Future[Any] = Future()
        record_abandoned(piece, origin=origin)
        spy = _SettleSpy()
        assert close_work_scope(scope, token, spy) is None

        # When
        _finish(piece, outcome)

        # Then
        assert spy.summaries == [expected]
        assert scope.settled is True

    @pytest.mark.parametrize(
        ("scope_origin", "piece_origin"),
        [
            ("context", "other"),
            ("context", None),
            (None, None),
            ("context", "equal_not_identical"),
        ],
        ids=["other_origin", "piece_none", "both_none", "equal_not_identical"],
    )
    def test_non_own_piece_extends_hold_without_deciding(
        self, scope_origin, piece_origin
    ):
        """Other work only holds: the settled summary reports no own work."""
        origins = {"context": _AlwaysEqual(), None: None}
        opened_with = origins[scope_origin]
        recorded_with: Any = {
            "other": object(),
            None: None,
            "equal_not_identical": _AlwaysEqual(),
        }[piece_origin]
        scope, token = open_work_scope(origin=opened_with)
        piece: Future[Any] = Future()
        record_abandoned(piece, origin=recorded_with)
        spy = _SettleSpy()

        held = close_work_scope(scope, token, spy)
        piece.set_result("done")

        assert held is None
        assert spy.summaries == [WorkSummary()]

    def test_close_with_nothing_running_returns_summary_at_once(self):
        origin = object()
        scope, token = open_work_scope(origin=origin)
        piece: Future[Any] = Future()
        record_abandoned(piece, origin=origin)
        piece.set_result("done")
        spy = _SettleSpy()

        summary = close_work_scope(scope, token, spy)

        assert summary == WorkSummary(own_finished=True, own_failed=False)
        assert spy.summaries == []
        assert scope.settled is True

    def test_never_closed_scope_retains_only_running_pieces(self):
        """Finished pieces are folded and dropped even without a close."""
        scope, token = open_work_scope(origin=None)
        pieces: list[Future[Any]] = [Future() for _ in range(3)]
        for piece in pieces:
            record_abandoned(piece)

        pieces[0].set_result("done")
        pieces[1].set_exception(ValueError("declined"))

        assert scope.running_count == 1
        pieces[2].set_result("done")
        assert scope.running_count == 0
        assert close_work_scope(scope, token) == WorkSummary()

    def test_piece_recorded_after_close_extends_hold(self):
        """Work abandoned inside a still-running piece joins the closed scope."""
        # Given — the caller's context, as a still-running piece inherited it.
        origin = object()
        scope, token = open_work_scope(origin=origin)
        inherited = contextvars.copy_context()
        first: Future[Any] = Future()
        record_abandoned(first, origin=origin)
        spy = _SettleSpy()
        assert close_work_scope(scope, token, spy) is None

        # When — the running piece abandons work of its own, then ends.
        nested: Future[Any] = Future()
        inherited.run(record_abandoned, nested, None)
        first.set_result("done")

        # Then — the hold lasts until the nested work ends too.
        assert spy.summaries == []
        assert scope.running_count == 1
        nested.set_exception(ValueError("declined"))
        assert spy.summaries == [WorkSummary(own_finished=True, own_failed=False)]

    def test_scope_settled_by_last_piece_refuses_new_pieces(self):
        """Once the last held piece settled the scope, nothing it spawned joins."""
        scope, token = open_work_scope(origin=None)
        inherited = contextvars.copy_context()
        held: Future[Any] = Future()
        record_abandoned(held)
        spy = _SettleSpy()
        assert close_work_scope(scope, token, spy) is None
        held.set_result("done")
        late: Future[Any] = Future()

        inherited.run(record_abandoned, late, None)

        assert spy.summaries == [WorkSummary()]
        assert scope.running_count == 0
        assert late._done_callbacks == []

    def test_settled_scope_refuses_new_pieces(self):
        scope, token = open_work_scope(origin=None)
        inherited = contextvars.copy_context()
        close_work_scope(scope, token)
        late: Future[Any] = Future()

        inherited.run(record_abandoned, late, None)

        assert scope.running_count == 0
        assert late._done_callbacks == []

    def test_record_outside_any_scope_registers_nothing(self):
        piece: Future[Any] = Future()

        record_abandoned(piece, origin=object())

        assert piece._done_callbacks == []

    def test_record_reaches_every_unsettled_scope_in_chain(self):
        outer_origin, inner_origin = object(), object()
        outer, outer_token = open_work_scope(origin=outer_origin)
        inner, inner_token = open_work_scope(origin=inner_origin)
        piece: Future[Any] = Future()

        record_abandoned(piece, origin=inner_origin)

        assert (outer.running_count, inner.running_count) == (1, 1)
        inner_spy, outer_spy = _SettleSpy(), _SettleSpy()
        assert close_work_scope(inner, inner_token, inner_spy) is None
        assert close_work_scope(outer, outer_token, outer_spy) is None
        piece.set_result("done")
        assert inner_spy.summaries == [WorkSummary(own_finished=True)]
        assert outer_spy.summaries == [WorkSummary()]

    def test_close_and_last_piece_end_racing_settle_exactly_once(self):
        """Fold vs close under a barrier: one settle per scope, every time."""
        settles: list[int] = []
        for _ in range(200):
            # Given
            origin = object()
            scope, token = open_work_scope(origin=origin)
            piece: Future[Any] = Future()
            record_abandoned(piece, origin=origin)
            spy = _SettleSpy()
            barrier = threading.Barrier(2)
            closed: list[WorkSummary | None] = []

            def _finisher(
                piece: Future[Any] = piece, barrier: threading.Barrier = barrier
            ) -> None:
                barrier.wait(_WAIT_S)
                piece.set_result("done")

            finisher = threading.Thread(target=_finisher, daemon=True)
            finisher.start()

            # When
            barrier.wait(_WAIT_S)
            closed.append(close_work_scope(scope, token, spy))
            finisher.join(_WAIT_S)

            # Then — the close returned the summary, or the fold settled it.
            settles.append(len(spy.summaries) + (closed[0] is not None))
        assert set(settles) == {1}

    def test_close_of_outer_closes_nested_scope_left_current(self):
        """A nested call cancelled before its own close is closed with the outer one."""
        outer, outer_token = open_work_scope(origin=object())
        inner, _inner_token = open_work_scope(origin=object())

        close_work_scope(outer, outer_token)

        assert inner.settled is True
        assert outer.settled is True
        assert current_work_scope() is None

    def test_stale_scope_close_leaves_running_enclosing_scope_open(self):
        """A scope that is not on the current chain never closes the current one."""
        # Given — a scope opened in another context, and a live current scope.
        stale_context = contextvars.copy_context()
        stale, _stale_token = stale_context.run(open_work_scope, None)
        live, live_token = open_work_scope(origin=None)

        # When
        close_work_scope(stale, None)

        # Then — the live scope still holds what it records.
        piece: Future[Any] = Future()
        record_abandoned(piece)
        assert live.settled is False
        assert live.running_count == 1
        piece.set_result("done")
        close_work_scope(live, live_token)

    def test_foreign_context_token_reset_is_skipped(self):
        """A token from another context does not raise; the scope still closes."""
        other_context = contextvars.copy_context()
        scope, token = other_context.run(open_work_scope, None)

        summary = close_work_scope(scope, token)

        assert summary == WorkSummary()
        assert scope.settled is True

    def test_current_work_scope_returns_innermost_then_parent(self):
        outer, outer_token = open_work_scope(origin=None)
        inner, inner_token = open_work_scope(origin=None)

        assert current_work_scope() is inner
        assert inner.parent is outer
        close_work_scope(inner, inner_token)
        assert current_work_scope() is outer
        close_work_scope(outer, outer_token)

    def test_settle_action_raising_is_logged_not_propagated(self):
        """The fold catches a settle action's error and logs it."""
        scope, token = open_work_scope(origin=None)
        piece: Future[Any] = Future()
        record_abandoned(piece)

        def _raising(_summary: WorkSummary) -> None:
            raise RuntimeError("ledger down")

        assert close_work_scope(scope, token, _raising) is None
        with capture_logs() as logs:
            piece.set_result("done")

        failures = [log for log in logs if log["event"] == "abandoned_work.fold_failed"]
        assert len(failures) == 1
        assert failures[0]["log_level"] == "warning"
        assert failures[0]["error_type"] == "RuntimeError"
