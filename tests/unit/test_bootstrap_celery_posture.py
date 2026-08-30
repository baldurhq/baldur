"""Bootstrap-side Celery starter-deferral posture.

Deferring the background starters is the *designed* posture in a Celery worker
main process on a forking pool — the pool children start them. It is a defect
only when the deferral was decided from argv and no Celery worker signal ever
confirmed it, which means the adapter's bootstrap receivers were never
connected and nothing will un-defer.

Four surfaces implement that separation and are covered here:

- ``_arm_celery_bootstrap_receivers()`` — the third arming site, inside
  ``init()`` itself, for Django+Celery deployments that call neither adapter
  entry point. Gated so it never pulls celery into a process that lacked it.
- ``_schedule_celery_deferral_check()`` — the watchdog, armed for the
  argv-only deferral and nothing else.
- ``reconcile_celery_deferral_posture()`` — what a worker signal settles:
  cancel the watchdog, and report the one posture the watchdog cannot see.
- ``start_background_workers()`` — records which posture this process took, so
  the reconciliation can tell a designed deferral from threads about to be
  forked away.

Timer mechanics: ``threading.Timer`` is replaced by a recording double
(``UNIT_TEST_GUIDELINES.md`` §6.5.6) so nothing sleeps the configured delay and
the callback is driven explicitly.
"""

from __future__ import annotations

import sys
import threading
from unittest.mock import patch

import pytest

import baldur.bootstrap as bootstrap_module
from baldur.adapters.celery.bootstrap_hooks import (
    disconnect_celery_bootstrap_receivers,
    is_celery_bootstrap_receivers_connected,
)

_ADAPTER_MODULE = "baldur.adapters.celery.bootstrap_hooks"


class _RecordingTimer:
    """``threading.Timer`` double that records instead of scheduling.

    Matches the real Timer's constructor signature and the ``daemon`` attribute
    bootstrap sets immediately after construction. The callback is never run on
    a thread — tests invoke ``run_callback()`` when they want it.
    """

    created: list[_RecordingTimer] = []

    def __init__(self, interval, function, args=None, kwargs=None):
        self.interval = interval
        self.function = function
        self.args = args or ()
        self.kwargs = kwargs or {}
        self.daemon = False
        self.started = False
        self.cancelled = False
        _RecordingTimer.created.append(self)

    def start(self):
        self.started = True

    def cancel(self):
        self.cancelled = True

    def run_callback(self):
        self.function(*self.args, **self.kwargs)


@pytest.fixture
def recording_timer(monkeypatch):
    """Swap ``threading.Timer`` for the recording double and hand back the log."""
    _RecordingTimer.created = []
    monkeypatch.setattr(threading, "Timer", _RecordingTimer)
    return _RecordingTimer.created


@pytest.fixture(autouse=True)
def _isolated_posture_state():
    """Reset every process-global this module writes, before and after."""
    bootstrap_module._reset_celery_deferral_state()
    original_deferred = bootstrap_module._background_starters_deferred
    original_init_done = bootstrap_module._init_done
    try:
        yield
    finally:
        bootstrap_module._reset_celery_deferral_state()
        bootstrap_module._background_starters_deferred = original_deferred
        bootstrap_module._init_done = original_init_done


@pytest.fixture
def celery_worker_argv_process():
    """Present this process as a celery worker known only by argv.

    The one combination the watchdog exists for: no ``worker_init`` signal
    observed, not serving, and an argv shape that says "celery worker".
    """
    with (
        patch(
            "baldur.core.process_utils.is_celery_worker_main",
            return_value=False,
        ),
        patch(
            "baldur.core.process_utils.is_celery_worker_serving",
            return_value=False,
        ),
        patch(
            "baldur.core.process_utils.is_celery_worker_process",
            return_value=True,
        ),
    ):
        yield


class TestCeleryReceiverArmingBehavior:
    """``_arm_celery_bootstrap_receivers()`` — arming from inside ``init()``.

    Two conditions, both required: this process must look like a Celery worker,
    and celery must already be imported — which the predicate itself requires,
    because arming must never be the thing that pulls celery into a process that
    had not loaded it.
    """

    @pytest.fixture(autouse=True)
    def _receivers_disconnected(self):
        disconnect_celery_bootstrap_receivers()
        try:
            yield
        finally:
            disconnect_celery_bootstrap_receivers()

    def test_celery_worker_process_gets_the_receivers_connected(self):
        """The Django+Celery deployment that calls neither entry point."""
        with patch(
            "baldur.core.process_utils.is_celery_worker_process",
            return_value=True,
        ):
            bootstrap_module._arm_celery_bootstrap_receivers()

        assert is_celery_bootstrap_receivers_connected() is True

    def test_non_celery_process_connects_nothing(self):
        """A web process that merely imports the Celery app must stay clean."""
        with patch(
            "baldur.core.process_utils.is_celery_worker_process",
            return_value=False,
        ):
            bootstrap_module._arm_celery_bootstrap_receivers()

        assert is_celery_bootstrap_receivers_connected() is False

    def test_non_celery_process_does_not_import_the_celery_adapter(self, monkeypatch):
        """The predicate is checked *before* the adapter import, not after.

        The adapter module imports ``celery.signals`` at its own module scope,
        so importing it first and deciding afterwards would be exactly the
        "arming pulled celery in" outcome the gate exists to prevent.
        """
        # Given — the adapter module is provably not loaded
        monkeypatch.delitem(sys.modules, _ADAPTER_MODULE, raising=False)

        # When
        with patch(
            "baldur.core.process_utils.is_celery_worker_process",
            return_value=False,
        ):
            bootstrap_module._arm_celery_bootstrap_receivers()

        # Then
        assert _ADAPTER_MODULE not in sys.modules

    def test_plain_argv_process_arms_nothing_under_the_real_predicate(
        self, monkeypatch
    ):
        """End-to-end through the real detection, not the patched one."""
        monkeypatch.setattr(sys, "argv", ["python", "manage.py", "runserver"])

        bootstrap_module._arm_celery_bootstrap_receivers()

        assert is_celery_bootstrap_receivers_connected() is False

    def test_arming_failure_does_not_propagate(self):
        """Fail-soft: a missing celery extra leaves the un-armed behavior.

        The deferral watchdog is what reports the resulting gap; raising here
        would abort an ``init()`` over a diagnostic.
        """
        with (
            patch(
                "baldur.core.process_utils.is_celery_worker_process",
                return_value=True,
            ),
            patch(
                f"{_ADAPTER_MODULE}.connect_celery_bootstrap_receivers",
                side_effect=ImportError("celery extra not installed"),
            ) as connect,
            patch.object(bootstrap_module, "logger") as logger,
        ):
            bootstrap_module._arm_celery_bootstrap_receivers()

        connect.assert_called_once_with()
        assert (
            logger.debug.call_args.args[0]
            == "baldur.celery_bootstrap_receivers_arm_failed"
        )


class TestCeleryDeferralWatchdogBehavior:
    """``_schedule_celery_deferral_check()`` — armed for the argv-only deferral.

    A healthy prefork parent arms nothing: its ``worker_init`` receiver sets the
    signal-based flag before ``init()`` runs, so the first check returns. The
    naive alternative — warn whenever the starters were deferred — fires on
    *every* healthy prefork worker.
    """

    def test_argv_only_deferral_arms_a_daemon_timer(
        self, recording_timer, celery_worker_argv_process
    ):
        """The one combination with nothing to confirm or correct the deferral."""
        bootstrap_module._schedule_celery_deferral_check()

        assert len(recording_timer) == 1
        assert recording_timer[0].started is True
        assert recording_timer[0].daemon is True

    def test_timer_uses_the_configured_hooks_check_delay(
        self, recording_timer, celery_worker_argv_process
    ):
        """Same setting the gunicorn hooks check reads, for the same reason.

        It has to outlast the adapter's own import path so a correct-but-slow
        wiring does not warn.
        """
        from baldur.settings.recovery_shutdown import get_recovery_shutdown_settings

        bootstrap_module._schedule_celery_deferral_check()

        assert (
            recording_timer[0].interval
            == get_recovery_shutdown_settings().hooks_check_delay_seconds
        )

    def test_observed_worker_init_signal_arms_nothing(self, recording_timer):
        """A healthy prefork parent: the flag is already set when ``init()`` runs."""
        with (
            patch(
                "baldur.core.process_utils.is_celery_worker_main",
                return_value=True,
            ),
            patch(
                "baldur.core.process_utils.is_celery_worker_serving",
                return_value=False,
            ),
        ):
            bootstrap_module._schedule_celery_deferral_check()

        assert recording_timer == []

    def test_serving_process_arms_nothing(self, recording_timer):
        """A pool child never deferred anything, so there is nothing to report."""
        with (
            patch(
                "baldur.core.process_utils.is_celery_worker_main",
                return_value=False,
            ),
            patch(
                "baldur.core.process_utils.is_celery_worker_serving",
                return_value=True,
            ),
        ):
            bootstrap_module._schedule_celery_deferral_check()

        assert recording_timer == []

    def test_non_celery_process_arms_nothing(self, recording_timer, monkeypatch):
        """Every non-celery deployment must pay nothing for this diagnostic."""
        monkeypatch.setattr(sys, "argv", ["python", "manage.py", "runserver"])

        bootstrap_module._schedule_celery_deferral_check()

        assert recording_timer == []

    def test_fired_timer_warns_when_no_worker_signal_arrived(
        self, recording_timer, celery_worker_argv_process
    ):
        """The deferral was never confirmed — the adapter was never wired."""
        with patch.object(bootstrap_module, "logger") as logger:
            bootstrap_module._schedule_celery_deferral_check()
            recording_timer[0].run_callback()

        warned = [call.args[0] for call in logger.warning.call_args_list if call.args]
        assert warned == ["baldur.celery_worker_init_not_observed"]

    def test_fired_timer_reports_whether_the_receivers_are_connected(
        self, recording_timer, celery_worker_argv_process
    ):
        """The operator reads the right remedy off the one line.

        Unconnected receivers are a wiring gap; connected ones mean the check
        simply ran before the app finished importing.
        """
        disconnect_celery_bootstrap_receivers()

        with patch.object(bootstrap_module, "logger") as logger:
            bootstrap_module._schedule_celery_deferral_check()
            recording_timer[0].run_callback()

        assert logger.warning.call_args.kwargs["receivers_connected"] is False

    def test_late_worker_signal_silences_the_fired_timer_callback(
        self, recording_timer
    ):
        """The callback re-checks: a signal that arrived meanwhile means no defect."""
        with (
            patch(
                "baldur.core.process_utils.is_celery_worker_main",
                return_value=False,
            ) as worker_main,
            patch(
                "baldur.core.process_utils.is_celery_worker_serving",
                return_value=False,
            ),
            patch(
                "baldur.core.process_utils.is_celery_worker_process",
                return_value=True,
            ),
            patch.object(bootstrap_module, "logger") as logger,
        ):
            bootstrap_module._schedule_celery_deferral_check()
            worker_main.return_value = True  # worker_init fired before the delay
            recording_timer[0].run_callback()

        logger.warning.assert_not_called()

    def test_rearming_cancels_the_previous_timer(
        self, recording_timer, celery_worker_argv_process
    ):
        """A second ``init()`` in one process must leave one live watchdog."""
        bootstrap_module._schedule_celery_deferral_check()
        bootstrap_module._schedule_celery_deferral_check()

        assert recording_timer[0].cancelled is True
        assert recording_timer[1].cancelled is False
        assert bootstrap_module._celery_deferral_timer is recording_timer[1]

    def test_state_reset_cancels_a_pending_timer_and_clears_the_warning_record(
        self, recording_timer, celery_worker_argv_process
    ):
        """A Timer armed by the previous ``init()`` outlives it.

        Left running it would report on a posture the next ``init()`` is about
        to decide again.
        """
        bootstrap_module._schedule_celery_deferral_check()
        bootstrap_module._celery_deferral_warning_fired = True

        bootstrap_module._reset_celery_deferral_state()

        assert recording_timer[0].cancelled is True
        assert bootstrap_module._celery_deferral_timer is None
        assert bootstrap_module._celery_deferral_warning_fired is False

    def test_reset_init_state_drops_the_watchdog_and_the_posture_record(
        self, recording_timer, celery_worker_argv_process
    ):
        """The reset's Step 0, reached through the entry point that owns it.

        Without this wiring a live Timer from the previous ``init()`` reports on
        a posture that no longer exists, in a process the next ``init()`` is
        about to re-decide.
        """
        bootstrap_module._schedule_celery_deferral_check()
        bootstrap_module._background_starters_deferred = True

        bootstrap_module.reset_init_state()

        assert recording_timer[0].cancelled is True
        assert bootstrap_module._celery_deferral_timer is None
        assert bootstrap_module._background_starters_deferred is None


class TestCeleryDeferralReconciliationBehavior:
    """``reconcile_celery_deferral_posture()`` — what a worker signal settles.

    Called by the ``worker_init`` receiver *before* it runs ``init()``, so what
    it reads is the state of any earlier initialization — the Django-fixup path,
    where Baldur initializes at app-module import, ahead of every Celery signal.
    """

    def test_pending_timer_is_cancelled(self, recording_timer):
        """This signal is exactly the confirmation the watchdog was waiting for."""
        timer = _RecordingTimer(1.0, lambda: None)
        bootstrap_module._celery_deferral_timer = timer

        bootstrap_module.reconcile_celery_deferral_posture(fork_lane=True)

        assert timer.cancelled is True
        assert bootstrap_module._celery_deferral_timer is None

    def test_corrective_info_follows_a_warning_that_already_fired(self):
        """A slow app import can outrun the delay; the log must not end on that WARNING."""
        bootstrap_module._celery_deferral_warning_fired = True

        with patch.object(bootstrap_module, "logger") as logger:
            bootstrap_module.reconcile_celery_deferral_posture(fork_lane=True)

        events = [call.args[0] for call in logger.info.call_args_list if call.args]
        assert "baldur.celery_deferral_reconciled" in events

    def test_no_corrective_info_when_no_warning_fired(self):
        """The common case reconciles silently — the watchdog said nothing."""
        bootstrap_module._celery_deferral_warning_fired = False

        with patch.object(bootstrap_module, "logger") as logger:
            bootstrap_module.reconcile_celery_deferral_posture(fork_lane=True)

        events = [call.args[0] for call in logger.info.call_args_list if call.args]
        assert "baldur.celery_deferral_reconciled" not in events

    def test_pre_fork_threads_posture_is_reported_on_a_fork_lane(self):
        """The one posture the watchdog cannot see.

        No deferral happened here, so nothing armed the timer — an ``init()``
        that started the starters in a process about to fork is reported at the
        signal or nowhere.
        """
        bootstrap_module._init_done = True
        bootstrap_module._background_starters_deferred = False

        with patch.object(bootstrap_module, "logger") as logger:
            bootstrap_module.reconcile_celery_deferral_posture(fork_lane=True)

        warned = [call.args[0] for call in logger.warning.call_args_list if call.args]
        assert warned == ["baldur.celery_background_workers_started_pre_fork"]

    def test_healthy_prefork_parent_is_silent(self):
        """The negative half: a designed deferral produces no WARNING.

        Children's serving marks live in their own environ copies, so a naive
        "starters were deferred" check would fire on every healthy prefork
        worker.
        """
        bootstrap_module._init_done = True
        bootstrap_module._background_starters_deferred = True

        with patch.object(bootstrap_module, "logger") as logger:
            bootstrap_module.reconcile_celery_deferral_posture(fork_lane=True)

        logger.warning.assert_not_called()

    def test_non_fork_lane_with_started_starters_is_silent(self):
        """Threads started in a process that never forks are exactly right."""
        bootstrap_module._init_done = True
        bootstrap_module._background_starters_deferred = False

        with patch.object(bootstrap_module, "logger") as logger:
            bootstrap_module.reconcile_celery_deferral_posture(fork_lane=False)

        logger.warning.assert_not_called()

    def test_uninitialized_process_is_silent(self):
        """Nothing ran yet, so there are no pre-fork threads to report."""
        bootstrap_module._init_done = False
        bootstrap_module._background_starters_deferred = None

        with patch.object(bootstrap_module, "logger") as logger:
            bootstrap_module.reconcile_celery_deferral_posture(fork_lane=True)

        logger.warning.assert_not_called()


class TestBackgroundStarterPostureBehavior:
    """``start_background_workers()`` records the posture it took.

    Each starter still asks the fork-source predicate for itself; this record is
    the answer they all got, kept so the Celery reconciliation can distinguish a
    process that deferred by design from one that started threads it is about to
    fork away from.
    """

    @pytest.fixture
    def recorded_starters(self, monkeypatch):
        """Replace the 14 production starters with one recording stub.

        The real tuple starts daemon threads and touches settings for a dozen
        subsystems; the record under test is written once per pass, before any
        starter runs, so a single stub exercises it faithfully.
        """
        calls: list[str] = []
        monkeypatch.setattr(
            bootstrap_module,
            "_BACKGROUND_WORKER_STARTERS",
            (lambda: calls.append("ran"),),
        )
        return calls

    def test_fork_source_records_a_deferred_pass(self, recorded_starters):
        """A prefork worker main / gunicorn master defers, and says so."""
        with patch(
            "baldur.core.process_utils.is_fork_source_process",
            return_value=True,
        ):
            bootstrap_module.start_background_workers()

        assert bootstrap_module._background_starters_deferred is True

    def test_serving_process_records_a_started_pass(self, recorded_starters):
        """A pool child / plain process starts them, and says so."""
        with patch(
            "baldur.core.process_utils.is_fork_source_process",
            return_value=False,
        ):
            bootstrap_module.start_background_workers()

        assert bootstrap_module._background_starters_deferred is False

    def test_every_starter_still_runs_regardless_of_the_record(self, recorded_starters):
        """The record is a note, not a gate — each starter owns its own skip."""
        with patch(
            "baldur.core.process_utils.is_fork_source_process",
            return_value=True,
        ):
            bootstrap_module.start_background_workers()

        assert recorded_starters == ["ran"]

    def test_predicate_failure_records_an_unknown_posture(self, recorded_starters):
        """An undecidable posture must not read as "started" — the reconciliation
        reports the pre-fork-threads WARNING only on an explicit False."""
        with patch(
            "baldur.core.process_utils.is_fork_source_process",
            side_effect=RuntimeError("predicate blew up"),
        ):
            bootstrap_module.start_background_workers()

        assert bootstrap_module._background_starters_deferred is None
        assert recorded_starters == ["ran"]
