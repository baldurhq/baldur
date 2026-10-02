"""An operator replay whose job may still be running holds its entry (810 D4).

``ReplayExecutionMixin._run_operator_replay`` runs the replay handler inside a
work scope, so a job whose ``timeout=`` cut it off — or whose wait an
interruption cut short — leaves the scope holding, and the outcome says
``work_may_continue``. The console's ``retry_entry`` / ``force_redrive_entry``
then leave the entry REPLAYING until the stale-replay release instead of
handing it back to the queue, where any replay could start the job again beside
itself. ``_execute_replay`` keeps its boolean contract for callers that do not
complete the entry.

Verification techniques applied:
- Branch outcome (§8.12): every exit of ``_run_operator_replay`` — both gate
  refusals, success / failure / raise with and without work left running, a
  ``BaseException`` from the handler, a raising ``can_replay``.
- Contract: ``_execute_replay`` returns a real bool and raises the handler's
  error again.
- State transition (§8.8): lane x how the replay ended -> response, raise and
  entry status over a real in-memory repository; ending the work afterwards
  changes nothing; a second operator action on the held entry is refused.
- Side effect (§8.4): the held exit logs its failure WARNING with
  ``work_may_continue=True``.

The job is ``ScriptedReplayHandler``: its still-running step records a pending
``Future`` into the operator's scope, where a timeout stage inside the job
would record it — no thread, no sleep.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from structlog.testing import capture_logs

from baldur.adapters.memory import InMemoryFailedOperationRepository
from baldur.core.abandoned_work import current_work_scope
from baldur.core.exceptions import DLQReplayError, DLQStateConflictError
from baldur.interfaces.repositories import FailedOperationData, FailedOperationStatus
from baldur.models.dlq import DLQConfig
from baldur.services.dlq_read import DLQReadService, ReplayExecutionMixin
from baldur.services.dlq_read.replay_execution import OperatorReplayOutcome
from baldur.services.replay_service import register_replay_handler
from baldur.services.replay_service.handlers import _replay_handlers
from baldur.services.replay_service.models import ReplayResult
from tests.factories.replay_doubles import (
    STEP_DIE,
    STEP_FAIL,
    STEP_STILL_RUNNING,
    STEP_SUCCEED,
    ScriptedReplayHandler,
    WorkerDied,
)

PENDING = FailedOperationStatus.PENDING.value
REPLAYING = FailedOperationStatus.REPLAYING.value
REQUIRES_REVIEW = FailedOperationStatus.REQUIRES_REVIEW.value
RESOLVED = FailedOperationStatus.RESOLVED.value

# How a job ends, beyond the scripted handler's named steps.
RAISE = "raise"
RAISE_WHILE_RUNNING = "raise_while_running"
SUCCEED_WHILE_RUNNING = "succeed_while_running"


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def handlers() -> Iterator[list[ScriptedReplayHandler]]:
    """The handler registry, emptied for the test and restored after it."""
    before = dict(_replay_handlers)
    _replay_handlers.clear()
    made: list[ScriptedReplayHandler] = []
    yield made
    for handler in made:
        handler.settle()
    _replay_handlers.clear()
    _replay_handlers.update(before)


class _Job:
    """A registered job whose next replay ends by ``ends_by``."""

    def __init__(self, handlers, domain: str, ends_by: str) -> None:
        self.error = ConnectionError("gateway reset")
        self.scopes: list[object] = []
        self.handler = ScriptedReplayHandler(
            domain, on_replay=lambda entry: self.scopes.append(current_work_scope())
        )
        self.handler.script(self._step(ends_by))
        register_replay_handler(self.handler)
        handlers.append(self.handler)

    def _step(self, ends_by: str):
        if ends_by == RAISE:
            return self._raise
        if ends_by == RAISE_WHILE_RUNNING:
            return self._raise_while_running
        if ends_by == SUCCEED_WHILE_RUNNING:
            return self._succeed_while_running
        return ends_by

    def _raise(self, entry: FailedOperationData) -> ReplayResult:
        raise self.error

    def _raise_while_running(self, entry: FailedOperationData) -> ReplayResult:
        self.handler.play(STEP_STILL_RUNNING, entry.id)
        raise self.error

    def _succeed_while_running(self, entry: FailedOperationData) -> ReplayResult:
        self.handler.play(STEP_STILL_RUNNING, entry.id)
        return self.handler.play(STEP_SUCCEED, entry.id)


@pytest.fixture
def make_read_service():
    """A real ``DLQReadService`` over an in-memory repository (audit stubbed)."""

    def _make(cap: int = 3):
        repo = InMemoryFailedOperationRepository()
        service = DLQReadService(
            config=DLQConfig(enabled=True, max_replay_attempts=cap), repository=repo
        )
        service._log_dlq_audit = lambda **kwargs: None  # type: ignore[method-assign]
        return service, repo

    return _make


class _Executor(ReplayExecutionMixin):
    """Bare mixin host — the replay primitive reads nothing off ``self``."""


def _entry(domain: str, *, truncated: bool = False) -> FailedOperationData:
    return FailedOperationData(
        id="entry-1",
        domain=domain,
        failure_type="timeout",
        status=REPLAYING,
        request_data={"_truncated": True} if truncated else {"order_id": "o-1"},
    )


# =============================================================================
# _run_operator_replay — every exit
# =============================================================================


class TestRunOperatorReplayBehavior:
    """The outcome reports success, whether the job may still run, and the
    handler's error; the scope it opens never outlives the call."""

    @pytest.mark.parametrize(
        ("ends_by", "succeeded", "may_continue", "with_error"),
        [
            (STEP_SUCCEED, True, False, False),
            (SUCCEED_WHILE_RUNNING, True, True, False),
            (STEP_FAIL, False, False, False),
            (STEP_STILL_RUNNING, False, True, False),
            (RAISE, False, False, True),
            (RAISE_WHILE_RUNNING, False, True, True),
        ],
        ids=[
            "success_nothing_running",
            "success_work_running",
            "failure_nothing_running",
            "failure_work_running",
            "raise_nothing_running",
            "raise_work_running",
        ],
    )
    def test_run_operator_replay_work_may_continue_by_how_the_job_ended(
        self, handlers, ends_by, succeeded, may_continue, with_error
    ):
        # Given
        job = _Job(handlers, "op_outcome", ends_by)
        before = current_work_scope()

        # When
        outcome = _Executor()._run_operator_replay(_entry("op_outcome"))

        # Then — the outcome, and the scope opened around the handler only.
        assert outcome == OperatorReplayOutcome(
            succeeded=succeeded,
            work_may_continue=may_continue,
            error=job.error if with_error else None,
        )
        assert job.scopes[0] is not before
        assert current_work_scope() is before

    @pytest.mark.parametrize(
        "truncated", [True, False], ids=["truncate_gate", "can_replay_gate"]
    )
    def test_run_operator_replay_gate_refusal_work_may_continue_false(
        self, handlers, truncated
    ):
        """A refused replay never ran the job, so nothing can be left running."""
        job = _Job(handlers, "op_refused", STEP_STILL_RUNNING)
        if not truncated:
            job.handler.refused.add("entry-1")

        outcome = _Executor()._run_operator_replay(
            _entry("op_refused", truncated=truncated)
        )

        assert outcome == OperatorReplayOutcome(succeeded=False)
        assert job.handler.replayed == []

    def test_run_operator_replay_base_exception_propagates_after_scope_closes(
        self, handlers
    ):
        """An interrupt during the replay itself is not captured."""
        _Job(handlers, "op_interrupt", STEP_DIE)
        before = current_work_scope()

        with pytest.raises(WorkerDied):
            _Executor()._run_operator_replay(_entry("op_interrupt"))

        assert current_work_scope() is before

    def test_run_operator_replay_raising_can_replay_propagates_before_any_scope(
        self, handlers
    ):
        job = _Job(handlers, "op_gate_raises", STEP_SUCCEED)
        job.handler.can_replay_raises = True
        before = current_work_scope()

        with pytest.raises(RuntimeError, match="can_replay exploded"):
            _Executor()._run_operator_replay(_entry("op_gate_raises"))

        assert job.handler.replayed == []
        assert current_work_scope() is before


# =============================================================================
# _execute_replay — the boolean contract the PRO overlays still reach
# =============================================================================


class TestExecuteReplayContract:
    """``_execute_replay`` returns a real bool and raises the handler's error
    again, whatever the scope says (a non-bool outcome would read as success
    to a caller that tests it for truth)."""

    def test_execute_replay_contract_success_returns_true(self, handlers):
        _Job(handlers, "bool_ok", STEP_SUCCEED)

        assert _Executor()._execute_replay(_entry("bool_ok")) is True

    def test_execute_replay_contract_failure_returns_false(self, handlers):
        """Work left running is not reported here — only success or failure."""
        _Job(handlers, "bool_fail", STEP_STILL_RUNNING)

        assert _Executor()._execute_replay(_entry("bool_fail")) is False

    @pytest.mark.parametrize(
        "ends_by",
        [RAISE, RAISE_WHILE_RUNNING],
        ids=["nothing_running", "work_running"],
    )
    def test_execute_replay_contract_handler_error_is_raised_again(
        self, handlers, ends_by
    ):
        job = _Job(handlers, "bool_raise", ends_by)

        with pytest.raises(ConnectionError) as raised:
            _Executor()._execute_replay(_entry("bool_raise"))

        assert raised.value is job.error


# =============================================================================
# retry_entry / force_redrive_entry — the entry follows the job
# =============================================================================


def _held_entry_for(lane: str, repo, domain: str):
    """A PENDING entry for a retry; an at-cap one in review for a force-redrive."""
    if lane == "retry":
        return repo.create(domain=domain, failure_type="x", retry_count=0)
    entry = repo.create(domain=domain, failure_type="x", retry_count=3, max_retries=3)
    repo.update_status(entry.id, status=REQUIRES_REVIEW)
    return entry


def _operator_action(service, lane: str, pk: str):
    if lane == "retry":
        return service.retry_entry(pk)
    return service.force_redrive_entry(pk, actor_id="ops", reason="fixed")


_LANES = ["retry", "force_redrive"]


class TestOperatorReplayHoldBehavior:
    """A console retry or force-redrive whose job failed or raised while work
    it stopped waiting for may still run leaves the entry REPLAYING."""

    @pytest.mark.parametrize(
        ("ends_by", "raises", "response_status", "entry_status"),
        [
            (STEP_STILL_RUNNING, False, REPLAYING, REPLAYING),
            (RAISE_WHILE_RUNNING, True, None, REPLAYING),
            (STEP_FAIL, False, PENDING, PENDING),
            (RAISE, True, None, PENDING),
        ],
        ids=[
            "failure_work_running",
            "raise_work_running",
            "failure_nothing_running",
            "raise_nothing_running",
        ],
    )
    @pytest.mark.parametrize("lane", _LANES)
    def test_operator_replay_work_may_continue_holds_entry_replaying(
        self,
        make_read_service,
        handlers,
        lane,
        ends_by,
        raises,
        response_status,
        entry_status,
    ):
        # Given
        service, repo = make_read_service(cap=3)
        domain = f"hold_{lane}"
        _Job(handlers, domain, ends_by)
        entry = _held_entry_for(lane, repo, domain)

        # When
        if raises:
            with pytest.raises(DLQReplayError) as raised:
                _operator_action(service, lane, entry.id)
            names_the_hold = "stale-replay release" in str(raised.value)
        else:
            result = _operator_action(service, lane, entry.id)

        # Then — the response, the raise and the entry follow the job.
        if raises:
            assert names_the_hold is (entry_status == REPLAYING)
        else:
            assert result["success"] is False
            assert result["status"] == response_status
        assert repo.get_by_id(entry.id).status == entry_status

    @pytest.mark.parametrize("lane", _LANES)
    def test_operator_replay_work_may_continue_entry_stays_after_work_ends(
        self, make_read_service, handlers, lane
    ):
        """Only the stale-replay release brings the held entry back."""
        # Given — a held entry.
        service, repo = make_read_service(cap=3)
        domain = f"after_{lane}"
        job = _Job(handlers, domain, STEP_STILL_RUNNING)
        entry = _held_entry_for(lane, repo, domain)
        _operator_action(service, lane, entry.id)

        # When — the job's work ends.
        job.handler.settle()

        # Then
        assert repo.get_by_id(entry.id).status == REPLAYING

    @pytest.mark.parametrize("lane", _LANES)
    def test_operator_replay_work_may_continue_second_action_is_refused(
        self, make_read_service, handlers, lane
    ):
        """A second operator action on the held entry answers 409."""
        service, repo = make_read_service(cap=3)
        domain = f"twice_{lane}"
        _Job(handlers, domain, STEP_STILL_RUNNING)
        entry = _held_entry_for(lane, repo, domain)
        _operator_action(service, lane, entry.id)

        with pytest.raises(DLQStateConflictError):
            _operator_action(service, lane, entry.id)

    def test_retry_work_may_continue_on_last_attempt_is_not_sent_to_review(
        self, make_read_service, handlers
    ):
        """At the cap the held entry stays REPLAYING, not REQUIRES_REVIEW."""
        service, repo = make_read_service(cap=3)
        _Job(handlers, "hold_last", STEP_STILL_RUNNING)
        entry = repo.create(
            domain="hold_last", failure_type="x", retry_count=2, max_retries=3
        )

        result = service.retry_entry(entry.id)

        assert result["status"] == REPLAYING
        assert repo.get_by_id(entry.id).status == REPLAYING

    @pytest.mark.parametrize("lane", _LANES)
    def test_operator_replay_work_may_continue_success_still_resolves(
        self, make_read_service, handlers, lane
    ):
        """The job's own result was success: resolved whatever the scope says."""
        service, repo = make_read_service(cap=3)
        domain = f"ok_{lane}"
        _Job(handlers, domain, SUCCEED_WHILE_RUNNING)
        entry = _held_entry_for(lane, repo, domain)

        result = _operator_action(service, lane, entry.id)

        assert result["status"] == RESOLVED
        assert repo.get_by_id(entry.id).status == RESOLVED

    @pytest.mark.parametrize(
        ("lane", "event"),
        [
            ("retry", "dlq.entry_retry_failed"),
            ("force_redrive", "dlq.entry_force_redrive_failed"),
        ],
        ids=_LANES,
    )
    def test_operator_replay_work_may_continue_logs_warning(
        self, make_read_service, handlers, lane, event
    ):
        service, repo = make_read_service(cap=3)
        domain = f"log_{lane}"
        _Job(handlers, domain, STEP_STILL_RUNNING)
        entry = _held_entry_for(lane, repo, domain)

        with capture_logs() as logs:
            _operator_action(service, lane, entry.id)

        held = [log for log in logs if log["event"] == event]
        assert len(held) == 1
        assert held[0]["log_level"] == "warning"
        assert held[0]["work_may_continue"] is True
