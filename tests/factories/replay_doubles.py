"""A replay handler whose replays the test scripts, and the rows it reports.

``ScriptedReplayHandler`` stands in for a customer's ``ReplayHandler`` (or the
function-replay handler) at the replay service's handler seam: every
``replay()`` plays the next scripted step, so a test drives each way a replay
can end — the job ran and succeeded or failed, its own breaker refused it
before the body began, its own idempotency key kept it from starting, it
raised, its worker died mid-call, or the work it abandoned is still running —
without a job, a breaker or a dependency behind it.

A step is one of the ``STEP_*`` names, or a callable ``(entry) -> ReplayResult``
for anything else. ``can_replay`` refuses the ids in ``refused`` (or raises
when ``can_replay_raises`` is set). Every replay and every ``can_replay`` ask
is recorded, so a test can assert which entries were taken and which were only
looked at.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Iterable
from concurrent.futures import Future
from typing import Any

from baldur.core.abandoned_work import record_abandoned
from baldur.interfaces.repositories import FailedOperationData
from baldur.services.replay_service.handlers import ReplayHandler
from baldur.services.replay_service.models import ReplayResult

__all__ = [
    "STEP_BREAKER_REFUSED",
    "STEP_DIE",
    "STEP_FAIL",
    "STEP_FAIL_AFTER_BREAKER",
    "STEP_FAIL_WITHOUT_FLAGS",
    "STEP_NOT_STARTED",
    "STEP_RAISE",
    "STEP_STILL_RUNNING",
    "STEP_SUCCEED",
    "ScriptedReplayHandler",
    "WorkerDied",
]

# The job ran and returned.
STEP_SUCCEED = "succeed"
# The job ran and its dependency failed.
STEP_FAIL = "fail"
# The job's own breaker refused the call before the body began.
STEP_BREAKER_REFUSED = "breaker_refused"
# The job never began for another reason (its own idempotency key held it).
STEP_NOT_STARTED = "not_started"
# The body began, then a breaker of another call it makes refused: charged.
STEP_FAIL_AFTER_BREAKER = "fail_after_breaker"
# A hand-written handler that reports no start flags and fails.
STEP_FAIL_WITHOUT_FLAGS = "fail_without_flags"
# The handler raised.
STEP_RAISE = "raise"
# A timeout gave up waiting for the job; the work it abandoned still runs.
STEP_STILL_RUNNING = "still_running"
# The worker died mid-call: nothing after the call runs.
STEP_DIE = "die"

Step = str | Callable[[FailedOperationData], ReplayResult]


class WorkerDied(BaseException):  # noqa: N818 - models a process death, not an error
    """Escapes every ``except Exception``, as a killed worker leaves a call."""


def _flags(*, began: bool, refused: bool) -> dict[str, bool]:
    return {"job_started": began, "rejected_by_breaker": refused}


class ScriptedReplayHandler(ReplayHandler):
    """A replay handler whose replays play a script.

    Args:
        domain: The stored domain the handler is filed under.
        steps: Steps for the first replays, in order.
        default: The step every replay after the script plays.
        declared: The failure types the handler declares
            (``auto_replay_failure_types``) — the lanes an automatic recovery
            selects its domain's entries through.
        refused: Entry ids ``can_replay`` refuses.
        on_replay: Called with the entry before each step plays (a hook for a
            side effect a real job would have, such as moving a breaker row).
    """

    def __init__(
        self,
        domain: str,
        *,
        steps: Iterable[Step] = (),
        default: Step = STEP_SUCCEED,
        declared: tuple[str, ...] = (),
        refused: Iterable[str] = (),
        on_replay: Callable[[FailedOperationData], None] | None = None,
    ) -> None:
        self._domain = domain
        self._steps: deque[Step] = deque(steps)
        self.default = default
        self.declared = declared
        self.refused = set(refused)
        self.can_replay_raises = False
        self.on_replay = on_replay
        self.replayed: list[str] = []
        self.asked: list[str] = []
        self._still_running: list[Future[Any]] = []

    @property
    def domain(self) -> str:
        return self._domain

    @property
    def auto_replay_failure_types(self) -> tuple[str, ...]:
        return self.declared

    def script(self, *steps: Step) -> None:
        """Queue more steps after the ones not yet played."""
        self._steps.extend(steps)

    def can_replay(self, failed_op: FailedOperationData) -> tuple[bool, str]:
        self.asked.append(failed_op.id)
        if self.can_replay_raises:
            raise RuntimeError("can_replay exploded")
        if failed_op.id in self.refused:
            return False, "not safe to replay"
        return True, ""

    def replay(self, failed_op: FailedOperationData) -> ReplayResult:
        self.replayed.append(failed_op.id)
        if self.on_replay is not None:
            self.on_replay(failed_op)
        step = self._steps.popleft() if self._steps else self.default
        if callable(step):
            return step(failed_op)
        return self.play(step, failed_op.id)

    def settle(self) -> None:
        """End the work every still-running step left behind (teardown)."""
        for future in self._still_running:
            if not future.done():
                future.set_result(None)
        self._still_running.clear()

    def play(self, step: str, dlq_id: str) -> ReplayResult:
        """The result one named step produces (raises for the raising steps)."""
        if step == STEP_SUCCEED:
            return ReplayResult.succeeded(
                dlq_id, "re-ran", data=_flags(began=True, refused=False)
            )
        if step == STEP_FAIL:
            return self._failed(dlq_id, "dependency down", began=True, refused=False)
        if step == STEP_BREAKER_REFUSED:
            return self._failed(dlq_id, "circuit open", began=False, refused=True)
        if step == STEP_NOT_STARTED:
            return self._failed(dlq_id, "key in progress", began=False, refused=False)
        if step == STEP_FAIL_AFTER_BREAKER:
            return self._failed(dlq_id, "inner circuit open", began=True, refused=True)
        if step == STEP_FAIL_WITHOUT_FLAGS:
            return ReplayResult.failed(dlq_id, "handler says no")
        if step == STEP_RAISE:
            raise RuntimeError("handler blew up")
        if step == STEP_STILL_RUNNING:
            future: Future[Any] = Future()
            self._still_running.append(future)
            record_abandoned(future)
            return self._failed(dlq_id, "timed out", began=True, refused=False)
        if step == STEP_DIE:
            raise WorkerDied
        raise ValueError(f"unknown replay step: {step!r}")

    @staticmethod
    def _failed(dlq_id: str, error: str, *, began: bool, refused: bool) -> ReplayResult:
        return ReplayResult(
            success=False,
            dlq_id=dlq_id,
            error=error,
            data=_flags(began=began, refused=refused),
        )
