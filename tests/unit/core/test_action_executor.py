"""
Tests for ActionExecutor.
core/action_executor.py의 Action, ActionResult, ActionExecutor에 대한 단위 테스트.
실행 모드별 동작(ACTIVE, SHADOW, EVALUATION)과 편의 함수를 검증합니다.
"""

from datetime import datetime
from unittest.mock import patch

from baldur.core.action_executor import (
    Action,
    ActionExecutor,
    ActionResult,
    execute_action,
    get_action_executor,
)
from baldur.core.execution_mode import ExecutionMode, ExecutionModeType

# =============================================================================
# Action Dataclass Tests
# =============================================================================


class TestAction:
    """Action 데이터클래스 테스트."""

    def test_basic_creation(self):
        """Basic creation
        기본 Action 인스턴스 생성이 올바른지 확인.
        """
        action = Action(
            name="force_open",
            target="payment_api",
            execute_fn=lambda: "done",
        )
        assert action.name == "force_open"
        assert action.target == "payment_api"
        assert action.action_id is not None  # UUID auto-generated

    def test_auto_description(self):
        """Auto description
        description이 비어있으면 자동 생성되는지 확인.
        """
        action = Action(
            name="force_open",
            target="payment_api",
            execute_fn=lambda: None,
        )
        assert "force_open" in action.description
        assert "payment_api" in action.description

    def test_custom_description(self):
        """Custom description
        커스텀 description이 유지되는지 확인.
        """
        action = Action(
            name="test",
            target="target",
            execute_fn=lambda: None,
            description="Custom description",
        )
        assert action.description == "Custom description"

    def test_default_params(self):
        """Default params
        params의 기본값이 빈 딕셔너리인지 확인.
        """
        action = Action(name="test", target="target", execute_fn=lambda: None)
        assert action.params == {}


# =============================================================================
# ActionResult Tests
# =============================================================================


class TestActionResult:
    """ActionResult 데이터클래스 테스트."""

    def test_was_dry_run_when_not_executed(self):
        """Was dry run when not executed
        executed=False일 때 was_dry_run이 True인지 확인.
        """
        result = ActionResult(
            action_id="123",
            action_name="test",
            target="t",
            executed=False,
            mode="shadow",
            timestamp=datetime.now(),
        )
        assert result.was_dry_run is True

    def test_was_dry_run_when_executed(self):
        """Was dry run when executed
        executed=True일 때 was_dry_run이 False인지 확인.
        """
        result = ActionResult(
            action_id="123",
            action_name="test",
            target="t",
            executed=True,
            mode="active",
            timestamp=datetime.now(),
        )
        assert result.was_dry_run is False

    def test_to_dict(self):
        """To dict conversion
        to_dict 메서드가 필요한 모든 필드를 포함하는지 확인.
        """
        result = ActionResult(
            action_id="abc",
            action_name="test_action",
            target="svc",
            executed=True,
            success=True,
            mode="active",
            timestamp=datetime(2026, 1, 1, 0, 0, 0),
        )
        d = result.to_dict()
        assert d["action_id"] == "abc"
        assert d["executed"] is True
        assert d["success"] is True
        assert d["was_dry_run"] is False
        assert "timestamp" in d


# =============================================================================
# ActionExecutor Active Mode Tests
# =============================================================================


class TestActionExecutorActiveMode:
    """ACTIVE 모드에서의 ActionExecutor 테스트."""

    def setup_method(self):
        """각 테스트 전에 글로벌 싱글톤 리셋."""
        import baldur.core.action_executor as module

        module._default_executor = None

    @patch("baldur.core.action_executor.get_execution_mode")
    @patch("baldur.core.action_executor.log_intervention_evaluated")
    def test_active_mode_executes(self, mock_log, mock_mode):
        """Active mode executes action
        ACTIVE 모드에서 실제로 실행되는지 확인.
        """
        mock_mode.return_value = ExecutionMode(
            mode=ExecutionModeType.ACTIVE,
            log_decisions=True,
            execute_actions=True,
            validate_only=False,
        )

        execute_called = []
        action = Action(
            name="test_action",
            target="test_service",
            execute_fn=lambda: execute_called.append(True) or "result_value",
        )

        executor = ActionExecutor()
        result = executor.execute(action)

        assert result.executed is True
        assert result.success is True
        assert result.result == "result_value"
        assert len(execute_called) == 1

    @patch("baldur.core.action_executor.get_execution_mode")
    @patch("baldur.core.action_executor.log_intervention_evaluated")
    def test_active_mode_handles_exception(self, mock_log, mock_mode):
        """Active mode handles exception
        실행 중 예외 발생 시 success=False를 반환하는지 확인.
        """
        mock_mode.return_value = ExecutionMode(
            mode=ExecutionModeType.ACTIVE,
            log_decisions=True,
            execute_actions=True,
            validate_only=False,
        )

        action = Action(
            name="failing_action",
            target="test_service",
            execute_fn=lambda: (_ for _ in ()).throw(ValueError("test error")),
        )

        executor = ActionExecutor()
        result = executor.execute(action)

        assert result.executed is True
        assert result.success is False
        assert "test error" in result.error


# =============================================================================
# ActionExecutor Shadow Mode Tests
# =============================================================================


class TestActionExecutorShadowMode:
    """SHADOW 모드에서의 ActionExecutor 테스트."""

    @patch("baldur.core.action_executor.log_intervention_evaluated")
    def test_shadow_mode_does_not_execute(self, mock_log):
        """Shadow mode does not execute
        SHADOW 모드에서는 실행하지 않는지 확인.
        """
        mode = ExecutionMode(
            mode=ExecutionModeType.SHADOW,
            log_decisions=True,
            execute_actions=False,
            validate_only=False,
        )

        execute_called = []
        action = Action(
            name="test_action",
            target="test_service",
            execute_fn=lambda: execute_called.append(True),
        )

        executor = ActionExecutor(mode=mode)
        result = executor.execute(action)

        assert result.executed is False
        assert result.success is None
        assert len(execute_called) == 0
        assert result.mode == "shadow"


# =============================================================================
# ActionExecutor Evaluation Mode Tests
# =============================================================================


class TestActionExecutorEvaluationMode:
    """EVALUATION 모드에서의 ActionExecutor 테스트."""

    @patch("baldur.core.action_executor.log_intervention_evaluated")
    def test_evaluation_mode_validates_only(self, mock_log):
        """Evaluation mode validates only
        EVALUATION 모드에서 검증만 수행하는지 확인.
        """
        mode = ExecutionMode(
            mode=ExecutionModeType.EVALUATION,
            log_decisions=True,
            execute_actions=False,
            validate_only=True,
        )

        action = Action(
            name="test_action",
            target="test_service",
            execute_fn=lambda: "should_not_run",
            validate_fn=lambda: True,
        )

        executor = ActionExecutor(mode=mode)
        result = executor.execute(action)

        assert result.executed is False
        assert result.validation_result is True

    @patch("baldur.core.action_executor.log_intervention_evaluated")
    def test_evaluation_mode_validation_failure(self, mock_log):
        """Evaluation mode validation failure
        검증 함수가 실패할 때 validation_result=False를 반환하는지 확인.
        """
        mode = ExecutionMode(
            mode=ExecutionModeType.EVALUATION,
            log_decisions=True,
            execute_actions=False,
            validate_only=True,
        )

        action = Action(
            name="test_action",
            target="test_service",
            execute_fn=lambda: None,
            validate_fn=lambda: False,
        )

        executor = ActionExecutor(mode=mode)
        result = executor.execute(action)

        assert result.executed is False
        assert result.validation_result is False

    @patch("baldur.core.action_executor.log_intervention_evaluated")
    def test_evaluation_mode_validation_exception(self, mock_log):
        """Evaluation mode validation exception
        검증 함수에서 예외 발생 시 validation_result=False를 반환하는지 확인.
        """
        mode = ExecutionMode(
            mode=ExecutionModeType.EVALUATION,
            log_decisions=True,
            execute_actions=False,
            validate_only=True,
        )

        action = Action(
            name="test_action",
            target="test_service",
            execute_fn=lambda: None,
            validate_fn=lambda: (_ for _ in ()).throw(RuntimeError("fail")),
        )

        executor = ActionExecutor(mode=mode)
        result = executor.execute(action)

        assert result.validation_result is False


# =============================================================================
# Mode Override Tests
# =============================================================================


class TestActionExecutorModeOverride:
    """모드 오버라이드 테스트."""

    @patch("baldur.core.action_executor.log_intervention_evaluated")
    def test_mode_override(self, mock_log):
        """Mode override
        생성자에서 지정한 모드가 글로벌 모드보다 우선하는지 확인.
        """
        override_mode = ExecutionMode(
            mode=ExecutionModeType.SHADOW,
            log_decisions=True,
            execute_actions=False,
            validate_only=False,
        )
        executor = ActionExecutor(mode=override_mode)
        assert executor.mode.mode == ExecutionModeType.SHADOW


# =============================================================================
# Convenience Function Tests
# =============================================================================


class TestConvenienceFunctions:
    """편의 함수 테스트."""

    def setup_method(self):
        """글로벌 executor 리셋."""
        import baldur.core.action_executor as module

        module._default_executor = None

    def test_get_action_executor_singleton(self):
        """get_action_executor singleton
        get_action_executor가 동일한 인스턴스를 반환하는지 확인.
        """
        executor1 = get_action_executor()
        executor2 = get_action_executor()
        assert executor1 is executor2

    @patch("baldur.core.action_executor.get_execution_mode")
    @patch("baldur.core.action_executor.log_intervention_evaluated")
    def test_execute_action_function(self, mock_log, mock_mode):
        """execute_action function
        execute_action 편의 함수가 올바르게 동작하는지 확인.
        """
        mock_mode.return_value = ExecutionMode(
            mode=ExecutionModeType.ACTIVE,
            log_decisions=True,
            execute_actions=True,
            validate_only=False,
        )

        action = Action(
            name="test",
            target="svc",
            execute_fn=lambda: "ok",
        )
        result = execute_action(action)
        assert result.executed is True
        assert result.result == "ok"
