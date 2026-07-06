"""
Chaos Event Pipeline Integration Test (380).

End-to-end verification: ChaosSchedulerService.execute_now() emits events →
EventBus delivers to registered default handlers → handlers log + increment metrics.

No infra dependency (in-memory EventBus).

Test Categories:
    A. Event delivery:
        - BLOCKED event reaches handler with correct log + metrics
        - STARTED + STOPPED events reach handlers on success
        - STOPPED(error) event reaches handler with WARNING log level
    B. Feature toggle:
        - Handlers return early when chaos settings disabled
    C. Registration:
        - All 3 chaos handler types registered by default_handlers
"""

from __future__ import annotations

import pytest

pytest.importorskip("baldur_pro")

pytestmark = pytest.mark.requires_pro


from unittest.mock import MagicMock, patch

import pytest

from baldur.services.event_bus.bus.convenience import (
    get_event_bus,
    reset_event_bus,
)
from baldur.services.event_bus.bus.default_handlers import (
    register_default_handlers,
)
from baldur.services.event_bus.bus.event_types import EventType
from baldur_pro.services.chaos.scheduler.service import ChaosSchedulerService


@pytest.fixture(autouse=True)
def _reset_event_bus():
    """Reset EventBus before and after each test for isolation."""
    reset_event_bus()
    yield
    reset_event_bus()


@pytest.fixture
def scheduler_with_bus():
    """Create scheduler wired to real EventBus with default handlers registered."""
    register_default_handlers()
    scheduler = ChaosSchedulerService()
    scheduler._event_bus = get_event_bus()
    return scheduler


@pytest.fixture
def mock_schedule():
    """Mock ScheduledExperiment."""
    schedule = MagicMock()
    schedule.id = "sched-integ-001"
    schedule.experiment_type = "latency_injection"
    schedule.target_service = "payment"
    schedule.target_domain = ""
    schedule.blast_radius = "instance"
    schedule.description = "Integration test latency"
    schedule.enabled = True
    schedule.approval_status = "auto_approved"
    schedule.experiment_config = {}
    schedule.run_count = 0
    return schedule


class TestChaosEventPipelineIntegration:
    """Integration: scheduler → EventBus → default handlers."""

    def test_blocked_event_reaches_handler(self, scheduler_with_bus, mock_schedule):
        """
        Purpose:
            Blocked experiment emits BLOCKED event that reaches default handler.
        Expected:
            - Handler logs WARNING with event_bus.chaos_experiment_blocked
            - Metrics counter incremented with status=blocked
        """
        scheduler = scheduler_with_bus
        scheduler._schedules[mock_schedule.id] = mock_schedule

        # Given — pre-execution returns blocked
        blocked_result = MagicMock()
        blocked_result.skip_reason = "Scheduler is disabled"
        blocked_result.status = "skipped"

        with (
            patch.object(
                scheduler, "_run_pre_execution_checks", return_value=blocked_result
            ),
            patch(
                "baldur.settings.chaos.get_chaos_settings",
            ) as mock_settings,
            patch(
                "baldur.services.event_bus.bus._chaos_handlers.logger",
            ) as mock_handler_logger,
            patch(
                "baldur.services.event_bus.bus._chaos_handlers._get_chaos_handler_counter",
            ) as mock_get_counter,
        ):
            mock_settings.return_value = MagicMock(enabled=True)
            mock_counter = MagicMock()
            mock_get_counter.return_value = mock_counter

            # When
            scheduler.execute_now(mock_schedule.id)

            # Then — handler received the event
            event_names = [
                c.args[0] for c in mock_handler_logger.warning.call_args_list
            ]
            assert "event_bus.chaos_experiment_blocked" in event_names
            mock_counter.labels.assert_called_with(status="blocked")

    def test_success_events_reach_handlers(self, scheduler_with_bus, mock_schedule):
        """
        Purpose:
            Successful experiment emits STARTED then STOPPED, both reaching handlers.
        Expected:
            - STARTED handler logs DEBUG with event_bus.chaos_experiment_started
            - STOPPED handler logs INFO with event_bus.chaos_experiment_stopped
            - Metrics incremented for both started and completed
        """
        scheduler = scheduler_with_bus
        scheduler._schedules[mock_schedule.id] = mock_schedule

        exec_result = MagicMock()
        exec_result.status = "completed"
        exec_result.duration_seconds = 1.0
        exec_result.success = True
        exec_result.dry_run = False

        with (
            patch.object(scheduler, "_run_pre_execution_checks", return_value=None),
            patch.object(scheduler, "_execute_experiment", return_value=exec_result),
            patch(
                "baldur.settings.chaos.get_chaos_settings",
            ) as mock_settings,
            patch(
                "baldur.services.event_bus.bus._chaos_handlers.logger",
            ) as mock_handler_logger,
            patch(
                "baldur.services.event_bus.bus._chaos_handlers._get_chaos_handler_counter",
            ) as mock_get_counter,
        ):
            mock_settings.return_value = MagicMock(enabled=True)
            mock_counter = MagicMock()
            mock_get_counter.return_value = mock_counter

            # When
            scheduler.execute_now(mock_schedule.id)

            # Then — STARTED handler called (DEBUG)
            debug_events = [c.args[0] for c in mock_handler_logger.debug.call_args_list]
            assert "event_bus.chaos_experiment_started" in debug_events

            # Then — STOPPED handler called (INFO for success)
            info_events = [c.args[0] for c in mock_handler_logger.info.call_args_list]
            assert "event_bus.chaos_experiment_stopped" in info_events

            # Then — metrics incremented for both started and completed
            label_calls = [c[1]["status"] for c in mock_counter.labels.call_args_list]
            assert "started" in label_calls
            assert "completed" in label_calls

    def test_error_event_reaches_handler_with_warning(
        self, scheduler_with_bus, mock_schedule
    ):
        """
        Purpose:
            Failed experiment emits STARTED + STOPPED(error), handler uses WARNING.
        Expected:
            - STOPPED handler logs WARNING with event_bus.chaos_experiment_stopped
            - Metrics incremented with status=error
        """
        scheduler = scheduler_with_bus
        scheduler._config.dry_run_mode = False
        scheduler._schedules[mock_schedule.id] = mock_schedule

        with (
            patch.object(scheduler, "_run_pre_execution_checks", return_value=None),
            patch.object(
                scheduler,
                "_execute_experiment",
                side_effect=RuntimeError("test failure"),
            ),
            patch(
                "baldur.settings.chaos.get_chaos_settings",
            ) as mock_settings,
            patch(
                "baldur.services.event_bus.bus._chaos_handlers.logger",
            ) as mock_handler_logger,
            patch(
                "baldur.services.event_bus.bus._chaos_handlers._get_chaos_handler_counter",
            ) as mock_get_counter,
        ):
            mock_settings.return_value = MagicMock(enabled=True)
            mock_counter = MagicMock()
            mock_get_counter.return_value = mock_counter

            # When
            scheduler.execute_now(mock_schedule.id)

            # Then — STOPPED(error) handler uses WARNING (not success, not dry_run)
            warning_events = [
                c.args[0] for c in mock_handler_logger.warning.call_args_list
            ]
            assert "event_bus.chaos_experiment_stopped" in warning_events

            label_calls = [c[1]["status"] for c in mock_counter.labels.call_args_list]
            assert "error" in label_calls

    def test_handler_disabled_does_not_process_events(
        self, scheduler_with_bus, mock_schedule
    ):
        """
        Purpose:
            When chaos settings disabled, handlers return early without processing.
        Expected:
            - No debug/info/warning log calls from handler logger
        """
        scheduler = scheduler_with_bus
        scheduler._schedules[mock_schedule.id] = mock_schedule

        exec_result = MagicMock()
        exec_result.status = "completed"
        exec_result.duration_seconds = 0.5
        exec_result.success = True
        exec_result.dry_run = False

        with (
            patch.object(scheduler, "_run_pre_execution_checks", return_value=None),
            patch.object(scheduler, "_execute_experiment", return_value=exec_result),
            patch(
                "baldur.settings.chaos.get_chaos_settings",
            ) as mock_settings,
            patch(
                "baldur.services.event_bus.bus._chaos_handlers.logger",
            ) as mock_handler_logger,
        ):
            mock_settings.return_value = MagicMock(enabled=False)

            # When
            scheduler.execute_now(mock_schedule.id)

            # Then — no handler logging occurred
            mock_handler_logger.debug.assert_not_called()
            mock_handler_logger.info.assert_not_called()
            mock_handler_logger.warning.assert_not_called()

    def test_default_handlers_registers_all_three_chaos_handlers(self):
        """
        Purpose:
            Verify register_default_handlers() registers all 3 chaos event types.
        Expected:
            - BLOCKED, STARTED, STOPPED each have their handler subscribed
        """
        register_default_handlers()
        bus = get_event_bus()

        blocked_subs = bus.get_subscriptions(EventType.CHAOS_EXPERIMENT_BLOCKED)
        started_subs = bus.get_subscriptions(EventType.CHAOS_EXPERIMENT_STARTED)
        stopped_subs = bus.get_subscriptions(EventType.CHAOS_EXPERIMENT_STOPPED)

        # Verify handler presence
        blocked_names = [s["handler_name"] for s in blocked_subs]
        started_names = [s["handler_name"] for s in started_subs]
        stopped_names = [s["handler_name"] for s in stopped_subs]

        assert "_on_chaos_experiment_blocked" in blocked_names
        assert "_on_chaos_experiment_started" in started_names
        assert "_on_chaos_experiment_stopped" in stopped_names
