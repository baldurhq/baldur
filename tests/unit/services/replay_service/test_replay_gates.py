"""A replay asks its gates before it takes the entry, and gives back what it took.

Target: ``ReplayService._execute_replay`` and the lanes that reach it
(``replay_single``, ``replay_batch``, the recovery sweep, the recovery trial):

- the truncate gate and the handler's own ``can_replay`` run before the
  acquisition — a refused entry stays PENDING with no attempt spent (D2);
- a replay whose job body never began (its own breaker refused the call, or its
  own idempotency key kept it from starting) gives its attempt back and returns
  a skipped result; a trial also gives back the attempt of a job that ran and
  failed; a replay whose work may still be running is left REPLAYING (D2, D4);
- the order is: replay key marked failed -> attempt given back -> completion,
  so the next acquisition of the same attempt number is not turned away as "in
  progress" (D4).

Everything runs for real over an in-memory DLQ; the replay handler is the
scripted double, and the idempotency gate is a real gate over an in-memory
cache where the order matters.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from structlog.testing import capture_logs

from baldur.adapters.cache.memory_adapter import InMemoryCacheAdapter
from baldur.adapters.memory import InMemoryFailedOperationRepository
from baldur.core.idempotency_gate import (
    IdempotencyGate,
    configure_idempotency_gate,
    reset_idempotency_gate,
)
from baldur.interfaces.governance import GovernanceChecker
from baldur.interfaces.repositories import ResolutionTrigger
from baldur.models.governance import GovernanceCheckResult
from baldur.services.event_bus.bus.event_bus import BaldurEventBus
from baldur.services.replay_service import ReplayService
from baldur.services.replay_service.handlers import (
    _replay_handlers,
    register_replay_handler,
)
from baldur.services.replay_service.service import (
    REASON_BREAKER_REFUSED,
    REASON_HANDLER_REFUSED,
    REASON_JOB_NOT_STARTED,
    RECOVERY_TRIALS_METADATA_KEY,
)
from tests.factories.replay_doubles import (
    STEP_BREAKER_REFUSED,
    STEP_FAIL,
    STEP_FAIL_AFTER_BREAKER,
    STEP_NOT_STARTED,
    STEP_STILL_RUNNING,
    STEP_SUCCEED,
    ScriptedReplayHandler,
)

DOMAIN = "payment_api"
DECLARED = "MAX_RETRIES_TIMEOUTERROR"

_TRIAL = "trial"
_SWEEP = "sweep"
_SINGLE = "replay_single"
_BATCH = "replay_batch"


# =============================================================================
# Fixtures and helpers
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


@pytest.fixture
def repo() -> InMemoryFailedOperationRepository:
    return InMemoryFailedOperationRepository()


@pytest.fixture
def service(repo) -> Iterator[ReplayService]:
    """A real replay service over ``repo``, governance injected."""
    replay_service = ReplayService(
        repository=repo,
        cache=InMemoryCacheAdapter(key_prefix=f"t807g:{uuid.uuid4().hex}:"),
    )
    replay_service._event_bus = MagicMock(spec=BaldurEventBus)
    governance = MagicMock(spec=GovernanceChecker)
    governance.check_all_governance.return_value = GovernanceCheckResult(allowed=True)
    replay_service._governance = governance
    replay_service._governance_resolved = True
    with (
        patch.object(
            ReplayService,
            "_get_replay_automation_config",
            autospec=True,
            return_value=None,
        ),
        patch.object(
            ReplayService, "_load_failure_type_map", autospec=True, return_value={}
        ),
    ):
        yield replay_service


@pytest.fixture
def real_gate() -> Iterator[IdempotencyGate]:
    """A real idempotency gate over an in-memory cache, for this test only."""
    gate = IdempotencyGate(cache=InMemoryCacheAdapter(key_prefix="t807gate:"))
    configure_idempotency_gate(gate)
    yield gate
    reset_idempotency_gate()


def _handler(handlers, **kwargs: Any) -> ScriptedReplayHandler:
    kwargs.setdefault("declared", (DECLARED,))
    handler = ScriptedReplayHandler(DOMAIN, **kwargs)
    register_replay_handler(handler)
    handlers.append(handler)
    return handler


def _park(repo, *, truncated: bool = False) -> str:
    request_data: dict[str, Any] = {"doc": "x"}
    if truncated:
        request_data["_truncated"] = True
    return repo.create(
        domain=DOMAIN,
        failure_type=DECLARED,
        error_message="parked",
        request_data=request_data,
    ).id


def _run_lane(service, lane: str, dlq_id: str, **kwargs: Any):
    """Replay ``dlq_id`` through one replay-service lane; return its result."""
    if lane == _SINGLE:
        return service.replay_single(dlq_id)
    if lane == _BATCH:
        return service.replay_batch(
            domain=DOMAIN,
            failure_type=DECLARED,
            max_items=10,
            use_adaptive=False,
            use_priority=False,
        )
    if lane == _SWEEP:
        return service.replay_on_circuit_close(
            DOMAIN,
            max_items=10,
            escalate_failures=kwargs.get("escalate_failures", False),
            service_failure_type_map={},
        )
    entry = service.repository.get_by_id(dlq_id)
    result, _cut = service._execute_replay_within(
        dlq_id,
        None,
        trigger=ResolutionTrigger.AUTO_REPLAY_RECOVERY,
        entry=entry,
        trial=True,
    )
    return result


def _state(repo, dlq_id: str) -> tuple[str, int]:
    entry = repo.get_by_id(dlq_id)
    return entry.status, entry.retry_count


# =============================================================================
# Behavior — the gates run before the entry is taken
# =============================================================================


class TestReplayGatesBehavior:
    """Truncated, refused or unreadable: never acquired, on every lane."""

    @pytest.mark.parametrize("lane", [_SINGLE, _BATCH, _SWEEP, _TRIAL])
    @pytest.mark.parametrize(
        "gate", ["truncated", "can_replay_refuses", "can_replay_raises", "allowed"]
    )
    def test_replay_gate_decides_before_the_entry_is_acquired(
        self, service, repo, handlers, lane, gate
    ):
        # Given
        handler = _handler(handlers, steps=[STEP_SUCCEED])
        dlq_id = _park(repo, truncated=gate == "truncated")
        if gate == "can_replay_refuses":
            handler.refused.add(dlq_id)
        handler.can_replay_raises = gate == "can_replay_raises"

        # When
        with patch.object(
            InMemoryFailedOperationRepository,
            "try_acquire_for_replay",
            autospec=True,
            side_effect=InMemoryFailedOperationRepository.try_acquire_for_replay,
        ) as acquire:
            _run_lane(service, lane, dlq_id)

        # Then
        if gate == "allowed":
            assert acquire.call_count == 1
            assert handler.replayed == [dlq_id]
            assert _state(repo, dlq_id) == ("resolved", 1)
        else:
            acquire.assert_not_called()
            assert handler.replayed == []
            assert _state(repo, dlq_id) == ("pending", 0)

    def test_replay_gate_truncated_entry_is_skipped_with_its_reason(
        self, service, repo, handlers
    ):
        handler = _handler(handlers)
        dlq_id = _park(repo, truncated=True)

        result = service.replay_single(dlq_id)

        assert result.skipped is True
        assert result.data == {"skip_reason": "request_data_truncated"}
        assert result.handler_ran is False
        # The handler's own gate is not reached past the framework's.
        assert handler.asked == []

    @pytest.mark.parametrize("raises", [False, True], ids=["refuses", "raises"])
    def test_replay_gate_handler_refusal_is_skipped_and_signals_no_replay(
        self, service, repo, handlers, raises
    ):
        """Nothing was attempted: no replay event, metric or audit."""
        handler = _handler(handlers)
        dlq_id = _park(repo)
        handler.refused.add(dlq_id)
        handler.can_replay_raises = raises

        with patch(
            "baldur.services.replay_service.service.log_dlq_replay_audit",
            autospec=True,
        ) as audit:
            result = service.replay_single(dlq_id)

        assert result.skipped is True
        assert result.data == {"skip_reason": REASON_HANDLER_REFUSED}
        assert handler.asked == [dlq_id]
        audit.assert_not_called()
        service._event_bus.emit.assert_not_called()

    def test_replay_gate_reads_the_entry_when_the_lane_did_not_hand_it_over(
        self, service, repo, handlers
    ):
        handler = _handler(handlers)
        dlq_id = _park(repo)

        result = service._execute_replay(dlq_id)

        assert result.success is True
        assert handler.asked == [dlq_id]

    def test_replay_gate_on_a_missing_entry_fails_without_a_handler_call(
        self, service, handlers
    ):
        handler = _handler(handlers)

        result = service._execute_replay("missing-id")

        assert result.success is False
        assert result.error == "DLQ entry not found"
        assert handler.asked == []


# =============================================================================
# Behavior — what a replay gives back, and in which order
# =============================================================================


class TestReplayAttemptGiveBackBehavior:
    """The entry's attempts after each way a replay can end, per lane."""

    @pytest.mark.parametrize(
        ("step", "lane", "expected"),
        [
            (STEP_BREAKER_REFUSED, _TRIAL, ("pending", 0)),
            (STEP_BREAKER_REFUSED, _SWEEP, ("pending", 0)),
            (STEP_BREAKER_REFUSED, _SINGLE, ("pending", 0)),
            (STEP_NOT_STARTED, _TRIAL, ("pending", 0)),
            (STEP_NOT_STARTED, _SWEEP, ("pending", 0)),
            (STEP_NOT_STARTED, _SINGLE, ("pending", 0)),
            (STEP_FAIL_AFTER_BREAKER, _TRIAL, ("pending", 0)),
            (STEP_FAIL_AFTER_BREAKER, _SWEEP, ("pending", 1)),
            (STEP_FAIL, _TRIAL, ("pending", 0)),
            (STEP_FAIL, _SWEEP, ("pending", 1)),
        ],
        ids=[
            "own_breaker_refused_trial",
            "own_breaker_refused_sweep",
            "own_breaker_refused_single",
            "job_not_started_trial",
            "job_not_started_sweep",
            "job_not_started_single",
            "refused_after_body_began_trial",
            "refused_after_body_began_sweep_charged",
            "ran_and_failed_trial",
            "ran_and_failed_sweep_charged",
        ],
    )
    def test_give_back_follows_whether_the_job_began(
        self, service, repo, handlers, step, lane, expected
    ):
        _handler(handlers, steps=[step])
        dlq_id = _park(repo)

        _run_lane(service, lane, dlq_id)

        assert _state(repo, dlq_id) == expected

    @pytest.mark.parametrize(
        ("step", "reason"),
        [
            (STEP_BREAKER_REFUSED, REASON_BREAKER_REFUSED),
            (STEP_NOT_STARTED, REASON_JOB_NOT_STARTED),
        ],
        ids=["own_breaker_refused", "job_not_started"],
    )
    @pytest.mark.parametrize("lane", [_TRIAL, _SINGLE])
    def test_give_back_body_never_began_returns_a_skipped_result(
        self, service, repo, handlers, step, reason, lane
    ):
        _handler(handlers, steps=[step])
        dlq_id = _park(repo)

        result = _run_lane(service, lane, dlq_id)

        assert result.skipped is True
        assert result.data == {"skip_reason": reason}
        assert result.handler_ran is True
        assert result.error

    def test_give_back_trial_counts_a_recovery_trial_only_when_the_job_ran(
        self, service, repo, handlers
    ):
        _handler(handlers, steps=[STEP_FAIL, STEP_BREAKER_REFUSED, STEP_FAIL])
        dlq_id = _park(repo)

        for _ in range(3):
            _run_lane(service, _TRIAL, dlq_id)

        entry = repo.get_by_id(dlq_id)
        assert entry.metadata[RECOVERY_TRIALS_METADATA_KEY] == 2
        assert (entry.status, entry.retry_count) == ("pending", 0)

    def test_give_back_sweep_pass_ends_at_its_jobs_own_breaker_refusal(
        self, service, repo, handlers
    ):
        """The replays behind a refused one would be refused the same way."""
        handler = _handler(handlers, steps=[STEP_SUCCEED, STEP_BREAKER_REFUSED])
        first, refused, behind = (_park(repo) for _ in range(3))

        result = _run_lane(service, _SWEEP, first)

        assert handler.replayed == [first, refused]
        assert result.ended_by_breaker_refusal is True
        assert result.deadline_cut_dlq_id == refused
        assert result.total == 1
        assert _state(repo, refused) == ("pending", 0)
        assert _state(repo, behind) == ("pending", 0)

    @pytest.mark.parametrize(
        ("lane", "retry_count"), [(_TRIAL, 0), (_SWEEP, 1)], ids=["trial", "sweep"]
    )
    def test_work_may_continue_leaves_the_entry_replaying(
        self, service, repo, handlers, lane, retry_count
    ):
        """A job a timeout abandoned may still be running: the entry is held in
        REPLAYING for the stale release, so no lane starts it again before
        then. Only the trial gives its attempt back."""
        _handler(handlers, steps=[STEP_STILL_RUNNING], default=STEP_SUCCEED)
        dlq_id = _park(repo)

        _run_lane(service, lane, dlq_id)
        again = service.replay_single(dlq_id)

        assert _state(repo, dlq_id) == ("replaying", retry_count)
        assert again.success is False
        assert "replaying" in again.error

    def test_work_may_continue_is_set_on_the_returned_result(
        self, service, repo, handlers
    ):
        _handler(handlers, steps=[STEP_STILL_RUNNING])
        dlq_id = _park(repo)

        result = _run_lane(service, _TRIAL, dlq_id)

        assert (result.success, result.work_may_continue) == (False, True)
        assert result.handler_ran is True

    def test_key_before_give_back_order_is_key_then_give_back_then_completion(
        self, service, repo, handlers, real_gate
    ):
        # Given: every step of the settle logs itself.
        _handler(handlers, steps=[STEP_FAIL])
        dlq_id = _park(repo)
        calls: list[str] = []
        real_mark_failed = IdempotencyGate.mark_failed
        real_give_back = InMemoryFailedOperationRepository.return_replay_attempt
        real_complete = InMemoryFailedOperationRepository.complete_replay

        def _logged(name, real):
            def _call(*args, **kwargs):
                calls.append(name)
                return real(*args, **kwargs)

            return _call

        # When
        with (
            patch.object(
                IdempotencyGate,
                "mark_failed",
                autospec=True,
                side_effect=_logged("key", real_mark_failed),
            ),
            patch.object(
                InMemoryFailedOperationRepository,
                "return_replay_attempt",
                autospec=True,
                side_effect=_logged("give_back", real_give_back),
            ),
            patch.object(
                InMemoryFailedOperationRepository,
                "complete_replay",
                autospec=True,
                side_effect=_logged("complete", real_complete),
            ),
        ):
            _run_lane(service, _TRIAL, dlq_id)

        # Then
        assert calls == ["key", "give_back", "complete"]

    def test_key_before_give_back_next_acquisition_is_not_turned_away(
        self, service, repo, handlers, real_gate
    ):
        """The given-back attempt number is reused at once: its key reads
        failed, so the next replay takes it over instead of aborting."""
        handler = _handler(handlers, steps=[STEP_FAIL, STEP_SUCCEED])
        dlq_id = _park(repo)

        _run_lane(service, _TRIAL, dlq_id)
        batch = service.replay_batch(
            domain=DOMAIN,
            failure_type=DECLARED,
            max_items=10,
            use_adaptive=False,
            use_priority=False,
        )

        assert handler.replayed == [dlq_id, dlq_id]
        assert (batch.success_count, batch.skipped_count) == (1, 0)
        assert _state(repo, dlq_id) == ("resolved", 1)

    @pytest.mark.parametrize(
        ("give_back", "expected", "event"),
        [
            (
                RuntimeError("store down"),
                ("pending", 1),
                "replay_service.replay_attempt_return_failed",
            ),
            (
                NotImplementedError("unsupported"),
                ("pending", 1),
                "replay_service.replay_attempt_return_unsupported",
            ),
            (False, ("replaying", 1), "replay_service.replay_attempt_return_skipped"),
        ],
        ids=["raises", "unsupported", "refused_by_the_fence"],
    )
    def test_give_back_that_fails_costs_an_attempt_never_the_entry(
        self, service, repo, handlers, give_back, expected, event
    ):
        """A store fault leaves the attempt spent and completes the entry; a
        give-back the fence refuses means another replay holds the entry, so
        this one does not complete it."""
        _handler(handlers, steps=[STEP_FAIL])
        dlq_id = _park(repo)
        if isinstance(give_back, Exception):
            fake = {"side_effect": give_back}
        else:
            fake = {"return_value": give_back}

        with (
            patch.object(
                InMemoryFailedOperationRepository,
                "return_replay_attempt",
                autospec=True,
                **fake,
            ) as returned,
            capture_logs() as logs,
        ):
            _run_lane(service, _TRIAL, dlq_id)

        returned.assert_called_once_with(repo, dlq_id, 1)
        assert _state(repo, dlq_id) == expected
        assert [e for e in logs if e["event"] == event]
