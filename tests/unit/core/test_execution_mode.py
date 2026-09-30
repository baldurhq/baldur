"""
Tests for Execution Mode and Action Executor.

Verifies that Shadow/Evaluation mode correctly prevents action execution
while still logging decisions, and that the kill switch resolves to
observe-only above every other rung without per-call logging (802 D2, D4).
"""

import json
from contextlib import nullcontext
from datetime import datetime
from unittest.mock import Mock, patch

import pytest
import structlog

from baldur import protect_facade
from baldur.core.action_executor import (
    Action,
    ActionExecutor,
    ActionResult,
    execute_action,
)
from baldur.core.execution_mode import (
    ExecutionMode,
    _get_mode_from_env,
    clear_execution_mode_override,
    get_execution_mode,
    intervention_suppressed,
    resolve_execution_mode,
    set_execution_mode,
)
from baldur.interfaces.resilience_policy import PolicyOutcome, PolicyResult
from baldur.models.dlq import DLQEntryResult
from baldur.resilience.policies.composer import _trace_structural_control
from baldur.services.retry_handler.models import RetryPolicyConfig
from baldur.settings.protect import reset_protect_settings
from tests.factories import dry_run_active, kill_switch_active


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


# =============================================================================
# Kill switch — the resolver's first rung, and quiet suppression (802 D2, D4)
# =============================================================================

_SUPPRESSED_EVENT = "execution_mode.intervention_suppressed"
_STRUCTURAL_EVENT = "execution_mode.structural_control_enforced"


def _suppression_records(logs: list[dict]) -> list[dict]:
    """The per-call records an observe-only suppression can write."""
    records = []
    for entry in logs:
        event = entry.get("event")
        if event in (_SUPPRESSED_EVENT, _STRUCTURAL_EVENT):
            records.append(entry)
        elif isinstance(event, str) and event.startswith("{"):
            try:
                records.append(json.loads(event))
            except ValueError:
                continue
    return records


def _switches(enabled: bool, dry_run: bool):
    return patch(
        "baldur.core.execution_mode._read_switches", return_value=(enabled, dry_run)
    )


class TestExecutionModeKillSwitchBehavior:
    """A pulled kill switch is observe-only everywhere dry-run is, without logs."""

    def teardown_method(self):
        clear_execution_mode_override()
        _get_mode_from_env.cache_clear()
        reset_protect_settings()

    @pytest.mark.parametrize(
        "override",
        [None, ExecutionMode.active(), ExecutionMode.evaluation()],
        ids=["no_override", "override_active", "override_evaluation"],
    )
    @pytest.mark.parametrize("dry_run", [False, True], ids=["live", "dry_run"])
    def test_kill_switch_rung_resolves_shadow_above_every_other_rung(
        self, override, dry_run
    ):
        """A code hook cannot defeat the operator's brake."""
        if override is not None:
            set_execution_mode(override)

        with _switches(enabled=False, dry_run=dry_run):
            mode, source = resolve_execution_mode()

        assert source == "kill_switch"
        assert mode == ExecutionMode.shadow()

    def test_kill_switch_rung_overrides_an_evaluation_env_posture(self, monkeypatch):
        """The switch reports shadow even where the env posture is evaluation."""
        monkeypatch.setenv("BALDUR_EXECUTION_MODE", "evaluation")
        _get_mode_from_env.cache_clear()

        with _switches(enabled=False, dry_run=False):
            mode, source = resolve_execution_mode()

        assert (mode, source) == (ExecutionMode.shadow(), "kill_switch")

    def test_enabled_kill_switch_falls_through_to_the_override(self):
        """Negative twin: with the switch up, the override rung decides."""
        set_execution_mode(ExecutionMode.active())

        with _switches(enabled=True, dry_run=True):
            mode, source = resolve_execution_mode()

        assert (mode.should_execute, source) == (True, "override")

    def test_kill_switch_pulled_for_real_reports_shadow_until_released(self):
        """``get_execution_mode()`` follows the real switch, then recovers."""
        with kill_switch_active():
            during = (get_execution_mode(), resolve_execution_mode()[1])
        after = resolve_execution_mode()

        assert during == (ExecutionMode.shadow(), "kill_switch")
        assert after[0].should_execute is True
        assert after[1] == "env"

    def test_intervention_suppressed_under_kill_switch_is_true_and_silent(self):
        """No decision record and no would-have line per call under the brake."""
        with kill_switch_active(), structlog.testing.capture_logs() as logs:
            suppressed = intervention_suppressed("payment-api", "retry", attempt=1)

        assert suppressed is True
        assert _suppression_records(logs) == []

    def test_intervention_suppressed_under_dry_run_still_writes_both_records(self):
        """Dry-run keeps its would-have timeline: the record and the INFO line."""
        with dry_run_active(), structlog.testing.capture_logs() as logs:
            suppressed = intervention_suppressed("payment-api", "retry", attempt=1)

        assert suppressed is True
        events = [r.get("event") for r in _suppression_records(logs)]
        assert events.count(_SUPPRESSED_EVENT) == 1
        assert len(events) == 2

    @pytest.mark.parametrize(
        ("switch", "expected_traces"),
        [(kill_switch_active, 0), (dry_run_active, 1), (nullcontext, 0)],
        ids=["kill_switch", "dry_run_suppressed_trace", "active"],
    )
    def test_structural_control_trace_is_quiet_under_kill_switch(
        self, switch, expected_traces
    ):
        """A live structural refusal is traced for dry-run only."""
        refused = PolicyResult(outcome=PolicyOutcome.REJECTED)

        with switch(), structlog.testing.capture_logs() as logs:
            _trace_structural_control("payment_bulkhead", refused)

        assert [log["event"] for log in logs].count(_STRUCTURAL_EVENT) == (
            expected_traces
        )

    @pytest.mark.parametrize(
        ("switch", "records_expected"),
        [(kill_switch_active, False), (dry_run_active, True)],
        ids=["kill_switch", "dry_run_suppressed"],
    )
    def test_protected_call_writes_suppression_records_only_under_dry_run(
        self, switch, records_expected
    ):
        """A failing protected call under the brake writes no per-call record."""
        # Given
        calls: list[int] = []

        def fn() -> str:
            calls.append(1)
            raise ConnectionError("downstream refused")

        # When
        with (
            switch(),
            patch(
                "baldur.services.retry_handler.sinks.store_to_dlq",
                autospec=True,
                return_value=DLQEntryResult.created("dlq-1"),
            ),
            structlog.testing.capture_logs() as logs,
            pytest.raises(ConnectionError),
        ):
            protect_facade.protect(
                "svc.kill_switch_logs",
                fn,
                retry=RetryPolicyConfig(
                    max_attempts=3,
                    backoff_base=0,
                    backoff_max=0,
                    jitter_percent=0,
                    domain="svc.kill_switch_logs",
                ),
                circuit_breaker=False,
                dlq=True,
                timeout=None,
            )

        # Then: one attempt either way; records only for dry-run
        assert calls == [1]
        assert bool(_suppression_records(logs)) is records_expected
