"""
Chaos Zombie Experiment GC Tests (385).

Tests for:
A. RunningExperimentInfo NamedTuple — fields, immutability
B. cleanup_zombie_experiment() — orchestration, idempotency, exception isolation
C. _get_experiment_instance() — lookup behavior
D. kill_all() / get_running_experiments() — RunningExperimentInfo compatibility
E. register/unregister_experiment_instance in _execute_experiment — prerequisite fix
F. experiment_timeout_seconds — boundary validation
"""

from __future__ import annotations

import pytest

pytest.importorskip("baldur_pro")

pytestmark = pytest.mark.requires_pro


import time
from unittest.mock import MagicMock, patch

import pytest

from baldur.services.event_bus.bus.event_types import EventType
from baldur_pro.services.chaos.scheduler.service import (
    ChaosSchedulerService,
    RunningExperimentInfo,
)

_SCHEDULER_MODULE = "baldur_pro.services.chaos.scheduler.service"


@pytest.fixture
def scheduler():
    """ChaosSchedulerService with default config."""
    return ChaosSchedulerService()


@pytest.fixture
def mock_schedule():
    """Mock ScheduledExperiment with standard attributes."""
    schedule = MagicMock()
    schedule.id = "sched-001"
    schedule.experiment_type = "latency_injection"
    schedule.target_service = "payment"
    schedule.target_domain = ""
    schedule.blast_radius = "instance"
    schedule.description = "Test latency"
    schedule.enabled = True
    schedule.approval_status = "auto_approved"
    schedule.experiment_config = {}
    schedule.run_count = 0
    return schedule


def _inject_running(scheduler, schedule_id, experiment_id, mono_offset=0.0):
    """Helper: inject a RunningExperimentInfo into _running_experiments."""
    info = RunningExperimentInfo(
        experiment_id=experiment_id,
        started_at_monotonic=time.monotonic() - mono_offset,
    )
    scheduler._running_experiments[schedule_id] = info
    return info


def _inject_instance(scheduler, experiment_id, status="running"):
    """Helper: inject a mock experiment instance into _experiment_instances."""
    instance = MagicMock()
    instance.experiment_id = experiment_id
    instance.status = status
    return (
        scheduler._experiment_instances.__setitem__(experiment_id, instance) or instance
    )


# =============================================================================
# A. RunningExperimentInfo NamedTuple
# =============================================================================


class TestRunningExperimentInfoContract:
    """Contract verification for RunningExperimentInfo NamedTuple (D-6)."""

    def test_fields_are_experiment_id_and_started_at_monotonic(self):
        """NamedTuple has exactly experiment_id and started_at_monotonic fields."""
        assert RunningExperimentInfo._fields == (
            "experiment_id",
            "started_at_monotonic",
        )

    def test_immutable_namedtuple(self):
        """RunningExperimentInfo instances are immutable."""
        info = RunningExperimentInfo(experiment_id="exp-1", started_at_monotonic=100.0)
        with pytest.raises(AttributeError):
            info.experiment_id = "changed"

    def test_construct_with_keyword_args(self):
        """Can construct with keyword arguments."""
        info = RunningExperimentInfo(experiment_id="exp-1", started_at_monotonic=42.5)
        assert info.experiment_id == "exp-1"
        assert info.started_at_monotonic == 42.5


# =============================================================================
# B. cleanup_zombie_experiment() — Orchestration
# =============================================================================


class TestCleanupZombieExperimentBehavior:
    """Behavior verification for cleanup_zombie_experiment() orchestration (D-11)."""

    def test_cleanup_with_instance_and_running_returns_true(
        self, scheduler, mock_schedule
    ):
        """Full cleanup path: instance exists + running entry → returns True."""
        # Given
        scheduler._schedules[mock_schedule.id] = mock_schedule
        instance = MagicMock()
        instance.experiment_id = "exp-001"
        scheduler._experiment_instances["exp-001"] = instance
        _inject_running(scheduler, mock_schedule.id, "exp-001")

        # When
        with patch.object(scheduler, "_emit_chaos_event") as mock_emit:
            result = scheduler.cleanup_zombie_experiment("exp-001", "zombie_hunter")

        # Then
        assert result is True
        instance.rollback.assert_called_once()
        assert "exp-001" not in scheduler._experiment_instances
        assert mock_schedule.id not in scheduler._running_experiments
        mock_emit.assert_called_once()
        call_args = mock_emit.call_args
        assert call_args[0][0] == EventType.CHAOS_EXPERIMENT_STOPPED
        assert call_args[0][1]["status"] == "aborted"
        assert call_args[0][1]["cleanup_source"] == "zombie_hunter"

    def test_cleanup_idempotent_second_call_returns_false(
        self, scheduler, mock_schedule
    ):
        """Second cleanup for same experiment returns False (D-8 idempotency)."""
        # Given — already cleaned
        scheduler._schedules[mock_schedule.id] = mock_schedule

        # When
        with patch.object(scheduler, "_emit_chaos_event"):
            result = scheduler.cleanup_zombie_experiment("exp-nonexist", "watchdog")

        # Then
        assert result is False

    def test_cleanup_instance_exists_but_running_gone_returns_false(self, scheduler):
        """Instance exists but _running_experiments already cleared → returns False (no STOPPED emitted)."""
        # Given
        instance = MagicMock()
        instance.experiment_id = "exp-002"
        scheduler._experiment_instances["exp-002"] = instance

        # When
        with patch.object(scheduler, "_emit_chaos_event") as mock_emit:
            result = scheduler.cleanup_zombie_experiment("exp-002", "watchdog")

        # Then — rollback executed but no STOPPED event, so returns False
        assert result is False
        instance.rollback.assert_called_once()
        mock_emit.assert_not_called()

    def test_cleanup_emits_stopped_with_schedule_fields(self, scheduler, mock_schedule):
        """Compensating STOPPED event includes schedule-based fields (D-12)."""
        # Given
        scheduler._schedules[mock_schedule.id] = mock_schedule
        instance = MagicMock()
        scheduler._experiment_instances["exp-001"] = instance
        _inject_running(scheduler, mock_schedule.id, "exp-001")

        # When
        with patch.object(scheduler, "_emit_chaos_event") as mock_emit:
            scheduler.cleanup_zombie_experiment("exp-001", "zombie_hunter")

        # Then
        data = mock_emit.call_args[0][1]
        assert data["experiment_type"] == "latency_injection"
        assert data["target_service"] == "payment"
        assert data["description"] == "Test latency"

    def test_cleanup_deleted_schedule_uses_unknown_fallback(self, scheduler):
        """When schedule is deleted, event fields fall back to 'unknown' (H-5)."""
        # Given — no schedule registered, but running entry exists
        _inject_running(scheduler, "deleted-sched", "exp-003")

        # When
        with patch.object(scheduler, "_emit_chaos_event") as mock_emit:
            result = scheduler.cleanup_zombie_experiment("exp-003", "watchdog")

        # Then
        assert result is True
        data = mock_emit.call_args[0][1]
        assert data["experiment_type"] == "unknown"
        assert data["target_service"] == "unknown"
        assert data["description"] == "deleted-sched"

    def test_cleanup_rollback_exception_does_not_block_unregister(
        self, scheduler, mock_schedule
    ):
        """Rollback failure does not prevent unregister or running cleanup (H-7)."""
        # Given
        scheduler._schedules[mock_schedule.id] = mock_schedule
        instance = MagicMock()
        instance.rollback.side_effect = RuntimeError("rollback failed")
        scheduler._experiment_instances["exp-004"] = instance
        _inject_running(scheduler, mock_schedule.id, "exp-004")

        # When
        with patch.object(scheduler, "_emit_chaos_event"):
            result = scheduler.cleanup_zombie_experiment("exp-004", "zombie_hunter")

        # Then — cleanup still succeeds despite rollback error
        assert result is True
        assert mock_schedule.id not in scheduler._running_experiments

    def test_cleanup_unregister_exception_does_not_block_running_cleanup(
        self, scheduler, mock_schedule
    ):
        """Unregister failure does not prevent _running_experiments cleanup (H-7)."""
        # Given
        scheduler._schedules[mock_schedule.id] = mock_schedule
        instance = MagicMock()
        scheduler._experiment_instances["exp-005"] = instance
        _inject_running(scheduler, mock_schedule.id, "exp-005")

        # When
        with patch.object(
            scheduler,
            "unregister_experiment_instance",
            side_effect=RuntimeError("lock error"),
        ):
            with patch.object(scheduler, "_emit_chaos_event"):
                result = scheduler.cleanup_zombie_experiment("exp-005", "zombie_hunter")

        # Then — running entry still cleaned
        assert result is True
        assert mock_schedule.id not in scheduler._running_experiments

    def test_cleanup_no_rollback_attribute_skips_rollback(
        self, scheduler, mock_schedule
    ):
        """Instance without rollback attribute is handled gracefully."""
        # Given
        scheduler._schedules[mock_schedule.id] = mock_schedule
        instance = MagicMock(spec=[])  # no attributes
        instance.experiment_id = "exp-006"
        scheduler._experiment_instances["exp-006"] = instance
        _inject_running(scheduler, mock_schedule.id, "exp-006")

        # When
        with patch.object(scheduler, "_emit_chaos_event"):
            result = scheduler.cleanup_zombie_experiment("exp-006", "watchdog")

        # Then
        assert result is True


# =============================================================================
# C. _get_experiment_instance()
# =============================================================================


class TestGetExperimentInstanceBehavior:
    """Behavior verification for _get_experiment_instance() internal accessor."""

    def test_returns_instance_when_registered(self, scheduler):
        """Returns the experiment instance when it exists."""
        instance = MagicMock()
        scheduler._experiment_instances["exp-1"] = instance
        assert scheduler._get_experiment_instance("exp-1") is instance

    def test_returns_none_when_not_registered(self, scheduler):
        """Returns None for unregistered experiment ID."""
        assert scheduler._get_experiment_instance("nonexistent") is None


# =============================================================================
# D. kill_all() / get_running_experiments() — RunningExperimentInfo
# =============================================================================


class TestKillAllRunningExperimentInfoBehavior:
    """Behavior verification for kill_all() with RunningExperimentInfo value type."""

    def test_kill_all_unpacks_info_experiment_id(self, scheduler):
        """kill_all correctly unpacks info.experiment_id from RunningExperimentInfo."""
        _inject_running(scheduler, "sched-1", "exp-1")
        _inject_running(scheduler, "sched-2", "exp-2")

        with patch.object(scheduler, "kill_experiment", return_value=True) as mock_kill:
            count = scheduler.kill_all(reason="test")

        assert count == 2
        call_ids = {call.args[0] for call in mock_kill.call_args_list}
        assert call_ids == {"exp-1", "exp-2"}

    def test_kill_all_calls_request_kill_on_experiment_instances(self, scheduler):
        """kill_all calls experiment.request_kill() for in-memory kill (D-10)."""
        # Given
        _inject_running(scheduler, "sched-1", "exp-1")
        mock_experiment = MagicMock()
        scheduler._experiment_instances["exp-1"] = mock_experiment

        # When
        with patch.object(scheduler, "kill_experiment", return_value=True):
            scheduler.kill_all(reason="graceful_shutdown")

        # Then
        mock_experiment.request_kill.assert_called_once_with("graceful_shutdown")

    def test_kill_all_skips_request_kill_when_no_instance(self, scheduler):
        """kill_all skips request_kill when experiment instance not in dict."""
        # Given — running experiment but no instance registered
        _inject_running(scheduler, "sched-1", "exp-1")
        assert "exp-1" not in scheduler._experiment_instances

        # When — should not raise
        with patch.object(scheduler, "kill_experiment", return_value=True):
            count = scheduler.kill_all(reason="test")

        # Then
        assert count == 1


class TestGetRunningExperimentsBehavior:
    """Behavior verification for get_running_experiments() return type."""

    def test_returns_dict_of_running_experiment_info(self, scheduler):
        """get_running_experiments returns dict[str, RunningExperimentInfo]."""
        _inject_running(scheduler, "sched-1", "exp-1")
        result = scheduler.get_running_experiments()
        assert isinstance(result, dict)
        info = result["sched-1"]
        assert isinstance(info, RunningExperimentInfo)
        assert info.experiment_id == "exp-1"

    def test_returns_copy_not_reference(self, scheduler):
        """Returned dict is a copy — mutations don't affect internal state."""
        _inject_running(scheduler, "sched-1", "exp-1")
        result = scheduler.get_running_experiments()
        result.clear()
        assert len(scheduler.get_running_experiments()) == 1


# =============================================================================
# E. register/unregister in _execute_experiment
# =============================================================================


class TestExecuteExperimentRegistrationBehavior:
    """Behavior verification for register/unregister calls in _execute_experiment (Fix 1/2)."""

    def test_execute_experiment_registers_and_unregisters(
        self, scheduler, mock_schedule
    ):
        """_execute_experiment registers instance before execute and unregisters in finally."""
        from baldur.utils.time import utc_now

        mock_experiment = MagicMock()
        mock_result = MagicMock()
        mock_result.status = "completed"
        mock_result.dry_run = False
        mock_result.to_dict.return_value = {}
        mock_experiment.execute.return_value = mock_result

        with (
            patch(
                "baldur_pro.services.chaos.experiments.create_experiment",
                return_value=mock_experiment,
            ),
            patch("baldur_pro.services.chaos.safety_guard.get_safety_guard"),
            patch("baldur_pro.services.chaos.blast_radius.get_blast_radius_manager"),
            patch.object(scheduler, "_update_schedule_after_execution"),
            patch.object(scheduler, "_mark_idempotency_processed"),
        ):
            scheduler._execute_experiment(
                mock_schedule,
                "sched-001",
                "exp-001",
                utc_now(),
                force=True,
            )

        # After execution, instance should be unregistered (finally block)
        assert "exp-001" not in scheduler._experiment_instances


# =============================================================================
# F. experiment_timeout_seconds — Boundary (Contract)
# =============================================================================


class TestExperimentTimeoutSettingsContract:
    """Contract verification for experiment_timeout_seconds setting (Section 5)."""

    def test_default_value_is_7200(self):
        """Default changed from 300 to 7200 per 385 document."""
        from baldur.settings.chaos import ChaosSettings

        settings = ChaosSettings()
        assert settings.experiment_timeout_seconds == 7200

    def test_minimum_boundary_300(self):
        """ge=300: 299 fails, 300 passes."""
        from pydantic import ValidationError

        from baldur.settings.chaos import ChaosSettings

        with pytest.raises(ValidationError):
            ChaosSettings(experiment_timeout_seconds=299)
        settings = ChaosSettings(experiment_timeout_seconds=300)
        assert settings.experiment_timeout_seconds == 300

    def test_maximum_boundary_14400(self):
        """le=14400: 14400 passes, 14401 fails."""
        from pydantic import ValidationError

        from baldur.settings.chaos import ChaosSettings

        settings = ChaosSettings(experiment_timeout_seconds=14400)
        assert settings.experiment_timeout_seconds == 14400
        with pytest.raises(ValidationError):
            ChaosSettings(experiment_timeout_seconds=14401)
