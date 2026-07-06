"""
ExecutionOutcome protocol conformance tests.

Verifies that all four result types (ActionResult, PolicyResult,
ForwardResult, CompensationResult) satisfy the ExecutionOutcome protocol.
"""

from __future__ import annotations

from datetime import datetime

from baldur.core.action_executor import ActionResult
from baldur.core.execution_protocol import ExecutionOutcome
from baldur.core.step_execution_engine import CompensationResult, ForwardResult
from baldur.interfaces.resilience_policy import PolicyResult


class TestExecutionOutcomeConformanceContract:
    """All result types have success and executed properties."""

    def test_action_result_has_success_and_executed(self):
        """ActionResult has both success and executed fields."""
        result = ActionResult(
            action_id="test",
            action_name="test",
            target="target",
            executed=True,
            mode="active",
            timestamp=datetime.now(),
            success=True,
        )
        assert result.success is True
        assert result.executed is True

    def test_policy_result_has_success_and_executed(self):
        """PolicyResult has success property and executed property."""
        result = PolicyResult(value=42)
        assert result.success is True
        assert result.executed is True

    def test_forward_result_has_success_and_executed(self):
        """ForwardResult has success and executed properties."""
        result = ForwardResult(completed=True)
        assert result.success is True
        assert result.executed is True

    def test_compensation_result_has_success_and_executed(self):
        """CompensationResult has success field and executed property."""
        result = CompensationResult(success=True)
        assert result.success is True
        assert result.executed is True

    def test_protocol_declares_success_and_executed(self):
        """ExecutionOutcome protocol declares success and executed."""
        assert hasattr(ExecutionOutcome, "success")
        assert hasattr(ExecutionOutcome, "executed")


class TestForwardResultSuccessBehavior:
    """ForwardResult.success logic: completed AND no error."""

    def test_completed_without_error_is_success(self):
        """completed=True, error=None => success=True."""
        result = ForwardResult(completed=True, error=None)
        assert result.success is True

    def test_completed_with_error_is_not_success(self):
        """completed=True but error set => success=False."""
        result = ForwardResult(completed=True, error=RuntimeError("fail"))
        assert result.success is False

    def test_not_completed_without_error_is_not_success(self):
        """completed=False, error=None => success=False."""
        result = ForwardResult(completed=False)
        assert result.success is False

    def test_not_completed_with_error_is_not_success(self):
        """completed=False, error set => success=False."""
        result = ForwardResult(completed=False, error=RuntimeError("fail"))
        assert result.success is False


class TestForwardResultExecutedBehavior:
    """ForwardResult.executed mirrors completed flag."""

    def test_executed_is_true_when_completed(self):
        """executed equals completed when True."""
        result = ForwardResult(completed=True)
        assert result.executed is True

    def test_executed_is_false_when_not_completed(self):
        """executed equals completed when False."""
        result = ForwardResult(completed=False)
        assert result.executed is False


class TestCompensationResultExecutedBehavior:
    """CompensationResult.executed is always True."""

    def test_executed_always_true_on_success(self):
        """Compensation execution always returns executed=True."""
        assert CompensationResult(success=True).executed is True

    def test_executed_always_true_on_failure(self):
        """Even failed compensation was executed."""
        assert CompensationResult(success=False).executed is True


class TestPolicyResultExecutedBehavior:
    """PolicyResult.executed is always True."""

    def test_executed_always_true(self):
        """PolicyResult is always executed if PolicyComposer ran."""
        assert PolicyResult(value=None).executed is True
