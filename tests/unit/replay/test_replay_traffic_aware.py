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
        """Blocked when a circuit projecting onto the domain is open."""
        from baldur.tasks.traffic_aware_replay import check_traffic_health

        mock_cb = MagicMock()
        mock_cb.repository = MagicMock(spec=[])
        mock_cb.get_all_states.return_value = [
            {"service_name": "payment", "state": "open"}
        ]
        mock_cb_getter.return_value = mock_cb

        result = check_traffic_health(domain="payment")

        assert result.is_healthy is False
        assert "circuit_breaker" in result.checks
        assert result.checks["circuit_breaker"] is False
        assert "circuit_open" in result.reason
        # get_state is get-or-create — reading it would fabricate a CLOSED row
        # for a name this process has never seen and report it healthy.
        mock_cb.get_state.assert_not_called()

    @patch("baldur_pro.services.governance.checks.check_all_governance")
    @patch("baldur.services.circuit_breaker.get_circuit_breaker_service")
    def test_no_error_budget_leg_is_reported(self, mock_cb_getter, mock_governance):
        """The report carries no error-budget check.

        The leg it replaced consulted a gate that resolves to nothing on every
        install; the RuntimeError that raised landed in the generic handler and
        fail-opened, so the check reported a pass it had never made.
        """
        from baldur.services.circuit_breaker import CircuitBreakerService
        from baldur.tasks.traffic_aware_replay import check_traffic_health

        mock_cb = MagicMock(spec=CircuitBreakerService)
        mock_cb.repository = MagicMock(spec=[])
        mock_cb.get_all_states.return_value = []
        mock_cb_getter.return_value = mock_cb
        mock_governance.return_value = MagicMock(allowed=True, block_message="")

        result = check_traffic_health(domain=None)

        assert "error_budget" not in result.checks
        assert result.is_healthy is True

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
            CircuitProjection,
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
                projection = CircuitProjection()
                mock_health.return_value = TrafficHealthStatus.healthy(
                    checks={"circuit_breaker": True, "governance": True},
                    circuits=projection,
                )

                with patch.object(task, "_execute_replay") as mock_replay:
                    mock_replay.return_value = {"total": 10, "success": 8, "failed": 2}
                    result = task.run()
                    mock_replay.assert_called_once_with(None, 25, projection)

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
        """The registered object must be one a real Celery app can resolve."""
        celery = pytest.importorskip("celery")

        from baldur.tasks.traffic_aware_replay import (
            TrafficAwareReplayTask,
            register_traffic_aware_tasks_with_celery,
        )

        # set_as_current=False keeps this app from becoming the process-wide
        # current app and rebinding every @shared_task proxy to it.
        app = celery.Celery(
            "test_traffic_aware_registration",
            set_as_current=False,
        )
        register_traffic_aware_tasks_with_celery(app)

        # Against a MagicMock app this assertion held no matter what the
        # registrar produced, which is how a registrar that handed Celery a
        # bare instance with no bind() stayed green. A real app rejects that,
        # so the name lands in app.tasks only if a usable task was built.
        assert TrafficAwareReplayTask.name in app.tasks


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


def _governance_allowed():
    """A real allow verdict — the governance leg reads two of its fields."""
    from baldur.models.governance import GovernanceCheckResult

    return GovernanceCheckResult(allowed=True)


class TestCheckTrafficHealthBehavior:
    """The circuit leg after the error-budget leg was removed.

    The report now covers exactly what it checks: circuits projected into the
    namespace DLQ entries are stored under, plus governance. The snapshot it
    built rides back on the result so the pass that follows does not scan the
    store a second time.
    """

    @pytest.fixture(autouse=True)
    def _require_pro(self):
        # The governance leg is patched at its PRO implementation.
        pytest.importorskip("baldur_pro")

    def _health(self, states, *, domain, repository=None):
        from baldur.services.circuit_breaker import CircuitBreakerService
        from baldur.tasks.traffic_aware_replay import check_traffic_health

        cb = MagicMock(spec=CircuitBreakerService)
        cb.repository = repository if repository is not None else MagicMock(spec=[])
        cb.get_all_states.return_value = states
        with (
            patch(
                "baldur_pro.services.governance.checks.check_all_governance",
                return_value=_governance_allowed(),
            ),
            patch(
                "baldur.services.circuit_breaker.get_circuit_breaker_service",
                return_value=cb,
            ),
        ):
            return check_traffic_health(domain=domain)

    @pytest.mark.parametrize(
        ("states", "domain", "healthy", "reason_fragment"),
        [
            (
                [{"service_name": "Payment-API", "state": "closed"}],
                "payment_api",
                True,
                "",
            ),
            (
                [{"service_name": "payment_api", "state": "open"}],
                "payment_api",
                False,
                "circuit_open",
            ),
            ([], "payment_api", False, "no_circuit_projects"),
            # No domain named: the caller fans out itself, so the check only
            # has to prove the store is readable.
            ([], None, True, ""),
        ],
    )
    def test_domain_postures(self, states, domain, healthy, reason_fragment):
        result = self._health(states, domain=domain)

        assert result.is_healthy is healthy
        if reason_fragment:
            assert reason_fragment in result.reason

    def test_an_unrefreshable_store_stops_the_pass_rather_than_guessing(self):
        """Draining against a snapshot that may be stale is exactly the case
        the whole-store restore exists to prevent."""
        repository = MagicMock(spec=["force_sync_from_l2", "get_l2_health"])
        repository.force_sync_from_l2.return_value = False
        repository.get_l2_health.return_value = {"adapter_type": "redis"}

        result = self._health([], domain=None, repository=repository)

        assert result.is_healthy is False
        assert result.checks["circuit_breaker"] is False
        assert "refreshed" in result.reason

    def test_a_raising_circuit_read_fails_open(self):
        from baldur.tasks.traffic_aware_replay import check_traffic_health

        with (
            patch(
                "baldur_pro.services.governance.checks.check_all_governance",
                return_value=_governance_allowed(),
            ),
            patch(
                "baldur.services.circuit_breaker.get_circuit_breaker_service",
                side_effect=RuntimeError("store unreachable"),
            ),
        ):
            result = check_traffic_health(domain="payment_api")

        assert result.is_healthy is True
        assert result.checks["circuit_breaker"] is True
        assert result.circuits is None

    @pytest.mark.parametrize("domain", [None, "payment_api"])
    def test_the_report_never_carries_an_error_budget_check(self, domain):
        result = self._health(
            [{"service_name": "payment_api", "state": "closed"}], domain=domain
        )

        assert "error_budget" not in result.checks

    def test_the_snapshot_rides_back_for_the_pass_to_reuse(self):
        result = self._health(
            [{"service_name": "payment_api", "state": "closed"}], domain=None
        )

        assert result.circuits is not None
        assert result.circuits.drop_reason("payment_api") is None

    def test_the_snapshot_is_returned_even_when_the_domain_is_blocked(self):
        """The caller may still drain the domains that ARE closed."""
        result = self._health(
            [
                {"service_name": "payment_api", "state": "open"},
                {"service_name": "point_api", "state": "closed"},
            ],
            domain="payment_api",
        )

        assert result.is_healthy is False
        assert result.circuits is not None
        assert result.circuits.drop_reason("point_api") is None
