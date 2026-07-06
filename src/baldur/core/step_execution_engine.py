"""Step-based execution engine — Infrastructure-Only ABC.

Provides common infrastructure building blocks (forward loop, reverse
compensation loop, timeout, heartbeat) that Saga, Runbook, and future
step-based engines can compose in their own execute() flow.

Subclasses own the execute() orchestration. The engine only provides
building blocks — it does not enforce a specific execution order.
"""

from __future__ import annotations

import logging
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Generic, TypeVar

from baldur.core.idempotency_gate import IdempotencyGate
from baldur.core.timeout_executor import (
    HEARTBEAT_INTERVAL_SECONDS,
    LOCK_EXTEND_SECONDS,
    TimeoutExecutor,
)

__all__ = [
    "StepExecutionEngine",
    "SkipDecision",
    "FailureAction",
    "ForwardResult",
    "CompensationResult",
    "CompensationFailure",
    "LockConfig",
]

T_CONTEXT = TypeVar("T_CONTEXT")
T_STEP = TypeVar("T_STEP")
T_RESULT = TypeVar("T_RESULT")

logger = logging.getLogger(__name__)


# ── Decision Enums ──────────────────────────────────────────


class SkipDecision(str, Enum):
    """Decision from should_skip_step() hook."""

    CONTINUE = "continue"  # Proceed with execution
    SKIP = "skip"  # Skip this step
    SUSPEND = "suspend"  # Suspend entire execution
    ABORT = "abort"  # Abort entire execution


class FailureAction(str, Enum):
    """Decision from on_step_failed() hook."""

    COMPENSATE = "compensate"  # Begin compensation
    RETRY = "retry"  # Retry (subclass-managed)
    SUSPEND = "suspend"  # Suspend execution
    ABORT = "abort"  # Abort without compensation


# ── Result Data Models ──────────────────────────────────────


@dataclass
class LockConfig:
    """Lock configuration for timeout executor."""

    lock: Any = None  # LockExtendable
    namespace: str = ""
    session_id: str = ""
    heartbeat_interval: float = HEARTBEAT_INTERVAL_SECONDS
    extend_seconds: float = LOCK_EXTEND_SECONDS


@dataclass
class CompensationFailure:
    """Record of a single compensation failure."""

    step_index: int
    error: Exception


@dataclass
class ForwardResult:
    """Result of _execute_steps_forward()."""

    completed: bool
    last_index: int = 0
    error: Exception | None = None
    failure_action: FailureAction = FailureAction.COMPENSATE
    decision: SkipDecision | None = None
    step_results: dict[int, Any] = field(default_factory=dict)

    @property
    def success(self) -> bool:
        """Whether all steps completed without error."""
        return self.completed and self.error is None

    @property
    def executed(self) -> bool:
        """Whether the forward execution was attempted (alias for completed)."""
        return self.completed


@dataclass
class CompensationResult:
    """Result of _execute_steps_reverse()."""

    success: bool
    failures: list[CompensationFailure] = field(default_factory=list)

    @property
    def executed(self) -> bool:
        """Compensation is always executed when called."""
        return True


# ── Engine ABC ──────────────────────────────────────────────


class StepExecutionEngine(ABC, Generic[T_CONTEXT, T_STEP, T_RESULT]):
    """Step-based execution engine — Infrastructure-Only.

    Provides common infrastructure (forward loop, reverse compensation loop,
    timeout execution) as building blocks. Subclasses compose these in their
    own execute() implementation.

    Type Parameters:
        T_CONTEXT: Execution context (e.g., SagaInstance, RunbookContext)
        T_STEP: Step definition (e.g., SagaStepDef, RunbookStep)
        T_RESULT: Execution result (e.g., SagaResult, RunbookResult)
    """

    def __init__(
        self,
        timeout_executor: TimeoutExecutor | None = None,
        idempotency_gate: IdempotencyGate | None = None,
    ) -> None:
        self._timeout_executor = timeout_executor or TimeoutExecutor()
        self._idempotency_gate = idempotency_gate

    # ── Subclass Required ───────────────────────────────────

    @abstractmethod
    def execute(self, context: T_CONTEXT) -> T_RESULT:
        """Full execution flow. Subclass composes building blocks here."""
        ...

    @abstractmethod
    def _run_step(
        self,
        context: T_CONTEXT,
        step: T_STEP,
        stop_event: threading.Event,
    ) -> Any:
        """Execute a single step. Must check stop_event.is_set() periodically."""
        ...

    @abstractmethod
    def _run_compensation(self, context: T_CONTEXT, step: T_STEP) -> Any:
        """Execute compensation for a single step."""
        ...

    @abstractmethod
    def _get_step_timeout(self, step: T_STEP) -> float:
        """Return timeout in seconds for the given step."""
        ...

    @abstractmethod
    def _get_lock_config(self, context: T_CONTEXT) -> LockConfig:
        """Return lock configuration for timeout executor."""
        ...

    # ── Building Blocks (Forward) ───────────────────────────

    def _execute_steps_forward(
        self,
        context: T_CONTEXT,
        steps: list[T_STEP],
        start_index: int = 0,
    ) -> ForwardResult:
        """Execute steps sequentially. Subclass calls this from execute().

        Returns ForwardResult indicating success or failure point.
        """
        lock_cfg = self._get_lock_config(context)

        for idx in range(start_index, len(steps)):
            step = steps[idx]

            # Skip decision
            skip = self.should_skip_step(context, step, idx)
            if skip == SkipDecision.SKIP:
                continue
            if skip in (SkipDecision.SUSPEND, SkipDecision.ABORT):
                return ForwardResult(
                    completed=False,
                    last_index=idx,
                    decision=skip,
                )

            try:
                self.on_before_step(context, step, idx)

                def _run(
                    stop_event: Any,
                    _ctx: Any = context,
                    _step: Any = step,
                ) -> Any:
                    return self._run_step(_ctx, _step, stop_event)

                result = self._timeout_executor.execute(
                    fn=_run,
                    timeout_seconds=self._get_step_timeout(step),
                    lock=lock_cfg.lock,
                    lock_namespace=lock_cfg.namespace,
                    session_id=lock_cfg.session_id,
                    heartbeat_interval=lock_cfg.heartbeat_interval,
                    extend_seconds=lock_cfg.extend_seconds,
                )
                self.on_after_step(context, step, idx, result)
            except Exception as exc:
                action = self.on_step_failed(context, step, idx, exc)
                return ForwardResult(
                    completed=False,
                    last_index=idx,
                    error=exc,
                    failure_action=action,
                )

        return ForwardResult(completed=True, last_index=len(steps) - 1)

    # ── Building Blocks (Compensation) ──────────────────────

    def _execute_steps_reverse(
        self,
        context: T_CONTEXT,
        steps: list[T_STEP],
        from_index: int,
    ) -> CompensationResult:
        """Execute compensation in reverse order from from_index to 0."""
        failures: list[CompensationFailure] = []
        for idx in range(from_index, -1, -1):
            step = steps[idx]
            if not self.on_before_compensation(context, step, idx):
                continue  # Not a compensation target
            try:
                self._run_compensation(context, step)
            except Exception as exc:
                self.on_compensation_failed(context, step, idx, exc)
                failures.append(CompensationFailure(step_index=idx, error=exc))
        return CompensationResult(success=len(failures) == 0, failures=failures)

    # ── Optional Hooks ──────────────────────────────────────

    def should_skip_step(
        self,
        context: T_CONTEXT,
        step: T_STEP,
        idx: int,
    ) -> SkipDecision:
        """Pre-step skip/suspend/continue decision. Default: continue."""
        return SkipDecision.CONTINUE

    def on_before_step(
        self,
        context: T_CONTEXT,
        step: T_STEP,
        idx: int,
    ) -> None:
        """Hook before step execution. Default: no-op."""

    def on_after_step(
        self,
        context: T_CONTEXT,
        step: T_STEP,
        idx: int,
        result: Any,
    ) -> None:
        """Hook after step execution. Default: no-op."""

    def on_step_failed(
        self,
        context: T_CONTEXT,
        step: T_STEP,
        idx: int,
        exc: Exception,
    ) -> FailureAction:
        """Hook on step failure. Default: COMPENSATE."""
        return FailureAction.COMPENSATE

    def on_before_compensation(
        self,
        context: T_CONTEXT,
        step: T_STEP,
        idx: int,
    ) -> bool:
        """Hook before compensation. Return False to skip. Default: True."""
        return True

    def on_compensation_failed(
        self,
        context: T_CONTEXT,
        step: T_STEP,
        idx: int,
        exc: Exception,
    ) -> None:
        """Hook on compensation failure. Default: log error."""
        logger.error(
            "step_engine.compensation_failed",
            extra={"step_index": idx, "error": str(exc)},
        )
