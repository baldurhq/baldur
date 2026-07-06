"""Unit tests for core/step_execution_engine.py — StepExecutionEngine.

Verification techniques applied:
- Contract: SkipDecision/FailureAction enum values, LockConfig defaults
- Behavior: forward sequential execution, skip/suspend/abort decisions,
  compensation reverse loop, hook calling order
- Error: step failure → FailureAction mapping, compensation failure collection
"""

from __future__ import annotations

import threading
from typing import Any

from baldur.core.step_execution_engine import (
    FailureAction,
    ForwardResult,
    LockConfig,
    SkipDecision,
    StepExecutionEngine,
)
from baldur.core.timeout_executor import (
    HEARTBEAT_INTERVAL_SECONDS,
    LOCK_EXTEND_SECONDS,
)

# ── Test Engine (concrete subclass for testing) ─────────────


class _TestEngine(StepExecutionEngine[dict, str, dict]):
    """Minimal concrete engine for testing building blocks."""

    def __init__(self, step_results: dict[str, Any] | None = None, **kwargs):
        super().__init__(**kwargs)
        self._step_results = step_results or {}
        self._step_calls: list[str] = []
        self._comp_calls: list[str] = []

    def execute(self, context: dict) -> dict:
        return context

    def _run_step(
        self,
        context: dict,
        step: str,
        stop_event: threading.Event,
    ) -> Any:
        self._step_calls.append(step)
        if step in self._step_results:
            result = self._step_results[step]
            if isinstance(result, Exception):
                raise result
            return result
        return f"result-{step}"

    def _run_compensation(self, context: dict, step: str) -> Any:
        self._comp_calls.append(step)
        return f"comp-{step}"

    def _get_step_timeout(self, step: str) -> float:
        return 10.0

    def _get_lock_config(self, context: dict) -> LockConfig:
        return LockConfig()


# ── Contract Tests ──────────────────────────────────────────


class TestStepExecutionEngineContract:
    """Design contract values for engine enums and data models."""

    def test_skip_decision_values(self):
        """SkipDecision enum has exactly 4 members."""
        assert SkipDecision.CONTINUE == "continue"
        assert SkipDecision.SKIP == "skip"
        assert SkipDecision.SUSPEND == "suspend"
        assert SkipDecision.ABORT == "abort"
        assert len(SkipDecision) == 4

    def test_failure_action_values(self):
        """FailureAction enum has exactly 4 members."""
        assert FailureAction.COMPENSATE == "compensate"
        assert FailureAction.RETRY == "retry"
        assert FailureAction.SUSPEND == "suspend"
        assert FailureAction.ABORT == "abort"
        assert len(FailureAction) == 4

    def test_lock_config_defaults(self):
        """LockConfig defaults match TimeoutExecutor constants."""
        cfg = LockConfig()
        assert cfg.lock is None
        assert cfg.namespace == ""
        assert cfg.session_id == ""
        assert cfg.heartbeat_interval == HEARTBEAT_INTERVAL_SECONDS
        assert cfg.extend_seconds == LOCK_EXTEND_SECONDS

    def test_forward_result_default_failure_action(self):
        """ForwardResult default failure_action is COMPENSATE."""
        result = ForwardResult(completed=False)
        assert result.failure_action == FailureAction.COMPENSATE


# ── Forward Execution Behavior ──────────────────────────────


class TestForwardExecutionBehavior:
    """_execute_steps_forward() behavior verification."""

    def test_all_steps_succeed_returns_completed(self):
        """모든 step 성공 시 completed=True를 반환한다."""
        engine = _TestEngine()
        result = engine._execute_steps_forward({}, ["s1", "s2", "s3"])

        assert result.completed is True
        assert result.last_index == 2
        assert engine._step_calls == ["s1", "s2", "s3"]

    def test_empty_steps_returns_completed(self):
        """빈 step 목록은 completed=True (last_index=-1)."""
        engine = _TestEngine()
        result = engine._execute_steps_forward({}, [])
        assert result.completed is True
        assert result.last_index == -1

    def test_start_index_skips_earlier_steps(self):
        """start_index 지정 시 이전 step을 건너뛴다."""
        engine = _TestEngine()
        result = engine._execute_steps_forward({}, ["s0", "s1", "s2"], start_index=1)

        assert result.completed is True
        assert engine._step_calls == ["s1", "s2"]

    def test_step_failure_returns_incomplete_with_error(self):
        """step 실패 시 completed=False + error를 반환한다."""
        error = RuntimeError("step failed")
        engine = _TestEngine(step_results={"s2": error})

        result = engine._execute_steps_forward({}, ["s1", "s2", "s3"])

        assert result.completed is False
        assert result.last_index == 1
        assert result.error is error
        assert result.failure_action == FailureAction.COMPENSATE
        # s3 should not have been called
        assert "s3" not in engine._step_calls


class TestForwardSkipDecisionBehavior:
    """should_skip_step() hook integration with forward loop."""

    def test_skip_decision_skips_step(self):
        """SKIP 결정 시 해당 step을 건너뛴다."""

        class SkippingEngine(_TestEngine):
            def should_skip_step(self, ctx, step, idx):
                return SkipDecision.SKIP if step == "s2" else SkipDecision.CONTINUE

        engine = SkippingEngine()
        result = engine._execute_steps_forward({}, ["s1", "s2", "s3"])

        assert result.completed is True
        assert engine._step_calls == ["s1", "s3"]

    def test_suspend_decision_halts_execution(self):
        """SUSPEND 결정 시 실행을 중단한다."""

        class SuspendingEngine(_TestEngine):
            def should_skip_step(self, ctx, step, idx):
                return SkipDecision.SUSPEND if step == "s2" else SkipDecision.CONTINUE

        engine = SuspendingEngine()
        result = engine._execute_steps_forward({}, ["s1", "s2", "s3"])

        assert result.completed is False
        assert result.decision == SkipDecision.SUSPEND
        assert result.last_index == 1
        assert engine._step_calls == ["s1"]

    def test_abort_decision_halts_execution(self):
        """ABORT 결정 시 실행을 중단한다."""

        class AbortingEngine(_TestEngine):
            def should_skip_step(self, ctx, step, idx):
                return SkipDecision.ABORT if step == "s2" else SkipDecision.CONTINUE

        engine = AbortingEngine()
        result = engine._execute_steps_forward({}, ["s1", "s2", "s3"])

        assert result.completed is False
        assert result.decision == SkipDecision.ABORT


class TestForwardHookCallingOrderBehavior:
    """Hook calling order verification."""

    def test_hooks_called_in_correct_order(self):
        """on_before_step → _run_step → on_after_step 순서로 호출된다."""
        call_log = []

        class LoggingEngine(_TestEngine):
            def on_before_step(self, ctx, step, idx):
                call_log.append(f"before-{step}")

            def on_after_step(self, ctx, step, idx, result):
                call_log.append(f"after-{step}")

        engine = LoggingEngine()
        engine._execute_steps_forward({}, ["s1", "s2"])

        assert call_log == ["before-s1", "after-s1", "before-s2", "after-s2"]

    def test_on_step_failed_returns_custom_action(self):
        """on_step_failed()의 반환값이 ForwardResult.failure_action에 반영된다."""

        class RetryEngine(_TestEngine):
            def on_step_failed(self, ctx, step, idx, exc):
                return FailureAction.RETRY

        engine = RetryEngine(step_results={"s1": RuntimeError("fail")})
        result = engine._execute_steps_forward({}, ["s1"])

        assert result.failure_action == FailureAction.RETRY


# ── Compensation Behavior ───────────────────────────────────


class TestCompensationBehavior:
    """_execute_steps_reverse() behavior verification."""

    def test_reverse_order_execution(self):
        """보상은 역순으로 실행된다."""
        engine = _TestEngine()
        result = engine._execute_steps_reverse({}, ["s0", "s1", "s2"], from_index=2)

        assert result.success is True
        assert engine._comp_calls == ["s2", "s1", "s0"]

    def test_compensation_from_middle_index(self):
        """from_index부터 0까지 역순으로 보상한다."""
        engine = _TestEngine()
        result = engine._execute_steps_reverse({}, ["s0", "s1", "s2"], from_index=1)

        assert result.success is True
        assert engine._comp_calls == ["s1", "s0"]

    def test_compensation_failure_collected(self):
        """보상 실패 시 failures에 수집하고 나머지를 계속 실행한다."""

        class FailingCompEngine(_TestEngine):
            def _run_compensation(self, ctx, step):
                self._comp_calls.append(step)
                if step == "s1":
                    raise RuntimeError("comp failed")
                return f"comp-{step}"

        engine = FailingCompEngine()
        result = engine._execute_steps_reverse({}, ["s0", "s1", "s2"], from_index=2)

        assert result.success is False
        assert len(result.failures) == 1
        assert result.failures[0].step_index == 1
        # s0 and s2 should still have been compensated
        assert "s0" in engine._comp_calls
        assert "s2" in engine._comp_calls

    def test_on_before_compensation_false_skips_step(self):
        """on_before_compensation()이 False를 반환하면 해당 step을 건너뛴다."""

        class SkipCompEngine(_TestEngine):
            def on_before_compensation(self, ctx, step, idx):
                return step != "s1"

        engine = SkipCompEngine()
        result = engine._execute_steps_reverse({}, ["s0", "s1", "s2"], from_index=2)

        assert result.success is True
        assert engine._comp_calls == ["s2", "s0"]  # s1 skipped
