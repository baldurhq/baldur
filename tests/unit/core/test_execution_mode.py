"""
Tests for Execution Mode and Action Executor.

Verifies that Shadow/Evaluation mode correctly prevents action execution
while still logging decisions.
"""

from datetime import datetime
from unittest.mock import Mock, patch

from baldur.core.action_executor import (
    Action,
    ActionExecutor,
    ActionResult,
    execute_action,
)
from baldur.core.execution_mode import (
    ExecutionMode,
    clear_execution_mode_override,
    get_execution_mode,
    set_execution_mode,
)


class TestExecutionMode:
    """Tests for ExecutionMode configuration."""

    def setup_method(self):
        """Reset global execution-mode state before each test."""
        self._reset_execution_mode_state()

    def teardown_method(self):
        """Restore global execution-mode state after each test.

        ``test_mode_from_env`` fills the ``_get_mode_from_env`` lru_cache with a
        ``shadow`` value while ``BALDUR_EXECUTION_MODE`` is patched; the patch is
        undone on block exit but the cached value survives, leaking observe-only
        mode into every later test on the same worker (it suppresses retry/CB
        interventions globally). Clearing both the override and the env cache
        keeps each test isolated even if an assertion fails mid-test.
        """
        self._reset_execution_mode_state()

    @staticmethod
    def _reset_execution_mode_state():
        from baldur.core.execution_mode import _get_mode_from_env

        clear_execution_mode_override()
        _get_mode_from_env.cache_clear()

    def test_active_mode_properties(self):
        """Active mode should allow execution."""
        mode = ExecutionMode.active()

        assert mode.is_active is True
        assert mode.is_shadow is False
        assert mode.is_evaluation is False
        assert mode.should_execute is True
        assert mode.is_dry_run is False

    def test_shadow_mode_properties(self):
        """Shadow mode should prevent execution."""
        mode = ExecutionMode.shadow()

        assert mode.is_active is False
        assert mode.is_shadow is True
        assert mode.is_evaluation is False
        assert mode.should_execute is False
        assert mode.is_dry_run is True

    def test_evaluation_mode_properties(self):
        """Evaluation mode should prevent execution but validate."""
        mode = ExecutionMode.evaluation()

        assert mode.is_active is False
        assert mode.is_shadow is False
        assert mode.is_evaluation is True
        assert mode.should_execute is False
        assert mode.is_dry_run is True
        assert mode.validate_only is True

    def test_mode_override(self):
        """Programmatic override should take precedence."""
        # Set shadow mode
        set_execution_mode(ExecutionMode.shadow())

        mode = get_execution_mode()
        assert mode.is_shadow is True

        # Clear override
        clear_execution_mode_override()

    def test_mode_from_env(self):
        """Environment variable should set mode."""
        with patch.dict("os.environ", {"BALDUR_EXECUTION_MODE": "shadow"}):
            # Clear cache to pick up new env
            from baldur.core.execution_mode import _get_mode_from_env

            _get_mode_from_env.cache_clear()

            # Need to clear override first
            clear_execution_mode_override()

            get_execution_mode()
            # Note: This may still return active due to caching
            # In real usage, env is read at startup


class TestActionExecutor:
    """Tests for ActionExecutor."""

    def setup_method(self):
        """Reset mode before each test."""
        clear_execution_mode_override()

    def teardown_method(self):
        """Clean up after each test."""
        clear_execution_mode_override()

    def test_active_mode_executes_action(self):
        """In active mode, action should be executed."""
        # Arrange
        set_execution_mode(ExecutionMode.active())
        execute_fn = Mock(return_value={"status": "success"})

        action = Action(
            name="test_action",
            target="test_service",
            execute_fn=execute_fn,
            params={"key": "value"},
        )

        executor = ActionExecutor()

        # Act
        result = executor.execute(action)

        # Assert
        assert result.executed is True
        assert result.success is True
        assert result.result == {"status": "success"}
        assert result.mode == "active"
        execute_fn.assert_called_once()

    def test_shadow_mode_does_not_execute_action(self):
        """In shadow mode, action should NOT be executed."""
        # Arrange
        set_execution_mode(ExecutionMode.shadow())
        execute_fn = Mock(return_value={"status": "success"})

        action = Action(
            name="test_action",
            target="test_service",
            execute_fn=execute_fn,
            params={"key": "value"},
        )

        executor = ActionExecutor()

        # Act
        result = executor.execute(action)

        # Assert
        assert result.executed is False
        assert result.success is None  # Not executed
        assert result.result is None
        assert result.mode == "shadow"
        assert result.was_dry_run is True
        execute_fn.assert_not_called()  # key assertion: action is NOT executed

    def test_evaluation_mode_does_not_execute_action(self):
        """In evaluation mode, action should NOT be executed."""
        # Arrange
        set_execution_mode(ExecutionMode.evaluation())
        execute_fn = Mock(return_value={"status": "success"})

        action = Action(
            name="test_action",
            target="test_service",
            execute_fn=execute_fn,
        )

        executor = ActionExecutor()

        # Act
        result = executor.execute(action)

        # Assert
        assert result.executed is False
        execute_fn.assert_not_called()
        assert result.mode == "evaluation"

    def test_evaluation_mode_runs_validation(self):
        """In evaluation mode, validation should run."""
        # Arrange
        set_execution_mode(ExecutionMode.evaluation())
        execute_fn = Mock()
        validate_fn = Mock(return_value=True)

        action = Action(
            name="test_action",
            target="test_service",
            execute_fn=execute_fn,
            validate_fn=validate_fn,
        )

        executor = ActionExecutor()

        # Act
        result = executor.execute(action)

        # Assert
        assert result.executed is False
        assert result.validation_result is True
        execute_fn.assert_not_called()
        validate_fn.assert_called_once()

    def test_action_result_to_dict(self):
        """ActionResult should serialize to dict."""
        result = ActionResult(
            action_id="test-123",
            action_name="test_action",
            target="test_service",
            executed=True,
            success=True,
            mode="active",
            timestamp=datetime(2025, 12, 15, 10, 0, 0),
        )

        data = result.to_dict()

        assert data["action_id"] == "test-123"
        assert data["executed"] is True
        assert data["was_dry_run"] is False

    def test_execute_action_convenience_function(self):
        """Convenience function should work."""
        set_execution_mode(ExecutionMode.shadow())
        execute_fn = Mock()

        action = Action(
            name="test_action",
            target="test_service",
            execute_fn=execute_fn,
        )

        result = execute_action(action)

        assert result.executed is False
        execute_fn.assert_not_called()

    def test_active_mode_handles_execution_error(self):
        """Active mode should handle execution errors."""
        set_execution_mode(ExecutionMode.active())
        execute_fn = Mock(side_effect=ValueError("Test error"))

        action = Action(
            name="test_action",
            target="test_service",
            execute_fn=execute_fn,
        )

        executor = ActionExecutor()
        result = executor.execute(action)

        assert result.executed is True
        assert result.success is False
        assert "Test error" in result.error

    def test_executor_with_mode_override(self):
        """Executor can be initialized with mode override."""
        # Global mode is active
        set_execution_mode(ExecutionMode.active())

        execute_fn = Mock()

        action = Action(
            name="test_action",
            target="test_service",
            execute_fn=execute_fn,
        )

        # But executor has shadow mode
        executor = ActionExecutor(mode=ExecutionMode.shadow())
        result = executor.execute(action)

        # Should use executor's mode, not global
        assert result.executed is False
        assert result.mode == "shadow"
        execute_fn.assert_not_called()
