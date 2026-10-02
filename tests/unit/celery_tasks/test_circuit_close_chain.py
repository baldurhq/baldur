"""The on-recovery chain: one task run is one pass, and it continues itself.

Everything here is about the task, not the sweep. The task owns three
decisions the service cannot make:

- **whether this pass may run at all.** The sweep reads no circuit state
  anywhere, and a continuation queued while the circuit was CLOSED is picked up
  seconds later — so the affirmation runs at the start of every pass, from the
  shared store's fleet read, through the same projection the drain selects by. A
  peer circuit under a different spelling must stop the chain instead of having
  its backlog walked into a dead dependency one entry at a time, and an
  operator's manual pin holds the chain quietly.
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
import uuid
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, create_autospec, patch

import pytest
from structlog.testing import capture_logs

from baldur.adapters.cache.memory_adapter import InMemoryCacheAdapter
from baldur.adapters.memory import InMemoryFailedOperationRepository
from baldur.celery_tasks.dlq_tasks import (
    _CHAIN_REQUEUE_SECONDS,
    _CIRCUIT_CLOSE_DEADLINE_MARGIN_SECONDS,
    _affirm_circuit_closed,
    _operator_requeue_bound,
    _pass_deadline,
    _should_continue_chain,
    conditional_replay_on_circuit_close,
    recover_parked_jobs,
)
from baldur.core.exceptions import DLQError
from baldur.interfaces.governance import GovernanceChecker
from baldur.interfaces.repositories import (
    CircuitBreakerStateData,
    FailedOperationData,
    FailedOperationRepository,
)
from baldur.models.governance import GovernanceCheckResult
from baldur.services.circuit_breaker import CircuitBreakerService
from baldur.services.circuit_breaker.exceptions import (
    UNREACHED_DEFAULT_STORE_REASON,
    CircuitBreakerStateUnavailableError,
)
from baldur.services.event_bus.bus.event_bus import BaldurEventBus
from baldur.services.event_bus.bus.event_types import EventType
from baldur.services.replay_service import ReplayService
from baldur.services.replay_service.handlers import (
    ReplayHandler,
    _replay_handlers,
    register_replay_handler,
)
from baldur.services.replay_service.models import BatchReplayResult, ReplayResult
from baldur.services.replay_service.recovery import RecoveryTickResult, TrialRecord
from baldur.services.replay_service.service import (
    REASON_CIRCUIT_REOPENED,
    REASON_CONTINUATION_BOUND_REACHED,
    REASON_INTEGRITY_BLOCKED,
    REASON_NO_REPLAY_HANDLER,
    REASON_OPERATOR_HOLD,
    REASON_PASS_ERRORED,
    REASON_PASS_MADE_NO_PROGRESS,
)
from baldur.utils.domain_validation import FALLBACK_DOMAIN, resolve_stored_domain
from baldur.utils.time import utc_now
from tests.factories.replay_doubles import ScriptedReplayHandler

SERVICE = "payment_api"
# A name with no domain identity of its own: it projects onto the shared
# unclassifiable bucket rather than onto a domain.
UNCLASSIFIABLE = "3ds-gateway"


# The keyword arguments every pass of a CLOSED-event chain carries forward.
_CHAIN_DEFAULTS = {
    "trigger": "auto_replay_circuit_close",
    "escalate_failures": True,
    "operator_requested": False,
    "rescanned": False,
}


def _row(service_name, state, **fields):
    return CircuitBreakerStateData(service_name=service_name, state=state, **fields)


def _rows(states):
    """Breaker rows from ``{"service_name", "state"}`` shapes (or full rows)."""
    return [
        state
        if isinstance(state, CircuitBreakerStateData)
        else _row(state["service_name"], state["state"])
        for state in states
    ]


def _cb_service(states, *, calls=None, repository=None):
    """A circuit-breaker service whose shared store holds ``states``."""
    cb = MagicMock(spec=CircuitBreakerService)
    if repository is None:
        repository = MagicMock(spec=["get_cluster_states", "get_all_states"])

        def _cluster_states():
            if calls is not None:
                calls.append("get_cluster_states")
            return _rows(states)

        repository.get_cluster_states.side_effect = _cluster_states
    cb.repository = repository
    cb.refuses_calls.side_effect = lambda row: row.state == "open"
    return cb


# =============================================================================
# _affirm_circuit_closed
# =============================================================================


class TestCircuitAffirmationBehavior:
    """Project every circuit forward and judge it by the chain's rule."""

    @pytest.mark.parametrize(
        ("states", "expected"),
        [
            ([{"service_name": "payment_api", "state": "closed"}], (None, None)),
            (
                [
                    {"service_name": "Payment-API", "state": "closed"},
                    {"service_name": "payment-api", "state": "open"},
                ],
                (REASON_CIRCUIT_REOPENED, "payment-api"),
            ),
            (
                [{"service_name": "payment_api", "state": "half_open"}],
                (REASON_CIRCUIT_REOPENED, "payment_api"),
            ),
            # Nothing projects onto the domain: an in-memory circuit store in
            # another process holds none of its rows, so stopping here would
            # stop every sweep on such a deployment before its first pass.
            ([{"service_name": "point_api", "state": "open"}], (None, None)),
            ([], (None, None)),
        ],
    )
    def test_projection_shapes(self, states, expected):
        with patch(
            "baldur.services.circuit_breaker.get_circuit_breaker_service",
            return_value=_cb_service(states),
        ):
            reason, row = _affirm_circuit_closed(SERVICE)

        assert (reason, row.service_name if row is not None else None) == expected

    def test_a_failed_read_proceeds_rather_than_inventing_a_stop(self):
        """The sweep performed no circuit read at all before this affirmation
        existed, so a failed read must reproduce the previous behaviour."""
        repository = MagicMock(spec=["get_cluster_states"])
        repository.get_cluster_states.side_effect = CircuitBreakerStateUnavailableError(
            "get_cluster_states", "backend_degraded"
        )
        with (
            patch(
                "baldur.services.circuit_breaker.get_circuit_breaker_service",
                return_value=_cb_service([], repository=repository),
            ),
            capture_logs() as logs,
        ):
            assert _affirm_circuit_closed(SERVICE) == (None, None)

        assert [e for e in logs if e["event"] == "dlq.circuit_affirmation_failed"]

    def test_an_unreached_default_store_reads_this_process_s_rows(self):
        """Nobody named a shared store: this process's view is the cluster, so
        the read is not a failure and nothing is reported."""
        repository = MagicMock(spec=["get_cluster_states", "get_all_states"])
        repository.get_cluster_states.side_effect = CircuitBreakerStateUnavailableError(
            "get_cluster_states", UNREACHED_DEFAULT_STORE_REASON
        )
        repository.get_all_states.return_value = [_row(SERVICE, "open")]
        with (
            patch(
                "baldur.services.circuit_breaker.get_circuit_breaker_service",
                return_value=_cb_service([], repository=repository),
            ),
            capture_logs() as logs,
        ):
            reason, row = _affirm_circuit_closed(SERVICE)

        assert (reason, row.service_name) == (REASON_CIRCUIT_REOPENED, SERVICE)
        assert not [e for e in logs if e["event"] == "dlq.circuit_affirmation_failed"]

    def test_the_fleet_read_is_the_one_read(self):
        """A worker's local copy is whatever it hydrated at boot plus whatever
        it has touched; the pass reads the shared store itself."""
        calls: list[str] = []
        cb = _cb_service([{"service_name": SERVICE, "state": "closed"}], calls=calls)

        with patch(
            "baldur.services.circuit_breaker.get_circuit_breaker_service",
            return_value=cb,
        ):
            _affirm_circuit_closed(SERVICE)

        assert calls == ["get_cluster_states"]
        cb.get_all_states.assert_not_called()

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
        cb = _cb_service(
            [
                {"service_name": UNCLASSIFIABLE, "state": "open"},
                {"service_name": "other-gateway", "state": "open"},
            ]
        )

        with patch(
            "baldur.services.circuit_breaker.get_circuit_breaker_service",
            return_value=cb,
        ):
            reason, row = _affirm_circuit_closed(UNCLASSIFIABLE)

        assert (reason, row.service_name) == (REASON_CIRCUIT_REOPENED, UNCLASSIFIABLE)

    def test_an_unclassifiable_name_with_no_row_proceeds(self):
        cb = _cb_service([])

        with patch(
            "baldur.services.circuit_breaker.get_circuit_breaker_service",
            return_value=cb,
        ):
            assert _affirm_circuit_closed(UNCLASSIFIABLE) == (None, None)

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
# _pass_deadline
# =============================================================================


class TestPassDeadlineContract:
    """The margin and the precedence order, asserted literally."""

    def test_margin_is_thirty_seconds(self):
        assert _CIRCUIT_CLOSE_DEADLINE_MARGIN_SECONDS == 30

    def test_deadline_is_the_soft_limit_less_the_margin(self):
        task = SimpleNamespace(
            request=SimpleNamespace(timelimit=None), soft_time_limit=290
        )

        deadline = _pass_deadline(task)

        assert deadline == pytest.approx(time.monotonic() + 260, abs=1.0)

    def test_a_per_call_override_wins_over_the_decorator_value(self):
        """It is the limit that would actually kill this pass."""
        task = SimpleNamespace(
            request=SimpleNamespace(timelimit=(600, 120)), soft_time_limit=290
        )

        deadline = _pass_deadline(task)

        assert deadline == pytest.approx(time.monotonic() + 90, abs=1.0)

    def test_an_override_naming_only_a_hard_limit_falls_back_to_the_decorator(self):
        task = SimpleNamespace(
            request=SimpleNamespace(timelimit=(600, None)), soft_time_limit=290
        )

        deadline = _pass_deadline(task)

        assert deadline == pytest.approx(time.monotonic() + 260, abs=1.0)

    def test_no_soft_limit_anywhere_leaves_the_pass_unbounded(self):
        task = SimpleNamespace(
            request=SimpleNamespace(timelimit=None), soft_time_limit=None
        )

        assert _pass_deadline(task) is None

    def test_a_soft_limit_below_the_margin_still_leaves_a_second_to_work(self):
        """A non-positive budget would make every pass return empty, and the
        chain would spin dispatching passes that do nothing."""
        task = SimpleNamespace(
            request=SimpleNamespace(timelimit=None), soft_time_limit=5
        )

        deadline = _pass_deadline(task)

        assert deadline == pytest.approx(time.monotonic() + 1.0, abs=1.0)

    def test_the_shipped_task_declares_the_soft_limit_the_margin_is_sized_for(self):
        assert conditional_replay_on_circuit_close.soft_time_limit == 290


# =============================================================================
# _should_continue_chain
# =============================================================================


def _result(*, capped=False, exhausted=(), total=0, cursors=None, cut=None):
    return BatchReplayResult(
        total=total,
        capped=capped,
        scan_exhausted_lanes=list(exhausted),
        lane_cursors=dict(cursors or {}),
        deadline_cut_dlq_id=cut,
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

    def test_a_deadline_cut_counts_as_progress_with_nothing_completed(self):
        """A pass whose first replay the deadline cut completed nothing and left
        every cursor where it was — but the cut used one of that entry's replay
        attempts, so the chain moves on toward the entry's cap."""
        result = _result(capped=True, total=0, cursors={"TYPE_A|": "1.0|a"}, cut="7")

        assert _should_continue_chain(result, {"TYPE_A|": "1.0|a"}) is True


# =============================================================================
# The task, end to end
# =============================================================================


class _Chain:
    """One eager task run with the service and the re-dispatch both captured."""

    def __init__(
        self, result=None, error=None, states=None, service=None, integrity=True
    ):
        self.integrity = integrity
        if service is not None:
            # A real service: the pass-start decision is the production one.
            self.service = service
        else:
            self.service = MagicMock(spec=ReplayService)
            self.service.recovery_is_idle.return_value = False
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
                "baldur.services.event_bus.integrity_gate.replay_integrity_verdict",
                return_value=self.integrity,
            ) as verdict,
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
        self.verdict = verdict
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
            **_CHAIN_DEFAULTS,
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

    def test_a_pass_whose_first_replay_was_cut_queues_the_replay_of_it(self):
        """The cut entry stays PENDING behind unchanged cursors; the successor
        is queued with those cursors, so it selects the cut entry first."""
        chain = _Chain(
            _result(capped=True, total=0, cursors={"TYPE_A|": "1.0|a"}, cut="7")
        )

        result = chain.run(
            continuation=0, max_continuations=10, cursors={"TYPE_A|": "1.0|a"}
        )

        assert result["continued"] is True
        chain.dispatched.delay.assert_called_once_with(
            service_name=SERVICE,
            max_items=50,
            max_continuations=10,
            continuation=1,
            cursors={"TYPE_A|": "1.0|a"},
            **_CHAIN_DEFAULTS,
        )
        chain.service.emit_circuit_close_chain_stopped.assert_not_called()

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
            **{**_CHAIN_DEFAULTS, "rescanned": True},
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


# =============================================================================
# The pass-start check: a recovery with no lane and nothing parked (809 D1)
# =============================================================================

_AFFIRM_PATH = "baldur.celery_tasks.dlq_tasks._affirm_circuit_closed"


def _real_service(parked) -> ReplayService:
    """A real replay service over a repository whose strict count answers
    ``parked`` (or raises it)."""
    repository = create_autospec(FailedOperationRepository, instance=True)
    if isinstance(parked, Exception):
        repository.get_cluster_pending_count_by_domain.side_effect = parked
    else:
        repository.get_cluster_pending_count_by_domain.return_value = parked
    service = ReplayService(repository=repository, cache=InMemoryCacheAdapter())
    service._event_bus = MagicMock(spec=BaldurEventBus)
    return service


def _runtime_map(value):
    return patch.object(
        ReplayService, "_load_failure_type_map", autospec=True, return_value=value
    )


class TestCircuitRecoveryIdlePassBehavior:
    """An idle pass ends before anything that can only report a stop of nothing."""

    def test_an_idle_pass_ends_before_the_circuit_read_and_the_sweep(self):
        chain = _Chain()
        chain.service.recovery_is_idle.return_value = True

        with patch(_AFFIRM_PATH, autospec=True) as affirm:
            result = chain.run(continuation=0)

        assert result == {
            "success": True,
            "service_name": SERVICE,
            "total": 0,
            "nothing_parked": True,
        }
        chain.service.recovery_is_idle.assert_called_once_with(SERVICE)
        affirm.assert_not_called()
        chain.service.replay_on_circuit_close.assert_not_called()
        chain.service.emit_circuit_close_chain_stopped.assert_not_called()
        chain.dispatched.delay.assert_not_called()

    def test_an_idle_pass_logs_one_info_completion_and_nothing_louder(self):
        """The only trace of a finished no-lane recovery is an INFO line."""
        chain = _Chain()
        chain.service.recovery_is_idle.return_value = True

        chain.run(continuation=4)

        completed = [
            e for e in chain.logs if e["event"] == "dlq.circuit_recovery_completed"
        ]
        assert len(completed) == 1
        assert completed[0]["log_level"] == "info"
        assert completed[0]["nothing_parked"] is True
        assert completed[0]["dlq_total"] == 0
        assert completed[0]["continuation"] == 4
        assert [e for e in chain.logs if e["log_level"] not in ("debug", "info")] == []

    def test_a_pass_that_is_not_idle_runs_the_affirmation_and_the_sweep(self):
        chain = _Chain(_result(total=1))

        with patch(_AFFIRM_PATH, autospec=True, return_value=(None, None)) as affirm:
            result = chain.run()

        affirm.assert_called_once_with(
            SERVICE, trigger="auto_replay_circuit_close", operator_requested=False
        )
        chain.service.replay_on_circuit_close.assert_called_once()
        assert "nothing_parked" not in result

    def test_a_pass_carrying_positions_runs_even_when_idle_and_queues_the_cleared_pass(
        self,
    ):
        """An earlier pass of the chain had a lane; its cleared-positions
        successor may land on a worker that has one too."""
        # Given: the no-lane result a pass with nothing to select returns.
        chain = _Chain(BatchReplayResult())
        chain.service.recovery_is_idle.return_value = True

        # When
        result = chain.run(continuation=1, cursors={"OPEN_CIRCUIT|payment_api": "c"})

        # Then
        chain.service.recovery_is_idle.assert_not_called()
        chain.service.replay_on_circuit_close.assert_called_once()
        assert result["continued"] is True
        chain.dispatched.delay.assert_called_once_with(
            service_name=SERVICE,
            max_items=50,
            max_continuations=10,
            continuation=2,
            cursors=None,
            **{**_CHAIN_DEFAULTS, "rescanned": True},
        )

    def test_a_mapped_name_without_a_handler_and_nothing_parked_is_idle(self):
        """Every lane, a mapped one included, is scoped to the name's own domain
        and needs its replay handler: with neither a handler nor anything parked
        the pass ends before the circuit read and the sweep."""
        service = _real_service(0)
        chain = _Chain(service=service)

        with (
            _runtime_map({SERVICE: ["TIMEOUT"]}),
            patch.object(
                ReplayService,
                "replay_on_circuit_close",
                autospec=True,
                return_value=BatchReplayResult(),
            ) as sweep,
            patch(_AFFIRM_PATH, autospec=True, return_value=(None, None)) as affirm,
        ):
            result = chain.run()

        affirm.assert_not_called()
        sweep.assert_not_called()
        assert result["nothing_parked"] is True

    def test_a_count_that_raises_runs_the_pass_and_the_sweep_reports_pending_none(
        self,
    ):
        """An unanswerable count keeps today's path end to end: the pass
        runs, and the no-lane sweep raises the blocked surface without a count."""
        service = _real_service(DLQError("redis_inactive"))
        chain = _Chain(service=service)

        with (
            _runtime_map({}),
            patch(_AFFIRM_PATH, autospec=True, return_value=(None, None)) as affirm,
            patch(
                "baldur.services.replay_service.service.log_dlq_replay_blocked_audit",
                autospec=True,
            ),
        ):
            chain.run()

        affirm.assert_called_once_with(
            SERVICE, trigger="auto_replay_circuit_close", operator_requested=False
        )
        blocked = [
            c.kwargs["data"]
            for c in service._event_bus.emit.call_args_list
            if c.args[0] == EventType.DLQ_REPLAY_BLOCKED
        ]
        assert len(blocked) == 1
        assert blocked[0]["pending"] is None
        assert blocked[0]["block_reason"] == REASON_NO_REPLAY_HANDLER

    def test_an_idle_check_that_raises_runs_the_pass_and_reports_it_errored(self):
        """A handler whose declared failure types cannot be read as lanes
        breaks lane resolution. The pass-start check must not let that escape
        the task with no signal: the pass runs, and the sweep's own failure is
        reported as an errored pass, as before the check existed."""
        # Given: a registered handler declaring an unhashable failure type.
        service = _real_service(0)
        chain = _Chain(service=service)
        register_replay_handler(_UnhashableDeclaringHandler())

        # When
        try:
            with (
                _runtime_map({}),
                patch(_AFFIRM_PATH, autospec=True, return_value=(None, None)) as affirm,
                patch(
                    "baldur.services.replay_service.service.log_dlq_replay_blocked_audit",
                    autospec=True,
                ),
            ):
                result = chain.run()
        finally:
            _replay_handlers.pop(resolve_stored_domain(SERVICE), None)

        # Then
        affirm.assert_called_once_with(
            SERVICE, trigger="auto_replay_circuit_close", operator_requested=False
        )
        assert result["success"] is False
        assert "nothing_parked" not in result
        blocked = [
            c.kwargs["data"]
            for c in service._event_bus.emit.call_args_list
            if c.args[0] == EventType.DLQ_REPLAY_BLOCKED
        ]
        assert [event["block_reason"] for event in blocked] == [REASON_PASS_ERRORED]


class _UnhashableDeclaringHandler(ReplayHandler):
    """A handler whose declared failure types hold a list, not strings."""

    @property
    def domain(self) -> str:
        return SERVICE

    @property
    def auto_replay_failure_types(self):
        return (["MAX_RETRIES_TIMEOUTERROR"],)

    def can_replay(self, failed_op: FailedOperationData) -> tuple[bool, str]:
        return True, ""

    def replay(self, failed_op: FailedOperationData) -> ReplayResult:
        return ReplayResult.succeeded(failed_op.id, "done")


# =============================================================================
# 807 — a recovery chain's pauses, holds and stops
# =============================================================================

_LANE = "MAX_RETRIES_TIMEOUTERROR|payment_api"


def _pinned(name, state, *, expires_in=timedelta(minutes=30)):
    """A row under an operator's manual pin (a Block when OPEN)."""
    return _row(
        name,
        state,
        opened_at=utc_now() - timedelta(minutes=10) if state == "open" else None,
        manually_controlled=True,
        manual_override_expires_at=utc_now() + expires_in,
    )


def _refusal_ended(cursors=None):
    """A pass its job's own breaker ended on the first replay it tried."""
    result = _result(capped=True, total=0, cursors=cursors or {_LANE: "1.0|a"}, cut="7")
    result.ended_by_breaker_refusal = True
    return result


class TestCircuitCloseChainRecoveryContract:
    """The pause and the operator's re-queue bound, asserted literally."""

    def test_chain_requeue_pause_is_thirty_seconds(self):
        assert _CHAIN_REQUEUE_SECONDS == 30

    def test_operator_requeue_bound_outlasts_the_default_lock_ttl(self):
        """ceil(300 / 30) + 1: a holder that died lets its lock expire first."""
        assert _operator_requeue_bound() == 11


class TestCircuitCloseChainRecoveryBehavior:
    """A refusal pauses the chain; an operator's chain waits; holds stop quietly."""

    def test_refused_pass_countdown_queues_the_next_pass_after_the_pause(self):
        chain = _Chain(_refusal_ended())

        result = chain.run(continuation=2, cursors={_LANE: "1.0|a"})

        assert result["continued"] is True
        chain.dispatched.delay.assert_not_called()
        chain.dispatched.apply_async.assert_called_once_with(
            kwargs={
                "service_name": SERVICE,
                "max_items": 50,
                "max_continuations": 10,
                "continuation": 3,
                "cursors": {_LANE: "1.0|a"},
                **_CHAIN_DEFAULTS,
            },
            countdown=_CHAIN_REQUEUE_SECONDS,
        )

    @pytest.mark.parametrize(
        "result",
        [
            _result(capped=True, total=0, cursors={_LANE: "1.0|a"}, cut="7"),
            _result(capped=True, total=50, cursors={_LANE: "2.0|b"}),
        ],
        ids=["deadline_cut", "ordinary"],
    )
    def test_refused_pass_countdown_applies_only_to_a_refusal_ended_pass(self, result):
        chain = _Chain(result)

        chain.run(continuation=0, cursors={_LANE: "1.0|a"})

        chain.dispatched.delay.assert_called_once()
        chain.dispatched.apply_async.assert_not_called()

    def test_refused_pass_countdown_keeps_a_refusal_window_below_the_bound(self):
        """Refusals the shared row does not show (half-open slots held
        elsewhere, a stale local copy) pause the chain instead of spending
        its continuation budget back to back."""
        # Given: every pass is ended by its job's own breaker; four passes in all.
        chain = _Chain(_refusal_ended())
        kwargs = {
            "continuation": 0,
            "max_continuations": 4,
            "cursors": {_LANE: "1.0|a"},
        }

        # When: the passes run as the broker would hand them out, for 60 s.
        elapsed, passes = 0.0, 0
        while elapsed <= 60:
            chain.dispatched.reset_mock()
            chain.run(**kwargs)
            passes += 1
            queued = chain.dispatched.apply_async.call_args
            assert queued is not None
            kwargs = queued.kwargs["kwargs"]
            elapsed += queued.kwargs["countdown"]

        # Then: one pass per pause, and the bound never announced.
        assert passes == 3
        bound_stops = [
            call
            for call in chain.service.emit_circuit_close_chain_stopped.call_args_list
            if call.kwargs["block_reason"] == REASON_CONTINUATION_BOUND_REACHED
        ]
        assert bound_stops == []

    def test_operator_requested_requeue_waits_for_a_held_lock_after_the_pause(self):
        chain = _Chain(BatchReplayResult(inflight_skipped=True))

        result = chain.run(operator_requested=True)

        assert result == {
            "success": True,
            "service_name": SERVICE,
            "total": 0,
            "inflight_skipped": True,
            "requeued": True,
        }
        chain.dispatched.apply_async.assert_called_once_with(
            kwargs={
                "service_name": SERVICE,
                "max_items": 50,
                "max_continuations": 10,
                **{**_CHAIN_DEFAULTS, "operator_requested": True},
                "continuation": 0,
                "cursors": None,
                "requeue_attempt": 1,
            },
            countdown=_CHAIN_REQUEUE_SECONDS,
        )

    @pytest.mark.parametrize(
        ("requeue_attempt", "requeued"),
        [(_operator_requeue_bound() - 2, True), (_operator_requeue_bound() - 1, False)],
        ids=["last_allowed_try", "bound_reached"],
    )
    def test_operator_requested_requeue_stops_at_its_bound(
        self, requeue_attempt, requeued
    ):
        chain = _Chain(BatchReplayResult(inflight_skipped=True))

        result = chain.run(operator_requested=True, requeue_attempt=requeue_attempt)

        assert result["requeued"] is requeued
        assert chain.dispatched.apply_async.called is requeued
        exhausted = [
            e
            for e in chain.logs
            if e["event"] == "dlq.circuit_recovery_operator_requeue_exhausted"
        ]
        assert len(exhausted) == (0 if requeued else 1)

    def test_operator_requested_requeue_drains_after_the_running_chain_stops(self):
        """The operator's close-with-replay that met a running automatic chain
        still drains the backlog once that chain has released the lock."""
        # Given: the first try meets the running chain's lock.
        chain = _Chain()
        chain.service.replay_on_circuit_close.side_effect = [
            BatchReplayResult(inflight_skipped=True),
            _result(capped=True, total=50, cursors={_LANE: "2.0|b"}),
        ]
        chain.run(operator_requested=True)
        requeued = chain.dispatched.apply_async.call_args.kwargs["kwargs"]

        # When: the re-queued try runs after the pause.
        chain.dispatched.reset_mock()
        result = chain.run(**requeued)

        # Then: it swept and queued its continuation.
        assert result["total"] == 50
        assert result["continued"] is True
        assert chain.service.replay_on_circuit_close.call_count == 2
        assert chain.dispatched.delay.call_args.kwargs["operator_requested"] is True

    def test_operator_requested_requeue_is_not_made_for_an_automatic_chain(self):
        chain = _Chain(BatchReplayResult(inflight_skipped=True))

        result = chain.run()

        assert "requeued" not in result
        chain.dispatched.apply_async.assert_not_called()
        chain.dispatched.delay.assert_not_called()

    @pytest.mark.parametrize(
        "make_rows",
        [
            lambda: [_pinned(SERVICE, "closed")],
            lambda: [_pinned("Payment-API", "open")],
        ],
        ids=["force_close_pin", "operator_block"],
    )
    def test_operator_hold_stops_the_chain_quietly_at_info(self, make_rows):
        """The operator's own decision, audited where the pin was set: no
        blocked event, metric or audit from the chain."""
        rows = make_rows()
        chain = _Chain(_result(total=1), states=rows)

        result = chain.run(cursors={_LANE: "1.0|a"})

        assert result["block_reason"] == REASON_OPERATOR_HOLD
        chain.service.replay_on_circuit_close.assert_not_called()
        chain.service.emit_circuit_close_chain_stopped.assert_not_called()
        held = [e for e in chain.logs if e["event"] == "dlq.circuit_recovery_held"]
        assert len(held) == 1
        assert held[0]["log_level"] == "info"
        assert held[0]["holder"] == rows[0].service_name
        assert held[0]["hold_expires_at"] == (
            rows[0].manual_override_expires_at.isoformat()
        )
        assert [e for e in chain.logs if e["log_level"] == "warning"] == []

    def test_operator_hold_does_not_stop_the_chain_the_operator_requested(self):
        chain = _Chain(_result(total=1), states=[_pinned(SERVICE, "closed")])

        result = chain.run(operator_requested=True)

        assert result["success"] is True
        chain.service.replay_on_circuit_close.assert_called_once()

    def test_operator_hold_on_an_operator_requested_chain_still_honours_a_block(self):
        """Only a pinned row that fails the chain's rule stops the operator's chain."""
        chain = _Chain(_result(total=1), states=[_pinned("Payment-API", "open")])

        result = chain.run(operator_requested=True)

        assert result["block_reason"] == REASON_OPERATOR_HOLD
        chain.service.replay_on_circuit_close.assert_not_called()

    def test_operator_hold_lapsed_pin_holds_nothing(self):
        chain = _Chain(
            _result(total=1),
            states=[_pinned(SERVICE, "closed", expires_in=timedelta(minutes=-1))],
        )

        result = chain.run()

        assert result["success"] is True
        chain.service.replay_on_circuit_close.assert_called_once()

    @pytest.mark.parametrize(
        "make_pinned",
        [
            lambda: _pinned(SERVICE, "closed"),
            lambda: _pinned("Payment-API", "open"),
        ],
        ids=["force_close_pinned_peer", "blocked_peer"],
    )
    def test_pinned_peer_beside_an_unpinned_open_row_is_circuit_reopened(
        self, make_pinned
    ):
        """The unpinned row is judged first: a breaker that re-opened on its
        own is announced even beside an operator's pin."""
        chain = _Chain(
            _result(total=1), states=[make_pinned(), _row("payment-api", "open")]
        )

        result = chain.run(cursors={_LANE: "1.0|a"})

        assert result["block_reason"] == REASON_CIRCUIT_REOPENED
        chain.service.emit_circuit_close_chain_stopped.assert_called_once_with(
            service_name=SERVICE,
            block_reason=REASON_CIRCUIT_REOPENED,
            lane_cursors={_LANE: "1.0|a"},
            offending_circuit="payment-api",
        )

    def test_integrity_blocked_stops_the_chain_with_its_signal(self):
        chain = _Chain(_result(total=1), integrity=False)

        result = chain.run(cursors={_LANE: "1.0|a"})

        assert result["block_reason"] == REASON_INTEGRITY_BLOCKED
        chain.verdict.assert_called_once_with(SERVICE)
        chain.service.replay_on_circuit_close.assert_not_called()
        chain.service.emit_circuit_close_chain_stopped.assert_called_once_with(
            service_name=SERVICE,
            block_reason=REASON_INTEGRITY_BLOCKED,
            lane_cursors={_LANE: "1.0|a"},
        )
        stopped = [
            e
            for e in chain.logs
            if e["event"] == "dlq.circuit_recovery_stopped_integrity"
        ]
        assert stopped[0]["log_level"] == "warning"

    def test_integrity_verdict_is_asked_at_the_start_of_every_pass(self):
        chain = _Chain(_result(total=1))

        chain.run(continuation=4, cursors={_LANE: "1.0|a"})

        chain.verdict.assert_called_once_with(SERVICE)
        chain.service.replay_on_circuit_close.assert_called_once()

    @pytest.mark.parametrize(
        ("state", "proceeds"),
        [("half_open", True), ("open", False)],
        ids=["half_open_probe_admitted", "refusing_row"],
    )
    def test_recovery_trigger_chain_stops_only_on_a_row_that_refuses_calls(
        self, state, proceeds
    ):
        """A chain a recovery trial dispatched probes through the job's own
        breaker: a HALF_OPEN row does not stop it, a refusing row does."""
        chain = _Chain(_result(total=1), states=[_row("payment-api", state)])

        result = chain.run(trigger="auto_replay_recovery", escalate_failures=False)

        assert chain.service.replay_on_circuit_close.called is proceeds
        assert result["success"] is proceeds
        if proceeds:
            sweep = chain.service.replay_on_circuit_close.call_args.kwargs
            assert sweep["trigger"] == "auto_replay_recovery"
            assert sweep["escalate_failures"] is False


class TestCircuitCloseChainRescanBoundBehavior:
    """A chain rescans from the start at most once (807 D10)."""

    def test_rescan_runs_once_so_a_domain_of_refused_entries_ends_below_the_bound(
        self,
    ):
        # Given: 25 parked entries every replay is refused, 10 per pass.
        before = dict(_replay_handlers)
        _replay_handlers.clear()
        repo = InMemoryFailedOperationRepository()
        refused = [
            repo.create(domain=SERVICE, failure_type="MAX_RETRIES_TIMEOUTERROR").id
            for _ in range(25)
        ]
        register_replay_handler(
            ScriptedReplayHandler(
                SERVICE, declared=("MAX_RETRIES_TIMEOUTERROR",), refused=refused
            )
        )
        service = ReplayService(
            repository=repo,
            cache=InMemoryCacheAdapter(key_prefix=f"t807r:{uuid.uuid4().hex}:"),
        )
        service._event_bus = MagicMock(spec=BaldurEventBus)
        service._governance = MagicMock(spec=GovernanceChecker)
        service._governance.check_all_governance.return_value = GovernanceCheckResult(
            allowed=True
        )
        service._governance_resolved = True
        chain = _Chain(service=service)
        kwargs = {"max_items": 10, "max_continuations": 20}

        # When: each queued pass runs in turn until none is queued.
        passes, rescans = 0, 0
        try:
            with (
                _runtime_map({}),
                patch.object(
                    ReplayService,
                    "_get_replay_automation_config",
                    autospec=True,
                    return_value=None,
                ),
            ):
                while True:
                    chain.dispatched.reset_mock()
                    chain.run(**kwargs)
                    passes += 1
                    if not chain.dispatched.delay.called:
                        break
                    kwargs = chain.dispatched.delay.call_args.kwargs
                    rescans += kwargs["cursors"] is None
                    assert passes < kwargs["max_continuations"]
        finally:
            _replay_handlers.clear()
            _replay_handlers.update(before)

        # Then: three passes, one rescan of three more, the empty pass after
        # each walk — and no bound announced.
        assert rescans == 1
        assert passes == 8
        bound_stops = [
            c
            for c in service._event_bus.emit.call_args_list
            if c.args[0] == EventType.DLQ_REPLAY_BLOCKED
            and c.kwargs["data"]["block_reason"] == REASON_CONTINUATION_BOUND_REACHED
        ]
        assert bound_stops == []
        assert {repo.get_by_id(dlq_id).status for dlq_id in refused} == {"pending"}

    def test_rescan_is_not_repeated_by_a_chain_that_already_rescanned(self):
        chain = _Chain(_result(total=2, cursors={_LANE: "2.0|b"}))

        result = chain.run(continuation=1, cursors={_LANE: "1.0|a"}, rescanned=True)

        assert result["continued"] is False
        chain.dispatched.delay.assert_not_called()


class TestRecoverParkedJobsTaskBehavior:
    """The recovery tick's task: one tick, a deadline, a summary."""

    def test_recover_parked_jobs_task_options(self):
        assert recover_parked_jobs.name == "baldur.celery_tasks.recover_parked_jobs"
        assert recover_parked_jobs.queue == "dlq_processing"
        assert recover_parked_jobs.soft_time_limit == 290
        assert recover_parked_jobs.time_limit == 300
        assert recover_parked_jobs.acks_late is False

    def test_recover_parked_jobs_runs_one_tick_and_reports_it(self):
        tick = RecoveryTickResult(
            status="completed",
            released=2,
            trials=[TrialRecord(domain=SERVICE, dlq_id="7", outcome="failed")],
            skipped={"orders_api": "not_due"},
        )

        with patch(
            "baldur.services.replay_service.recovery.run_recovery_trials",
            autospec=True,
            return_value=tick,
        ) as run:
            result = recover_parked_jobs.apply(task_id="tick-test").get()

        assert result == {
            "success": True,
            "status": "completed",
            "released": 2,
            "trials": [{"domain": SERVICE, "dlq_id": "7", "outcome": "failed"}],
            "skipped": {"orders_api": "not_due"},
        }
        deadline = run.call_args.kwargs["deadline"]
        assert deadline == pytest.approx(
            time.monotonic() + 290 - _CIRCUIT_CLOSE_DEADLINE_MARGIN_SECONDS, abs=2.0
        )

    def test_recover_parked_jobs_tick_that_raises_is_reported_not_raised(self):
        with (
            patch(
                "baldur.services.replay_service.recovery.run_recovery_trials",
                autospec=True,
                side_effect=RuntimeError("store down"),
            ),
            capture_logs() as logs,
        ):
            result = recover_parked_jobs.apply(task_id="tick-test").get()

        assert result == {"success": False, "error": "store down"}
        failed = [e for e in logs if e["event"] == "dlq.recovery_tick_failed"]
        assert failed[0]["log_level"] == "error"
