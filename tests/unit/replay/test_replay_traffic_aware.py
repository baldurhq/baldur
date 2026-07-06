"""
Traffic-Aware Replay Tests.

Tests for:
1. TrafficHealthStatus dataclass
2. check_traffic_health function
3. TrafficAwareReplayTask
4. Beat schedule integration
5. RuntimeConfig integration
"""

from unittest.mock import MagicMock, patch

import pytest

# =============================================================================
# TrafficHealthStatus Tests
# =============================================================================


class TestTrafficHealthStatus:
    """TrafficHealthStatus dataclass tests."""

    def test_healthy_factory(self):
        """Healthy-status factory test."""
        from baldur.tasks.traffic_aware_replay import TrafficHealthStatus

        checks = {"circuit_breaker": True, "error_budget": True, "governance": True}
        status = TrafficHealthStatus.healthy(checks)

        assert status.is_healthy is True
        assert status.reason == "All checks passed"
        assert status.checks == checks

    def test_unhealthy_factory(self):
        """Unhealthy-status factory test."""
        from baldur.tasks.traffic_aware_replay import TrafficHealthStatus

        checks = {"circuit_breaker": False, "error_budget": True}
        status = TrafficHealthStatus.unhealthy(
            reason="Circuit breaker is open",
            checks=checks,
        )

        assert status.is_healthy is False
        assert status.reason == "Circuit breaker is open"
        assert status.checks["circuit_breaker"] is False

    def test_default_checks_empty(self):
        """Default checks is an empty dict."""
        from baldur.tasks.traffic_aware_replay import TrafficHealthStatus

        status = TrafficHealthStatus(is_healthy=True, reason="test")
        assert status.checks == {}


# =============================================================================
# check_traffic_health Function Tests
# =============================================================================


class TestCheckTrafficHealth:
    """check_traffic_health function tests."""

    @pytest.fixture(autouse=True)
    def _require_pro(self):
        # Every test patches baldur_pro governance / error_budget_gate (PRO-tier).
        pytest.importorskip("baldur_pro")

    @patch("baldur_pro.services.governance.checks.check_all_governance")
    @patch("baldur_pro.services.error_budget_gate.get_error_budget_gate")
    def test_all_checks_pass_without_domain(self, mock_gate_getter, mock_governance):
        """All checks pass without a domain."""
        from baldur.tasks.traffic_aware_replay import check_traffic_health

        # Error Budget mock - pass
        mock_gate = MagicMock()
        mock_gate.is_replay_allowed.return_value = True
        mock_gate_getter.return_value = mock_gate

        # Governance mock - allowed
        mock_governance.return_value = MagicMock(
            allowed=True,
            block_message="",
        )

        result = check_traffic_health(domain=None)

        assert result.is_healthy is True
        assert "governance" in result.checks
        assert result.checks["governance"] is True

    @patch("baldur_pro.services.governance.checks.check_all_governance")
    @patch("baldur_pro.services.error_budget_gate.get_error_budget_gate")
    @patch("baldur.services.circuit_breaker.get_circuit_breaker_service")
    def test_circuit_breaker_open_blocks(
        self, mock_cb_getter, mock_gate_getter, mock_governance
    ):
        """Blocked when the circuit breaker is open."""
        from baldur.tasks.traffic_aware_replay import check_traffic_health

        # CB mock - OPEN state
        mock_cb = MagicMock()
        mock_cb.get_state.return_value = "open"
        mock_cb_getter.return_value = mock_cb

        result = check_traffic_health(domain="payment")

        assert result.is_healthy is False
        assert "circuit_breaker" in result.checks
        assert result.checks["circuit_breaker"] is False
        assert "open" in result.reason.lower()

    @patch("baldur_pro.services.governance.checks.check_all_governance")
    @patch("baldur_pro.services.error_budget_gate.get_error_budget_gate")
    def test_error_budget_insufficient_blocks(self, mock_gate_getter, mock_governance):
        """Blocked when the error budget is insufficient."""
        from baldur.tasks.traffic_aware_replay import check_traffic_health

        # Error Budget mock - insufficient
        mock_gate = MagicMock()
        mock_gate.is_replay_allowed.return_value = False
        mock_gate_getter.return_value = mock_gate

        result = check_traffic_health(domain=None)

        assert result.is_healthy is False
        assert "error_budget" in result.checks
        assert result.checks["error_budget"] is False
        assert "budget" in result.reason.lower()

    @patch("baldur_pro.services.governance.checks.check_all_governance")
    @patch("baldur_pro.services.error_budget_gate.get_error_budget_gate")
    def test_governance_blocked(self, mock_gate_getter, mock_governance):
        """Blocked when the governance check fails."""
        from baldur.tasks.traffic_aware_replay import check_traffic_health

        # Error Budget mock - pass
        mock_gate = MagicMock()
        mock_gate.is_replay_allowed.return_value = True
        mock_gate_getter.return_value = mock_gate

        # Governance mock - blocked
        mock_governance.return_value = MagicMock(
            allowed=False,
            block_message="Kill Switch is active",
        )

        result = check_traffic_health(domain=None)

        assert result.is_healthy is False
        assert "governance" in result.checks
        assert result.checks["governance"] is False
        assert "Kill Switch" in result.reason


# =============================================================================
# TrafficAwareReplayTask Tests
# =============================================================================


class TestTrafficAwareReplayTask:
    """TrafficAwareReplayTask tests."""

    def test_task_name(self):
        """Verify the task name."""
        from baldur.tasks.traffic_aware_replay import TrafficAwareReplayTask

        task = TrafficAwareReplayTask()
        assert task.name == "baldur.traffic_aware_replay"

    def test_traffic_aware_disabled_returns_disabled(self):
        """Returns disabled status when traffic-aware replay is disabled."""
        from baldur.tasks.traffic_aware_replay import TrafficAwareReplayTask

        task = TrafficAwareReplayTask()

        with patch.object(task, "_get_replay_automation_config") as mock_config:
            mock_config.return_value = {"traffic_aware_enabled": False}
            result = task.run()

        assert result["status"] == "disabled"
        assert result["total"] == 0
        assert "disabled" in result["reason"].lower()

    def test_unhealthy_traffic_returns_skipped(self):
        """Returns skipped status when traffic is unhealthy."""
        from baldur.tasks.traffic_aware_replay import (
            TrafficAwareReplayTask,
            TrafficHealthStatus,
        )

        task = TrafficAwareReplayTask()

        with patch.object(task, "_get_replay_automation_config") as mock_config:
            mock_config.return_value = {
                "traffic_aware_enabled": True,
                "traffic_aware_max_items": 30,
            }

            with patch(
                "baldur.tasks.traffic_aware_replay.check_traffic_health"
            ) as mock_health:
                mock_health.return_value = TrafficHealthStatus.unhealthy(
                    reason="CB is open",
                    checks={"circuit_breaker": False},
                )
                result = task.run(domain="payment")

        assert result["status"] == "skipped"
        assert result["reason"] == "CB is open"
        assert result["checks"]["circuit_breaker"] is False

    def test_healthy_traffic_executes_replay(self):
        """Runs replay when traffic is healthy."""
        from baldur.tasks.traffic_aware_replay import (
            TrafficAwareReplayTask,
            TrafficHealthStatus,
        )

        task = TrafficAwareReplayTask()

        with patch.object(task, "_get_replay_automation_config") as mock_config:
            mock_config.return_value = {
                "traffic_aware_enabled": True,
                "traffic_aware_max_items": 25,
            }

            with patch(
                "baldur.tasks.traffic_aware_replay.check_traffic_health"
            ) as mock_health:
                mock_health.return_value = TrafficHealthStatus.healthy(
                    checks={
                        "circuit_breaker": True,
                        "error_budget": True,
                        "governance": True,
                    }
                )

                with patch.object(task, "_execute_replay") as mock_replay:
                    mock_replay.return_value = {"total": 10, "success": 8, "failed": 2}
                    result = task.run()
                    mock_replay.assert_called_once_with(None, 25)

        assert result["status"] == "completed"
        assert result["total"] == 10
        assert result["success"] == 8
        assert result["failed"] == 2

    def test_replay_error_returns_error_status(self):
        """Returns error status when an exception occurs during replay."""
        from baldur.tasks.traffic_aware_replay import (
            TrafficAwareReplayTask,
            TrafficHealthStatus,
        )

        task = TrafficAwareReplayTask()

        with patch.object(task, "_get_replay_automation_config") as mock_config:
            mock_config.return_value = {
                "traffic_aware_enabled": True,
                "traffic_aware_max_items": 30,
            }

            with patch(
                "baldur.tasks.traffic_aware_replay.check_traffic_health"
            ) as mock_health:
                mock_health.return_value = TrafficHealthStatus.healthy(checks={})

                with patch.object(task, "_execute_replay") as mock_replay:
                    mock_replay.side_effect = RuntimeError("ReplayService failed")
                    result = task.run()

        assert result["status"] == "error"
        assert "ReplayService failed" in result["reason"]

    def test_get_severity_for_error(self):
        """Verify the severity for error status."""
        from baldur.tasks.traffic_aware_replay import TrafficAwareReplayTask

        task = TrafficAwareReplayTask()
        result = {"status": "error", "failed": 0, "success": 0}

        assert task._get_severity(result) == "warning"

    def test_get_severity_for_high_failure(self):
        """Verify the severity when the failure rate is high."""
        from baldur.tasks.traffic_aware_replay import TrafficAwareReplayTask

        task = TrafficAwareReplayTask()
        result = {"status": "completed", "failed": 10, "success": 5}

        assert task._get_severity(result) == "warning"

    def test_get_severity_for_success(self):
        """Verify the severity on success."""
        from baldur.tasks.traffic_aware_replay import TrafficAwareReplayTask

        task = TrafficAwareReplayTask()
        result = {"status": "completed", "failed": 2, "success": 10}

        assert task._get_severity(result) == "info"

    def test_summary_message_disabled(self):
        """Verify the disabled-status message."""
        from baldur.tasks.traffic_aware_replay import TrafficAwareReplayTask

        task = TrafficAwareReplayTask()
        result = {"status": "disabled", "reason": "Traffic-aware replay disabled"}

        message = task._get_summary_message(result)
        assert "disabled" in message

    def test_summary_message_completed(self):
        """Verify the completed-status message."""
        from baldur.tasks.traffic_aware_replay import TrafficAwareReplayTask

        task = TrafficAwareReplayTask()
        result = {"status": "completed", "total": 10, "success": 8, "failed": 2}

        message = task._get_summary_message(result)
        assert "8/10" in message
        assert "2" in message


# =============================================================================
# Beat Schedule Integration Tests
# =============================================================================


class TestBeatScheduleIntegration:
    """Beat schedule integration tests."""

    def test_get_traffic_aware_beat_schedule(self):
        """get_traffic_aware_beat_schedule function test."""
        from baldur.tasks.traffic_aware_replay import (
            get_traffic_aware_beat_schedule,
        )

        schedule = get_traffic_aware_beat_schedule()

        assert "traffic-aware-replay" in schedule
        assert schedule["traffic-aware-replay"]["task"] == "baldur.traffic_aware_replay"
        assert schedule["traffic-aware-replay"]["options"]["queue"] == "dlq_processing"

    def test_included_in_main_beat_schedule(self):
        """Included in the main beat schedule."""
        from baldur.adapters.celery.beat_schedule import (
            get_baldur_beat_schedule,
        )

        schedule = get_baldur_beat_schedule(include_traffic_aware=True)

        assert "traffic-aware-replay" in schedule

    def test_excluded_when_disabled(self):
        """Excluded from the schedule when disabled."""
        from baldur.adapters.celery.beat_schedule import (
            get_baldur_beat_schedule,
        )

        schedule = get_baldur_beat_schedule(include_traffic_aware=False)

        assert "traffic-aware-replay" not in schedule


# =============================================================================
# Task Registry Tests
# =============================================================================


class TestTaskRegistry:
    """Task registry tests."""

    def test_traffic_aware_tasks_list(self):
        """Verify the TRAFFIC_AWARE_TASKS list."""
        from baldur.tasks.traffic_aware_replay import (
            TRAFFIC_AWARE_TASKS,
            TrafficAwareReplayTask,
        )

        assert TrafficAwareReplayTask in TRAFFIC_AWARE_TASKS
        assert len(TRAFFIC_AWARE_TASKS) >= 1

    def test_register_with_celery(self):
        """Verify registration with the Celery app."""
        from baldur.tasks.traffic_aware_replay import (
            register_traffic_aware_tasks_with_celery,
        )

        mock_app = MagicMock()
        register_traffic_aware_tasks_with_celery(mock_app)

        # Verify register_task was called
        assert mock_app.register_task.called


# =============================================================================
# Module Exports Tests
# =============================================================================


class TestModuleExports:
    """Module exports tests."""

    def test_tasks_init_exports(self):
        """Verify export from tasks/__init__.py."""
        from baldur.tasks import (
            TRAFFIC_AWARE_TASKS,
            TrafficAwareReplayTask,
            TrafficHealthStatus,
            check_traffic_health,
        )

        # Passes if all exports import successfully
        assert TrafficHealthStatus is not None
        assert check_traffic_health is not None
        assert TrafficAwareReplayTask is not None
        assert TRAFFIC_AWARE_TASKS is not None

    def test_all_exports(self):
        """Verify the __all__ list."""
        from baldur.tasks.traffic_aware_replay import __all__

        expected_exports = [
            "TrafficHealthStatus",
            "check_traffic_health",
            "TrafficAwareReplayTask",
            "TRAFFIC_AWARE_TASKS",
            "register_traffic_aware_tasks_with_celery",
            "get_traffic_aware_beat_schedule",
        ]

        for export in expected_exports:
            assert export in __all__


# =============================================================================
# RuntimeConfig Integration Tests
# =============================================================================


class TestRuntimeConfigIntegration:
    """RuntimeConfig integration tests."""

    def test_traffic_aware_config_in_replay_automation(self):
        """Verify ReplayAutomationConfig exposes the traffic-aware settings."""
        from baldur.core.config import ReplayAutomationConfig

        config = ReplayAutomationConfig()

        assert hasattr(config, "traffic_aware_enabled")
        assert hasattr(config, "traffic_aware_max_items")
        assert config.traffic_aware_enabled is False  # default
        assert config.traffic_aware_max_items == 30  # default
