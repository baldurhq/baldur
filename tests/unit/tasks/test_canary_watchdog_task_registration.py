"""Canary watchdog Celery task registration and the unregistered-service guard.

Two halves of "the lane actually runs":

    A. Registration — the three functions carry ``@shared_task`` names, so a
       worker resolves the strings beat publishes. The registration is
       unconditional and lives outside the composition gate: an entitled
       process composes entries, but a *worker* is often a different process
       that never composed anything and still has to resolve the names.
    B. The service guard — ``configure_baldur_celery()`` does not register the
       PRO canary rollout service; ``baldur.init()`` does. A worker that only
       ran the former has an empty registry slot, so each task warns once and
       reports a skip rather than raising on a 1-to-5-minute cadence.

No PRO import: the guard's whole job is behaving well when PRO is not wired.
"""

from __future__ import annotations

from unittest.mock import Mock, patch

import pytest

from baldur.factory.registry import ProviderRegistry
from baldur.interfaces.canary import CanaryRolloutService
from baldur.tasks.canary_watchdog import (
    _canary_service_registered,
    _service_unregistered_result,
    auto_promote_eligible,
    collect_canary_metrics,
    reset_watchdog,
    scan_zombie_rollouts,
)

_DOTTED_TASK_NAMES = (
    "baldur.tasks.canary_watchdog.scan_zombie_rollouts",
    "baldur.tasks.canary_watchdog.auto_promote_eligible",
    "baldur.tasks.canary_watchdog.collect_canary_metrics",
)

# (task callable, the name it carries into the skip log)
_LANE_TASKS = (
    (scan_zombie_rollouts, "scan_zombie_rollouts"),
    (auto_promote_eligible, "auto_promote_eligible"),
    (collect_canary_metrics, "collect_canary_metrics"),
)


@pytest.fixture
def unregistered_canary_service():
    """Empty the canary service slot for the duration of one test.

    Emptied explicitly rather than assumed: this suite runs with the PRO
    distribution installed, so a neighbouring test that booted the framework
    can leave the slot populated and every guard case here would pass while
    asserting the opposite of what it claims.
    """
    with ProviderRegistry.canary_rollout_service.snapshot():
        ProviderRegistry.canary_rollout_service.reset()
        yield


@pytest.fixture
def registered_canary_service():
    """Put a stand-in canary service in the slot and hand it to the test.

    Specced against the OSS Protocol the tasks resolve, so a task calling a
    method the Protocol does not declare raises here instead of quietly
    recording an attribute nobody implements.
    """
    service = Mock(spec=CanaryRolloutService)
    service.get_active_rollouts.return_value = []
    with ProviderRegistry.canary_rollout_service.override(service):
        yield service


@pytest.fixture(autouse=True)
def _reset_watchdog_singleton(monkeypatch):
    """Isolate the process-wide watchdog and the settings it is built from.

    The tasks resolve the watchdog singleton, whose config now comes from the
    settings singleton — so an ambient ``BALDUR_CANARY_WATCHDOG_*`` value would
    otherwise decide what the opt-in cases below observe.

    The skip warning's once-per-task dedup set is process-wide for the same
    reason it exists, so it is emptied here too: otherwise the first case to
    warn for a task name would silence every later case asserting that warning.
    """
    from baldur.settings.canary_watchdog import reset_canary_watchdog_settings
    from baldur.tasks.canary_watchdog import _service_unregistered_warned

    for var in (
        "BALDUR_CANARY_WATCHDOG_ENABLE_AUTO_PROMOTE",
        "BALDUR_CANARY_WATCHDOG_ENABLE_AUTO_ROLLBACK",
    ):
        monkeypatch.delenv(var, raising=False)

    _service_unregistered_warned.clear()
    reset_canary_watchdog_settings()
    reset_watchdog()
    yield
    reset_watchdog()
    reset_canary_watchdog_settings()
    _service_unregistered_warned.clear()


# =============================================================================
# A. Celery task registration
# =============================================================================


class TestCanaryWatchdogTaskRegistrationContract:
    """The three dotted names resolve against a Celery app."""

    @staticmethod
    def _throwaway_app_tasks() -> set[str]:
        """Task names a fresh Celery app knows about.

        ``set_as_current=False`` is load-bearing: a new app otherwise becomes
        the process-wide current one and rebinds every ``@shared_task`` proxy to
        it, so later tests in the same session would patch one task object and
        have the proxy resolve to another.
        """
        celery = pytest.importorskip("celery")

        current_before = celery.current_app._get_current_object()
        app = celery.Celery(
            "canary_watchdog_registration_probe",
            set_as_current=False,
        )
        names = set(app.tasks.keys())

        assert celery.current_app._get_current_object() is current_before, (
            "the probe hijacked the current Celery app — every @shared_task "
            "proxy in the process now resolves to the throwaway app"
        )
        return names

    def test_every_lane_task_is_registered(self):
        """Beat publishes these three strings; a worker must resolve all three.

        Before the registration landed, every default Celery install published
        them on a 1/2/5-minute cadence and the worker answered NotRegistered.
        """
        registered = self._throwaway_app_tasks()

        assert set(_DOTTED_TASK_NAMES) <= registered

    def test_registration_survives_a_gated_out_lane(self, mock_oss_tier):
        """Composition and registration are independent.

        The gate decides whether *this* process composes beat entries; the
        registration decides whether *a worker* can resolve names some other
        process composed. Tying them together would leave an entitled beat
        process publishing to workers that cannot answer.
        """
        from baldur.tasks.canary_watchdog import get_canary_watchdog_beat_schedule

        assert get_canary_watchdog_beat_schedule() == {}
        assert set(_DOTTED_TASK_NAMES) <= self._throwaway_app_tasks()


# =============================================================================
# B. Unregistered-service guard
# =============================================================================


class TestCanaryWatchdogTaskGuardBehavior:
    """An empty canary slot skips cleanly instead of raising on every tick."""

    def test_empty_registry_slot_reads_as_unregistered(
        self, unregistered_canary_service
    ):
        """The probe answers the question the tasks ask before doing anything."""
        assert _canary_service_registered() is False

    def test_populated_registry_slot_reads_as_registered(
        self, registered_canary_service
    ):
        """A worker that ran init() resolves the slot and the lane proceeds."""
        assert _canary_service_registered() is True

    def test_skip_result_reports_success_without_work(self):
        """A skip is not a failure: beat must not see a task erroring on cadence.

        Reporting ``skipped`` alongside ``success`` keeps the result honest —
        nothing was scanned, and nothing went wrong either.
        """
        result = _service_unregistered_result("scan_zombie_rollouts")

        assert result == {
            "success": True,
            "skipped": True,
            "reason": "service_unregistered",
        }

    def test_skip_logs_exactly_one_warning_naming_the_task(self):
        """WARNING, not DEBUG: on an entitled install an empty slot is a
        misconfiguration, and the hint names the call the operator is missing."""
        import baldur.tasks.canary_watchdog as watchdog_module

        with patch.object(watchdog_module, "logger") as mock_logger:
            _service_unregistered_result("auto_promote_eligible")

        mock_logger.warning.assert_called_once()
        assert mock_logger.warning.call_args.args[0] == "canary_watchdog.task_skipped"
        assert mock_logger.warning.call_args.kwargs["task"] == "auto_promote_eligible"
        assert mock_logger.warning.call_args.kwargs["reason"] == "service_unregistered"
        assert "baldur.init()" in mock_logger.warning.call_args.kwargs["hint"]

    def test_repeat_skip_warns_once_and_then_drops_to_debug(self):
        """The dedup that makes the WARNING level affordable.

        These tasks tick every 1, 2 and 5 minutes. An undeduplicated line at
        WARNING would print thousands of times a day on one misconfigured
        worker, and an operator who learns to filter it loses the one line that
        told them ``baldur.init()`` is missing. Later ticks stay visible at
        DEBUG, which costs nothing in a production log configuration.
        """
        import baldur.tasks.canary_watchdog as watchdog_module

        with patch.object(watchdog_module, "logger") as mock_logger:
            for _tick in range(5):
                _service_unregistered_result("scan_zombie_rollouts")

        assert mock_logger.warning.call_count == 1
        assert [call.args[0] for call in mock_logger.debug.call_args_list] == [
            "canary_watchdog.task_skipped"
        ] * 4

    def test_dedup_is_scoped_per_task_not_per_process(self):
        """Three tasks, three first warnings — one silencing the others would
        leave two of the lane's three jobs with no diagnostic at all."""
        import baldur.tasks.canary_watchdog as watchdog_module

        with patch.object(watchdog_module, "logger") as mock_logger:
            for _task, task_name in _LANE_TASKS:
                _service_unregistered_result(task_name)
                _service_unregistered_result(task_name)

        assert [call.kwargs["task"] for call in mock_logger.warning.call_args_list] == [
            task_name for _task, task_name in _LANE_TASKS
        ]

    @pytest.mark.parametrize(
        ("task", "task_name"),
        _LANE_TASKS,
        ids=[name for _task, name in _LANE_TASKS],
    )
    def test_task_skips_without_raising_when_service_is_unregistered(
        self, unregistered_canary_service, task, task_name
    ):
        """Every task in the lane takes the skip path, labelled with its own name."""
        import baldur.tasks.canary_watchdog as watchdog_module

        with patch.object(watchdog_module, "logger") as mock_logger:
            result = task()

        assert result["skipped"] is True
        assert result["reason"] == "service_unregistered"
        assert mock_logger.warning.call_args.kwargs["task"] == task_name

    @pytest.mark.parametrize(
        ("task", "task_name"),
        _LANE_TASKS,
        ids=[name for _task, name in _LANE_TASKS],
    )
    def test_task_does_its_own_work_when_service_is_registered(
        self, registered_canary_service, task, task_name
    ):
        """The guard is a guard, not a permanent short-circuit.

        With the slot populated each task runs its own body and reports a real
        result — no ``skipped`` key — which is what keeps the skip arm above
        from passing for the wrong reason.
        """
        result = task()

        assert "skipped" not in result
        assert result["success"] is True

    @pytest.mark.parametrize(
        "task",
        [scan_zombie_rollouts, collect_canary_metrics],
        ids=["scan_zombie_rollouts", "collect_canary_metrics"],
    )
    def test_non_mutating_task_reaches_the_service(
        self, registered_canary_service, task
    ):
        """Lock renewal and metric collection come alive with registration.

        These two are the non-mutating half of the lane and carry no opt-in
        flag, so a populated slot is the only thing they were waiting for.
        """
        task()

        registered_canary_service.get_active_rollouts.assert_called()

    def test_auto_promote_stops_at_the_opt_in_flag_before_the_service(
        self, registered_canary_service
    ):
        """Activation is behaviour-preserving: registering does not start promoting.

        ``enable_auto_promote`` defaults off, so the task returns before it ever
        reads the rollout list. Without this assertion the defaults flip is only
        visible as a settings value, never as the behaviour it buys.
        """
        result = auto_promote_eligible()

        assert result["promote_count"] == 0
        registered_canary_service.get_active_rollouts.assert_not_called()
        registered_canary_service.promote.assert_not_called()
