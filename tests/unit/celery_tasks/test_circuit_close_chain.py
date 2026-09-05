"""The on-recovery chain: one task run is one pass, and it continues itself.

Everything here is about the task, not the sweep. The task owns three
decisions the service cannot make:

- **whether this pass may run at all.** The sweep reads no circuit state
  anywhere, and a continuation queued while the circuit was CLOSED is picked up
  seconds later — so the affirmation runs at the start of every pass, from a
  refreshed shared store, through the same projection the drain selects by. A
  peer circuit under a different spelling must stop the chain instead of having
  its backlog walked into a dead dependency one entry at a time.
- **when this pass must stop.** It has to end by RETURNING: the soft-time-limit
  exception is an ordinary Exception, the blanket handler would swallow it into
  an error dict, and the continuation — dispatched after the service call
  returns — would never run.
- **whether anything is left, and whether the pass got closer to it.** Both
  halves are required: reachability alone re-dispatches forever over a pass
  that moved nothing.

The re-dispatch lives here rather than in the service because the service
releases its per-service inflight lock in a ``finally`` — a continuation queued
from inside would meet its own predecessor's lock and end the drain silently.
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from structlog.testing import capture_logs

from baldur.celery_tasks.dlq_tasks import (
    _CIRCUIT_CLOSE_DEADLINE_MARGIN_SECONDS,
    _affirm_circuit_closed,
    _circuit_close_pass_deadline,
    _should_continue_chain,
    conditional_replay_on_circuit_close,
)
from baldur.services.circuit_breaker import CircuitBreakerService
from baldur.services.replay_service import ReplayService
from baldur.services.replay_service.models import BatchReplayResult
from baldur.services.replay_service.service import (
    REASON_CIRCUIT_REOPENED,
    REASON_CONTINUATION_BOUND_REACHED,
    REASON_PASS_ERRORED,
    REASON_PASS_MADE_NO_PROGRESS,
)
from baldur.utils.domain_validation import FALLBACK_DOMAIN

SERVICE = "payment_api"
# A name with no domain identity of its own: it projects onto the shared
# unclassifiable bucket rather than onto a domain.
UNCLASSIFIABLE = "3ds-gateway"


def _cb_service(states, *, calls=None, repository=None):
    """A circuit-breaker service whose L1 view is ``states``."""
    cb = MagicMock(spec=CircuitBreakerService)
    cb.repository = repository if repository is not None else MagicMock(spec=[])
    if calls is not None:

        def _all_states():
            calls.append("get_all_states")
            return states

        cb.get_all_states.side_effect = _all_states
    else:
        cb.get_all_states.return_value = states
    return cb


def _layered_repository(calls=None, *, sync_result=True):
    """A repository that offers the whole-store L2 restore."""
    repo = MagicMock(spec=["force_sync_from_l2", "get_by_service_name"])

    def _sync():
        if calls is not None:
            calls.append("force_sync_from_l2")
        return sync_result

    repo.force_sync_from_l2.side_effect = _sync
    repo.get_by_service_name.return_value = None
    return repo


def _unrefreshable_repository():
    """A layered repository with an L2 configured whose restore failed.

    ``force_sync_from_l2`` returns False for BOTH "no L2 configured" and "the
    load failed", so the health probe is the only thing that separates them.
    """
    repo = MagicMock(
        spec=["force_sync_from_l2", "get_by_service_name", "get_l2_health"]
    )
    repo.force_sync_from_l2.return_value = False
    repo.get_l2_health.return_value = {"adapter_type": "redis"}
    repo.get_by_service_name.return_value = None
    return repo


# =============================================================================
# _affirm_circuit_closed
# =============================================================================


class TestCircuitAffirmationBehavior:
    """Project every circuit forward and require all of them CLOSED."""

    @pytest.mark.parametrize(
        ("states", "expected"),
        [
            ([{"service_name": "payment_api", "state": "closed"}], (True, None)),
            (
                [
                    {"service_name": "Payment-API", "state": "closed"},
                    {"service_name": "payment-api", "state": "open"},
                ],
                (False, "payment-api"),
            ),
            (
                [{"service_name": "payment_api", "state": "half_open"}],
                (False, "payment_api"),
            ),
            # Nothing projects onto the domain: an in-memory circuit store in
            # another process holds none of its rows, so stopping here would
            # stop every sweep on such a deployment before its first pass.
            ([{"service_name": "point_api", "state": "open"}], (True, None)),
            ([], (True, None)),
        ],
    )
    def test_projection_shapes(self, states, expected):
        with patch(
            "baldur.services.circuit_breaker.get_circuit_breaker_service",
            return_value=_cb_service(states),
        ):
            assert _affirm_circuit_closed(SERVICE) == expected

    def test_a_failed_read_proceeds_rather_than_inventing_a_stop(self):
        """The sweep performed no circuit read at all before this affirmation
        existed, so a failed read must reproduce the previous behaviour."""
        with (
            patch(
                "baldur.services.circuit_breaker.get_circuit_breaker_service",
                side_effect=RuntimeError("store unreachable"),
            ),
            capture_logs() as logs,
        ):
            assert _affirm_circuit_closed(SERVICE) == (True, None)

        assert [e for e in logs if e["event"] == "dlq.circuit_affirmation_failed"]

    def test_a_refresh_that_failed_proceeds_but_says_so(self):
        """Proceeding is the decision (the CLOSED event is prior evidence),
        but the pass is about to affirm against a copy the shared store has
        moved past — swallowing that leaves it indistinguishable from a clean
        read."""
        with (
            patch(
                "baldur.services.circuit_breaker.get_circuit_breaker_service",
                return_value=_cb_service(
                    [{"service_name": SERVICE, "state": "closed"}],
                    repository=_unrefreshable_repository(),
                ),
            ),
            capture_logs() as logs,
        ):
            assert _affirm_circuit_closed(SERVICE) == (True, None)

        assert [
            e for e in logs if e["event"] == "dlq.circuit_affirmation_refresh_failed"
        ]

    def test_a_single_layer_store_is_not_reported_as_a_failed_refresh(self):
        """False with no L2 configured is the in-memory store, where L1 IS the
        store — warning on it would fire on every pass of the default topology."""
        repo = MagicMock(spec=["force_sync_from_l2", "get_by_service_name"])
        repo.force_sync_from_l2.return_value = False
        with (
            patch(
                "baldur.services.circuit_breaker.get_circuit_breaker_service",
                return_value=_cb_service(
                    [{"service_name": SERVICE, "state": "closed"}], repository=repo
                ),
            ),
            capture_logs() as logs,
        ):
            assert _affirm_circuit_closed(SERVICE) == (True, None)

        assert not [
            e for e in logs if e["event"] == "dlq.circuit_affirmation_refresh_failed"
        ]

    def test_the_shared_store_is_refreshed_before_it_is_read(self):
        """A worker's L1 is whatever it hydrated at boot plus whatever it has
        touched; a circuit a web pod re-opened would otherwise read CLOSED for
        the whole chain."""
        calls: list[str] = []
        cb = _cb_service(
            [{"service_name": SERVICE, "state": "closed"}],
            calls=calls,
            repository=_layered_repository(calls),
        )

        with patch(
            "baldur.services.circuit_breaker.get_circuit_breaker_service",
            return_value=cb,
        ):
            _affirm_circuit_closed(SERVICE)

        assert calls == ["force_sync_from_l2", "get_all_states"]

    def test_a_store_without_a_restore_seam_is_read_as_is(self):
        cb = _cb_service([{"service_name": SERVICE, "state": "closed"}])

        with patch(
            "baldur.services.circuit_breaker.get_circuit_breaker_service",
            return_value=cb,
        ):
            assert _affirm_circuit_closed(SERVICE) == (True, None)

    def test_get_state_is_never_used(self):
        """It is get-or-create: reading it would fabricate a CLOSED row inside
        the worker for a name it has never seen, and report it healthy."""
        cb = _cb_service([{"service_name": SERVICE, "state": "closed"}])

        with patch(
            "baldur.services.circuit_breaker.get_circuit_breaker_service",
            return_value=cb,
        ):
            _affirm_circuit_closed(SERVICE)

        cb.get_state.assert_not_called()

    def test_an_unclassifiable_name_affirms_its_raw_name_alone(self):
        """The fallback bucket pools unrelated names, so "every circuit
        projecting onto it" would range over strangers."""
        repository = _layered_repository()
        repository.get_by_service_name.return_value = SimpleNamespace(state="open")
        cb = _cb_service([], repository=repository)

        with patch(
            "baldur.services.circuit_breaker.get_circuit_breaker_service",
            return_value=cb,
        ):
            proceed, offending = _affirm_circuit_closed(UNCLASSIFIABLE)

        assert (proceed, offending) == (False, UNCLASSIFIABLE)
        repository.get_by_service_name.assert_called_once_with(UNCLASSIFIABLE)
        cb.get_all_states.assert_not_called()

    def test_an_unclassifiable_name_with_no_row_proceeds(self):
        cb = _cb_service([], repository=_layered_repository())

        with patch(
            "baldur.services.circuit_breaker.get_circuit_breaker_service",
            return_value=cb,
        ):
            assert _affirm_circuit_closed(UNCLASSIFIABLE) == (True, None)

    def test_the_fallback_bucket_is_the_one_this_branch_keys_on(self):
        """Guards the branch against a projection change that would silently
        route unclassifiable names into the many-to-one path."""
        from baldur.utils.domain_validation import resolve_stored_domain

        assert resolve_stored_domain(UNCLASSIFIABLE) == FALLBACK_DOMAIN

    def test_an_empty_projection_is_logged_so_the_permissive_case_is_visible(self):
        with (
            patch(
                "baldur.services.circuit_breaker.get_circuit_breaker_service",
                return_value=_cb_service([]),
            ),
            capture_logs() as logs,
        ):
            _affirm_circuit_closed(SERVICE)

        unknown = [e for e in logs if e["event"] == "dlq.circuit_state_unknown"]
        assert len(unknown) == 1
        assert unknown[0]["healing_domain"] == SERVICE


# =============================================================================
# _circuit_close_pass_deadline
# =============================================================================


class TestPassDeadlineContract:
    """The margin and the precedence order, asserted literally."""

    def test_margin_is_thirty_seconds(self):
        assert _CIRCUIT_CLOSE_DEADLINE_MARGIN_SECONDS == 30

    def test_deadline_is_the_soft_limit_less_the_margin(self):
        task = SimpleNamespace(
            request=SimpleNamespace(timelimit=None), soft_time_limit=290
        )

        deadline = _circuit_close_pass_deadline(task)

        assert deadline == pytest.approx(time.monotonic() + 260, abs=1.0)

    def test_a_per_call_override_wins_over_the_decorator_value(self):
        """It is the limit that would actually kill this pass."""
        task = SimpleNamespace(
            request=SimpleNamespace(timelimit=(600, 120)), soft_time_limit=290
        )

        deadline = _circuit_close_pass_deadline(task)

        assert deadline == pytest.approx(time.monotonic() + 90, abs=1.0)

    def test_an_override_naming_only_a_hard_limit_falls_back_to_the_decorator(self):
        task = SimpleNamespace(
            request=SimpleNamespace(timelimit=(600, None)), soft_time_limit=290
        )

        deadline = _circuit_close_pass_deadline(task)

        assert deadline == pytest.approx(time.monotonic() + 260, abs=1.0)

    def test_no_soft_limit_anywhere_leaves_the_pass_unbounded(self):
        task = SimpleNamespace(
            request=SimpleNamespace(timelimit=None), soft_time_limit=None
        )

        assert _circuit_close_pass_deadline(task) is None

    def test_a_soft_limit_below_the_margin_still_leaves_a_second_to_work(self):
        """A non-positive budget would make every pass return empty, and the
        chain would spin dispatching passes that do nothing."""
        task = SimpleNamespace(
            request=SimpleNamespace(timelimit=None), soft_time_limit=5
        )

        deadline = _circuit_close_pass_deadline(task)

        assert deadline == pytest.approx(time.monotonic() + 1.0, abs=1.0)

    def test_the_shipped_task_declares_the_soft_limit_the_margin_is_sized_for(self):
        assert conditional_replay_on_circuit_close.soft_time_limit == 290


# =============================================================================
# _should_continue_chain
# =============================================================================


def _result(*, capped=False, exhausted=(), total=0, cursors=None):
    return BatchReplayResult(
        total=total,
        capped=capped,
        scan_exhausted_lanes=list(exhausted),
        lane_cursors=dict(cursors or {}),
    )


class TestContinuationPredicateBehavior:
    """Reachability AND progress — either alone is a wrong answer."""

    @pytest.mark.parametrize(
        ("result", "carried", "expected"),
        [
            # Reachable and moved entries.
            (_result(capped=True, total=5), {}, True),
            # Reachable via the scan bound, moved no entries but advanced a
            # cursor past members it examined and rejected.
            (
                _result(exhausted=["TYPE_A|"], cursors={"TYPE_A|": "2.0|b"}),
                {"TYPE_A|": "1.0|a"},
                True,
            ),
            # Nothing reachable: the pass drained what there was.
            (_result(total=5, cursors={"TYPE_A|": "2.0|b"}), {}, False),
            # Reachable but made no progress at all — the shape that would
            # re-dispatch forever.
            (
                _result(capped=True, cursors={"TYPE_A|": "1.0|a"}),
                {"TYPE_A|": "1.0|a"},
                False,
            ),
            # An empty page is neither reachability nor progress.
            (_result(), {}, False),
        ],
    )
    def test_continuation_decision(self, result, carried, expected):
        assert _should_continue_chain(result, carried) is expected

    def test_acquiring_entries_counts_as_progress_even_with_a_static_cursor(self):
        """Every selected entry leaves PENDING before any skip branch runs, so
        none of them is selectable again."""
        result = _result(capped=True, total=3, cursors={"TYPE_A|": "1.0|a"})

        assert _should_continue_chain(result, {"TYPE_A|": "1.0|a"}) is True


# =============================================================================
# The task, end to end
# =============================================================================


class _Chain:
    """One eager task run with the service and the re-dispatch both captured."""

    def __init__(self, result=None, error=None, states=None):
        self.service = MagicMock(spec=ReplayService)
        if error is not None:
            self.service.replay_on_circuit_close.side_effect = error
        else:
            self.service.replay_on_circuit_close.return_value = result
        self.states = (
            states
            if states is not None
            else [{"service_name": SERVICE, "state": "closed"}]
        )
        self.dispatched = MagicMock(spec=conditional_replay_on_circuit_close)

    def run(self, **kwargs):
        params = {"service_name": SERVICE, "max_items": 50, "max_continuations": 10}
        params.update(kwargs)
        with (
            patch("baldur.services.get_replay_service", return_value=self.service),
            patch(
                "baldur.services.circuit_breaker.get_circuit_breaker_service",
                return_value=_cb_service(self.states),
            ),
            patch(
                "baldur.adapters.celery.tasks.conditional_replay_on_circuit_close",
                self.dispatched,
            ),
            capture_logs() as logs,
        ):
            eager = conditional_replay_on_circuit_close.apply(
                kwargs=params, task_id="chain-test"
            )
        self.logs = logs
        return eager.get()


class TestCircuitCloseChainBehavior:
    """What one pass returns, and what it queues behind itself."""

    def test_a_reachable_pass_queues_its_successor_with_counter_and_cursors(self):
        chain = _Chain(_result(capped=True, total=50, cursors={"TYPE_A|": "2.0|a-049"}))

        result = chain.run(continuation=3)

        assert result["continued"] is True
        chain.dispatched.delay.assert_called_once_with(
            service_name=SERVICE,
            max_items=50,
            max_continuations=10,
            continuation=4,
            cursors={"TYPE_A|": "2.0|a-049"},
        )

    def test_the_pass_is_handed_a_deadline_and_the_cursors_it_was_queued_with(self):
        chain = _Chain(_result(total=3))

        chain.run(continuation=2, cursors={"TYPE_A|": "1.0|a-002"})

        kwargs = chain.service.replay_on_circuit_close.call_args.kwargs
        assert kwargs["lane_cursors"] == {"TYPE_A|": "1.0|a-002"}
        assert kwargs["continuation"] == 2
        assert kwargs["deadline"] is not None

    def test_the_last_allowed_pass_announces_the_bound_instead_of_continuing(self):
        chain = _Chain(
            _result(
                capped=True,
                total=50,
                exhausted=["TYPE_A|"],
                cursors={"TYPE_A|": "2.0|a-049"},
            )
        )

        result = chain.run(continuation=9, max_continuations=10)

        assert result["continued"] is False
        chain.dispatched.delay.assert_not_called()
        chain.service.emit_circuit_close_chain_stopped.assert_called_once_with(
            service_name=SERVICE,
            block_reason=REASON_CONTINUATION_BOUND_REACHED,
            scan_exhausted_lanes=["TYPE_A|"],
            lane_cursors={"TYPE_A|": "2.0|a-049"},
        )
        assert [
            e for e in chain.logs if e["event"] == "dlq.circuit_recovery_bound_reached"
        ]

    def test_a_pass_that_reached_work_but_moved_none_of_it_stops_out_loud(self):
        """A pass that spends its whole deadline SELECTING replays nothing and
        advances no cursor, so the chain must stop — a continuation would re-run
        the identical page. Stopping quietly is the failure: `total == 0` makes
        the completion event return early, so the drain would end over a queue
        it never touched with nothing on any operator channel."""
        chain = _Chain(_result(capped=True, total=0, exhausted=["TYPE_A|"]))

        result = chain.run(continuation=0, max_continuations=10)

        assert result["continued"] is False
        chain.dispatched.delay.assert_not_called()
        chain.service.emit_circuit_close_chain_stopped.assert_called_once_with(
            service_name=SERVICE,
            block_reason=REASON_PASS_MADE_NO_PROGRESS,
            scan_exhausted_lanes=["TYPE_A|"],
            lane_cursors={},
        )

    def test_a_finished_drain_stops_without_the_blocked_signal(self):
        """Nothing was reachable, so there is nothing to act on — the signal
        must stay reserved for stops that leave work behind."""
        chain = _Chain(_result(capped=False, total=0))

        chain.run(continuation=0, max_continuations=10)

        chain.service.emit_circuit_close_chain_stopped.assert_not_called()

    def test_a_circuit_reopened_between_dispatch_and_body_runs_no_sweep(self):
        chain = _Chain(
            _result(total=1),
            states=[
                {"service_name": "Payment-API", "state": "closed"},
                {"service_name": "payment-api", "state": "open"},
            ],
        )

        result = chain.run(cursors={"TYPE_A|": "1.0|a"})

        chain.service.replay_on_circuit_close.assert_not_called()
        assert result["success"] is False
        assert result["block_reason"] == REASON_CIRCUIT_REOPENED
        assert result["total"] == 0
        chain.service.emit_circuit_close_chain_stopped.assert_called_once_with(
            service_name=SERVICE,
            block_reason=REASON_CIRCUIT_REOPENED,
            lane_cursors={"TYPE_A|": "1.0|a"},
            offending_circuit="payment-api",
        )

    def test_a_pass_that_raised_announces_the_stop_rather_than_dying_quietly(self):
        """An ERROR log reaches no event, metric or audit consumer, so a chain
        that dies here would be indistinguishable from one that finished."""
        chain = _Chain(error=RuntimeError("selection blew up"))

        result = chain.run(cursors={"TYPE_A|": "1.0|a"})

        assert result["success"] is False
        chain.service.emit_circuit_close_chain_stopped.assert_called_once_with(
            service_name=SERVICE,
            block_reason=REASON_PASS_ERRORED,
            lane_cursors={"TYPE_A|": "1.0|a"},
        )

    def test_a_failing_stop_signal_does_not_mask_the_pass_failure(self):
        chain = _Chain(error=RuntimeError("selection blew up"))
        chain.service.emit_circuit_close_chain_stopped.side_effect = RuntimeError(
            "bus down"
        )

        result = chain.run()

        assert result["success"] is False
        assert "selection blew up" in result["error"]

    def test_a_drained_chain_runs_one_more_pass_with_its_cursors_cleared(self):
        """The stale-replay release returns abandoned entries to PENDING at
        their ORIGINAL created_at — behind every cursor a running chain holds."""
        chain = _Chain(_result(total=2, cursors={"TYPE_A|": "2.0|a-001"}))

        result = chain.run(continuation=1, cursors={"TYPE_A|": "1.0|a-000"})

        assert result["continued"] is True
        chain.dispatched.delay.assert_called_once_with(
            service_name=SERVICE,
            max_items=50,
            max_continuations=10,
            continuation=2,
            cursors=None,
        )

    def test_a_cursorless_pass_with_nothing_reachable_ends_the_chain(self):
        """The successor of the reset pass carries no cursors, so it cannot
        ask for another reset — that is what bounds the retry."""
        chain = _Chain(_result(total=2))

        result = chain.run(continuation=2)

        assert result["continued"] is False
        chain.dispatched.delay.assert_not_called()
        chain.service.emit_circuit_close_chain_stopped.assert_not_called()

    def test_a_pass_the_inflight_lock_rejected_queues_nothing(self):
        """Its predecessor is still running and owns the continuation."""
        chain = _Chain(BatchReplayResult(inflight_skipped=True, capped=True))

        result = chain.run()

        assert result["continued"] is False
        chain.dispatched.delay.assert_not_called()

    def test_a_governance_block_ends_the_pass_before_any_re_dispatch(self):
        chain = _Chain(
            BatchReplayResult(
                governance_blocked=True,
                governance_block_reason="emergency_mode_active",
                capped=True,
            )
        )

        result = chain.run()

        assert result["success"] is False
        chain.dispatched.delay.assert_not_called()

    def test_the_completion_log_carries_the_pass_counter(self):
        """A chain's passes are otherwise indistinguishable in the log."""
        chain = _Chain(_result(total=4))

        chain.run(continuation=7)

        completed = [
            e for e in chain.logs if e["event"] == "dlq.circuit_recovery_completed"
        ]
        assert len(completed) == 1
        assert completed[0]["continuation"] == 7
