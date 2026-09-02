"""Unit tests for ``baldur.adapters.gunicorn.hooks``.

Covers the contract documented in the module docstring:

- ``post_worker_init`` sets ``GUNICORN_WORKER=1`` and initializes the
  shutdown coordinator with a ``RequestTracker``.
- ``post_worker_init`` re-starts the framework-agnostic OSS background
  daemon workers via ``baldur.bootstrap.start_background_workers()`` for
  **all** adapters (even when Django is absent), then re-starts the
  Django-only extra threads when the Django adapter is importable, and
  silently no-ops the Django branch when it is not.
- ``post_worker_init`` drops inherited external-connection state, but only
  when the application was preloaded.
- ``worker_int`` calls ``coordinator.initiate_shutdown()``.
- ``worker_exit`` runs only in the worker it was handed, waits for drain
  (settings-driven, 30 s by default), resets the Django background-thread
  guards when available, flushes the audit system unconditionally, and
  marks its own completion.
- The package re-exports the three hooks under stable names so users
  can ``from baldur.adapters.gunicorn import post_worker_init,
  worker_int, worker_exit``.

Every ``worker_exit`` call below is **positional**. gunicorn invokes the
hook as ``cfg.worker_exit(self, worker)`` and its own config validator
checks arity only, never parameter names — so a keyword-calling suite
binds correctly whichever way round the parameters are declared and can
never observe an argument-order defect.
"""

from __future__ import annotations

import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from structlog.testing import capture_logs


def _exiting_worker() -> MagicMock:
    """A gunicorn ``Worker`` stand-in for the process running the test.

    ``worker_exit`` runs its pipeline only when the worker it is handed is
    this process — gunicorn invokes the same hook in the master for a worker
    that had already exited when it was signalled.
    """
    worker = MagicMock()
    worker.pid = os.getpid()
    return worker


def _arbiter() -> SimpleNamespace:
    """Stand-in for ``worker_exit``'s first argument.

    The hook never reads it; what the tests pin is that it is passed
    first, which is the order gunicorn's arbiter uses.
    """
    return SimpleNamespace()


def _foreign_worker() -> SimpleNamespace:
    """A ``Worker`` describing some *other* process.

    This is what the master is handed when the arbiter reaches the hook for
    a worker that had already exited when it was signalled. ``-1`` is never
    a live pid, so the guard's comparison cannot pass by coincidence.
    """
    return SimpleNamespace(pid=-1)


def _worker_with_preload(preload_app: bool | None) -> SimpleNamespace:
    """An exiting-process worker whose ``cfg`` reports the preload setting.

    ``None`` produces a ``cfg`` with no ``preload_app`` attribute at all —
    the unexpected-gunicorn-shape branch, which must degrade to running the
    resets rather than silently skipping them.
    """
    cfg = (
        SimpleNamespace()
        if preload_app is None
        else SimpleNamespace(preload_app=preload_app)
    )
    return SimpleNamespace(pid=os.getpid(), cfg=cfg)


def _coordinator_after_a_real_drain(pending: int):
    """Install a singleton coordinator that has already finished one drain.

    ``pending`` requests are started and never completed, so a non-zero count
    makes the drain run out its timeout and force-abort them. Both drains end
    in TERMINATED — which is precisely why ``worker_exit`` cannot tell them
    apart from the drain predicate alone, and why the terminal marker has to
    carry the abandoned count itself.
    """
    from baldur.core.shutdown_coordinator import (
        GracefulShutdownCoordinator,
        RequestTracker,
        configure_shutdown_coordinator,
    )

    tracker = RequestTracker()
    for i in range(pending):
        tracker.start_request(f"never_completes_{i}")

    coordinator = GracefulShutdownCoordinator(
        request_tracker=tracker,
        drain_timeout=0.2,
        check_interval=0.01,
    )
    coordinator.initiate_shutdown()
    assert coordinator.wait_for_shutdown(timeout=5.0)
    configure_shutdown_coordinator(coordinator)
    return coordinator


@pytest.fixture(autouse=True)
def _reset_dlq_outbox_module_state():
    """Undo the outbox teardown the real ``worker_exit`` hook performs.

    The hook's teardown is process-global: it sets the producer-coercion flag
    and caches its terminal result so repeat callers are no-ops. Left behind,
    every later test in this worker dispatches DLQ captures synchronously and
    any teardown they run returns this file's cached counts.
    """
    from baldur.services.dlq_outbox import outbox as outbox_module

    def _clear() -> None:
        if outbox_module._outbox is not None:
            try:
                outbox_module._outbox.stop(timeout=1.0)
            except Exception:
                pass
            outbox_module._outbox = None
        outbox_module._outbox_origin_pid = None
        outbox_module._worker_dead = False
        outbox_module._worker_dead_coercions = 0
        outbox_module._shutdown_result = None
        outbox_module._teardown_started = False

    _clear()
    yield
    _clear()


@pytest.fixture(autouse=True)
def _isolated_gunicorn_env(monkeypatch):
    """Ensure ``GUNICORN_WORKER`` does not leak across tests."""
    monkeypatch.delenv("GUNICORN_WORKER", raising=False)
    yield
    monkeypatch.delenv("GUNICORN_WORKER", raising=False)


@pytest.fixture(autouse=True)
def _reset_shutdown_coordinator():
    """Each test starts with a fresh coordinator singleton."""
    from baldur.core.shutdown_coordinator import reset_shutdown_coordinator

    reset_shutdown_coordinator()
    yield
    reset_shutdown_coordinator()


@pytest.fixture(autouse=True)
def _isolated_sigterm_handler():
    """``post_worker_init`` installs a chained SIGTERM handler on the
    process. Without restoring the snapshot, each test leaves another
    layer of chain on top, and subsequent tests observe N invocations
    of ``_initiate_shutdown_safely`` instead of one."""
    import signal

    original = signal.getsignal(signal.SIGTERM)
    yield
    signal.signal(signal.SIGTERM, original)


@pytest.fixture(autouse=True)
def _mock_background_worker_starts():
    """Mock the framework-agnostic + Django background-worker starts by default.

    ``post_worker_init`` calls ``baldur.bootstrap.start_background_workers()``
    (the OSS-5 init()-started daemon workers) for all adapters, then
    ``BaldurConfig.start_background_threads()`` for the Django-only extras. Both
    spawn daemon threads (domain-gauge collector, precomputed-cache /
    system-metrics refresh loops, SelfhealerWatchdog, correlation-engine loop)
    that linger until module teardown joins them (5s × N → 10s+ teardown). Hook
    tests only need to verify wiring, not real thread lifecycle, so both are
    mocked by default.

    Tests that exercise the wiring contract directly install their own
    ``patch(...)`` context manager — that context replaces the autouse
    MagicMock for the scope of the test and restores it on exit, so
    ``assert_called_once()`` on the local mock still works."""
    with (
        patch("baldur.bootstrap.start_background_workers"),
        patch("baldur.adapters.django.apps.BaldurConfig.start_background_threads"),
        patch("baldur.adapters.django.apps.BaldurConfig.stop_background_threads"),
    ):
        yield


class TestPackageReExports:
    """The package ``__init__`` must expose the three hook callables."""

    def test_package_exports_three_hooks(self):
        from baldur.adapters import gunicorn as pkg

        assert callable(pkg.post_worker_init)
        assert callable(pkg.worker_int)
        assert callable(pkg.worker_exit)

    def test_all_lists_three_hooks(self):
        from baldur.adapters import gunicorn as pkg

        assert set(pkg.__all__) == {"post_worker_init", "worker_int", "worker_exit"}


class TestPostWorkerInit:
    """``post_worker_init`` contract."""

    def test_sets_gunicorn_worker_env_var(self):
        from baldur.adapters.gunicorn.hooks import post_worker_init

        post_worker_init(worker=MagicMock())

        assert os.environ["GUNICORN_WORKER"] == "1"

    def test_initializes_coordinator_with_request_tracker(self):
        from baldur.adapters.gunicorn.hooks import post_worker_init
        from baldur.core.shutdown_coordinator import get_shutdown_coordinator

        post_worker_init(worker=MagicMock())

        coordinator = get_shutdown_coordinator()
        assert coordinator._tracker is not None

    def test_calls_start_background_workers_for_all_adapters(self):
        """The framework-agnostic OSS-5 restart runs on every post_worker_init."""
        from baldur.adapters.gunicorn.hooks import post_worker_init

        with patch("baldur.bootstrap.start_background_workers") as m_start:
            post_worker_init(worker=MagicMock())

        m_start.assert_called_once()

    def test_calls_django_start_background_threads_when_available(self):
        from baldur.adapters.gunicorn.hooks import post_worker_init

        with patch(
            "baldur.adapters.django.apps.BaldurConfig.start_background_threads"
        ) as m_start:
            post_worker_init(worker=MagicMock())

        m_start.assert_called_once()

    def test_installs_chained_sigterm_handler(self):
        """post_worker_init must register a chained SIGTERM handler so
        gunicorn's master-forwarded SIGTERM triggers baldur's drain.
        gunicorn's worker_int callback only fires for SIGINT/SIGQUIT —
        without chaining SIGTERM in post_worker_init, the registered
        shutdown handlers would never run on graceful shutdown."""
        import signal

        from baldur.adapters.gunicorn.hooks import post_worker_init

        original_handler = signal.getsignal(signal.SIGTERM)
        try:
            with patch(
                "baldur.adapters.gunicorn.hooks._initiate_shutdown_safely"
            ) as m_initiate:
                post_worker_init(worker=MagicMock())

                installed_handler = signal.getsignal(signal.SIGTERM)
                assert installed_handler is not original_handler, (
                    "post_worker_init did not replace SIGTERM handler"
                )
                # Invoke the chained handler — it must call
                # initiate_shutdown safely.
                installed_handler(signal.SIGTERM, None)
                m_initiate.assert_called_once_with()
        finally:
            signal.signal(signal.SIGTERM, original_handler)

    def test_chained_sigterm_calls_original_handler(self):
        """The chained handler must delegate to whatever SIGTERM
        handler was registered before post_worker_init ran (gunicorn's
        ``handle_exit`` in production). Otherwise gunicorn's drain
        machinery never sees the signal."""
        import signal

        from baldur.adapters.gunicorn.hooks import post_worker_init

        captured = {"called_with": None}

        def _capture(signum, frame):
            captured["called_with"] = (signum, frame)

        original_handler = signal.getsignal(signal.SIGTERM)
        try:
            signal.signal(signal.SIGTERM, _capture)

            with patch("baldur.adapters.gunicorn.hooks._initiate_shutdown_safely"):
                post_worker_init(worker=MagicMock())

            installed_handler = signal.getsignal(signal.SIGTERM)
            installed_handler(signal.SIGTERM, "frame_sentinel")

            assert captured["called_with"] == (signal.SIGTERM, "frame_sentinel"), (
                "chained handler did not delegate to the original"
            )
        finally:
            signal.signal(signal.SIGTERM, original_handler)

    def test_framework_agnostic_start_runs_when_django_adapter_missing(
        self, monkeypatch
    ):
        """ImportError on Django path must not fail the hook — and the
        framework-agnostic ``start_background_workers()`` still runs (SC1).

        The Django branch is the *only* thing guarded by ``except ImportError``;
        ``start_background_workers()`` runs before it, so a missing Django
        adapter must not suppress the OSS-5 per-worker restart.
        """
        from baldur.adapters.gunicorn import hooks

        # Simulate Django adapter missing by removing the module from
        # sys.modules and blocking re-import via a meta-path finder that
        # raises ImportError for that exact dotted name.
        monkeypatch.delitem(sys.modules, "baldur.adapters.django.apps", raising=False)

        class _BlockDjangoApps:
            def find_module(self, name, path=None):
                if name == "baldur.adapters.django.apps":
                    return self
                return None

            def load_module(self, name):
                raise ImportError(f"blocked: {name}")

            def find_spec(self, name, path, target=None):
                if name == "baldur.adapters.django.apps":
                    raise ImportError(f"blocked: {name}")
                return None

        blocker = _BlockDjangoApps()
        sys.meta_path.insert(0, blocker)
        try:
            with patch("baldur.bootstrap.start_background_workers") as m_start:
                hooks.post_worker_init(worker=MagicMock())
        finally:
            sys.meta_path.remove(blocker)

        # Framework-agnostic restart fired despite the absent Django adapter,
        # and the env var is still set + coordinator still initialized.
        m_start.assert_called_once()
        assert os.environ["GUNICORN_WORKER"] == "1"

    def test_no_post_fork_hook_is_exported_by_the_package(self):
        """The post-fork resets ride the existing ``post_worker_init`` surface.
        Exporting a fourth hook name would make every user's gunicorn config
        stale, and omitting it would be silent.
        """
        from baldur.adapters import gunicorn as pkg

        assert not hasattr(pkg, "post_fork")
        assert "post_fork" not in pkg.__all__


class TestWorkerInt:
    """``worker_int`` contract."""

    def test_calls_initiate_shutdown(self):
        from baldur.adapters.gunicorn.hooks import worker_int
        from baldur.core.shutdown_coordinator import get_shutdown_coordinator

        coordinator = get_shutdown_coordinator()
        with patch.object(coordinator, "initiate_shutdown") as m_initiate:
            worker_int(worker=MagicMock())

        m_initiate.assert_called_once_with()


class TestWorkerExitProcessGuardBehavior:
    """``worker_exit`` acts only in the worker it was handed.

    gunicorn's arbiter invokes the same hook **in the master** for a worker
    that had already exited when it was signalled (``kill_worker`` →
    ``ESRCH``), a routine race during scale-down and timeout replacement.
    Running the pipeline there would tear down the master's audit system and
    set a process-global once-flag that every later-forked worker inherits.
    """

    def test_no_pipeline_step_runs_when_the_worker_is_another_process(self):
        # Given: the worker being reported is not this process
        from baldur.adapters.gunicorn.hooks import worker_exit
        from baldur.core.shutdown_coordinator import get_shutdown_coordinator

        coordinator = get_shutdown_coordinator()

        # When
        with (
            patch.object(coordinator, "wait_for_shutdown") as m_wait,
            patch(
                "baldur.adapters.django.apps.BaldurConfig.stop_background_threads"
            ) as m_stop,
            patch(
                "baldur.audit.async_audit_lifecycle.graceful_shutdown_audit_system"
            ) as m_flush,
            capture_logs() as cap_logs,
        ):
            worker_exit(_arbiter(), _foreign_worker())

        # Then: every step of the pipeline is skipped, including the marker
        m_wait.assert_not_called()
        m_stop.assert_not_called()
        m_flush.assert_not_called()
        assert [
            e["event"]
            for e in cap_logs
            if e.get("event") == "shutdown.worker_exit_completed"
        ] == []

    @pytest.mark.parametrize(
        ("gunicorn_worker_env", "expected_level", "expected_reason"),
        [
            (None, "debug", "not_the_exiting_worker"),
            ("1", "warning", "pid_mismatch_inside_worker"),
        ],
        ids=["master_side_race", "worker_that_lost_its_own_pid"],
    )
    def test_skip_log_level_separates_the_routine_race_from_a_broken_contract(
        self, monkeypatch, gunicorn_worker_env, expected_level, expected_reason
    ):
        """A flat DEBUG would make this guard fail-silent.

        ``post_worker_init`` sets ``GUNICORN_WORKER`` in the worker's own
        environ and never in the master. So an unset value means the expected
        master-side race, while a set one means a process that ran
        ``post_worker_init`` no longer recognizes its own pid — under which
        every worker skips its whole exit pipeline with nothing turning red.
        The WARNING arm is unreachable under a contract-honouring gunicorn;
        this test is the only place it executes.
        """
        from baldur.adapters.gunicorn.hooks import worker_exit

        if gunicorn_worker_env is None:
            monkeypatch.delenv("GUNICORN_WORKER", raising=False)
        else:
            monkeypatch.setenv("GUNICORN_WORKER", gunicorn_worker_env)

        with capture_logs() as cap_logs:
            worker_exit(_arbiter(), _foreign_worker())

        matching = [
            e for e in cap_logs if e.get("event") == "shutdown.worker_exit_skipped"
        ]
        assert len(matching) == 1
        assert matching[0]["log_level"] == expected_level
        assert matching[0]["reason"] == expected_reason
        assert matching[0]["process_id"] == os.getpid()


class TestWorkerExitDrainTimeoutBehavior:
    """The drain wait is settings-driven, and survives a settings failure."""

    def test_waits_for_shutdown_with_the_configured_drain_timeout(self):
        """The wait reads ``default_drain_timeout_seconds`` — 30.0 at
        defaults, which is what an operator tuning that field expects to
        change."""
        from baldur.adapters.gunicorn.hooks import worker_exit
        from baldur.core.shutdown_coordinator import get_shutdown_coordinator

        coordinator = get_shutdown_coordinator()
        with patch.object(coordinator, "wait_for_shutdown") as m_wait:
            worker_exit(_arbiter(), _exiting_worker())

        m_wait.assert_called_once_with(timeout=30.0)

    def test_a_tuned_drain_timeout_reaches_the_wait(self, monkeypatch):
        """The whole point of reading settings here is that the operator's
        value arrives — a hook that read the field and then waited 30.0
        anyway would pass a "reads settings" grep and change nothing."""
        from baldur.adapters.gunicorn.hooks import worker_exit
        from baldur.core.shutdown_coordinator import get_shutdown_coordinator
        from baldur.settings.recovery_shutdown import reset_recovery_shutdown_settings

        monkeypatch.setenv(
            "BALDUR_RECOVERY_SHUTDOWN_DEFAULT_DRAIN_TIMEOUT_SECONDS", "7.5"
        )
        reset_recovery_shutdown_settings()
        try:
            coordinator = get_shutdown_coordinator()
            with patch.object(coordinator, "wait_for_shutdown") as m_wait:
                worker_exit(_arbiter(), _exiting_worker())
        finally:
            monkeypatch.delenv(
                "BALDUR_RECOVERY_SHUTDOWN_DEFAULT_DRAIN_TIMEOUT_SECONDS", raising=False
            )
            reset_recovery_shutdown_settings()

        m_wait.assert_called_once_with(timeout=7.5)

    def test_falls_back_to_the_module_default_when_the_settings_read_raises(self):
        """A degenerate config must not skip the exit pipeline: the read is
        wrapped, the fallback mirrors the Field default, and the failure is
        reported rather than swallowed."""
        from baldur.adapters.gunicorn.hooks import (
            _DEFAULT_DRAIN_WAIT_SECONDS,
            worker_exit,
        )
        from baldur.core.shutdown_coordinator import get_shutdown_coordinator

        coordinator = get_shutdown_coordinator()
        with (
            patch(
                "baldur.settings.recovery_shutdown.get_recovery_shutdown_settings",
                side_effect=RuntimeError("degenerate config"),
            ),
            patch.object(coordinator, "wait_for_shutdown") as m_wait,
            patch(
                "baldur.audit.async_audit_lifecycle.graceful_shutdown_audit_system"
            ) as m_flush,
            capture_logs() as cap_logs,
        ):
            worker_exit(_arbiter(), _exiting_worker())

        m_wait.assert_called_once_with(timeout=_DEFAULT_DRAIN_WAIT_SECONDS)
        m_flush.assert_called_once()
        failures = [
            e
            for e in cap_logs
            if e.get("event") == "shutdown.drain_timeout_read_failed"
        ]
        assert len(failures) == 1
        assert failures[0]["log_level"] == "warning"

    def test_a_degenerate_config_does_not_cost_the_worker_its_audit_flush(self):
        """The fallback is worthless if the *next* line re-reads the same
        settings and raises.

        A coordinator nobody built yet is constructed lazily right here, and
        its constructor reads ``recovery_shutdown`` too. So on a config that
        fails validation the wait timeout falls back correctly and then the
        resolution one line later raises — taking the Django reset, the audit
        flush and the completion marker with it, which is exactly the outcome
        the fallback exists to prevent. The coordinator singleton is
        deliberately not pre-created here: pre-creating it is what hid this.
        """
        from baldur.adapters.gunicorn.hooks import worker_exit

        with (
            patch(
                "baldur.settings.recovery_shutdown.get_recovery_shutdown_settings",
                side_effect=RuntimeError("degenerate config"),
            ),
            patch(
                "baldur.audit.async_audit_lifecycle.graceful_shutdown_audit_system"
            ) as m_flush,
            capture_logs() as cap_logs,
        ):
            worker_exit(_arbiter(), _exiting_worker())

        m_flush.assert_called_once()
        events = [e["event"] for e in cap_logs]
        assert "shutdown.drain_wait_failed" in events
        assert "shutdown.worker_exit_completed" in events

    def test_a_failing_coordinator_resolution_is_isolated_like_every_other_step(
        self,
    ):
        from baldur.adapters.gunicorn.hooks import worker_exit

        with (
            patch(
                "baldur.core.shutdown_coordinator.get_shutdown_coordinator",
                side_effect=RuntimeError("coordinator unavailable"),
            ),
            patch(
                "baldur.audit.async_audit_lifecycle.graceful_shutdown_audit_system"
            ) as m_flush,
            capture_logs() as cap_logs,
        ):
            worker_exit(_arbiter(), _exiting_worker())

        m_flush.assert_called_once()
        failures = [
            e for e in cap_logs if e.get("event") == "shutdown.drain_wait_failed"
        ]
        assert len(failures) == 1
        assert failures[0]["log_level"] == "warning"
        assert failures[0]["worker_id"] == os.getpid()


class TestWorkerExitPipelineBehavior:
    """``worker_exit``'s step sequence, isolation and terminal marker."""

    def test_calls_django_stop_background_threads_when_available(self):
        from baldur.adapters.gunicorn.hooks import worker_exit

        with patch(
            "baldur.adapters.django.apps.BaldurConfig.stop_background_threads"
        ) as m_stop:
            worker_exit(_arbiter(), _exiting_worker())

        m_stop.assert_called_once()

    def test_logs_worker_drained_when_drain_completes(self):
        """A clean drain (wait_for_shutdown -> True) emits a reliable
        INFO ``shutdown.worker_drained`` from the worker's main process —
        the canonical externally-observable shutdown-complete signal."""
        from baldur.adapters.gunicorn.hooks import worker_exit
        from baldur.core.shutdown_coordinator import get_shutdown_coordinator

        coordinator = get_shutdown_coordinator()
        with (
            patch.object(coordinator, "wait_for_shutdown", return_value=True),
            capture_logs() as cap_logs,
        ):
            worker_exit(_arbiter(), _exiting_worker())

        matching = [e for e in cap_logs if e.get("event") == "shutdown.worker_drained"]
        assert len(matching) == 1
        assert matching[0]["log_level"] == "info"

    @pytest.mark.parametrize(
        ("pending_at_drain", "expected_aborted"),
        [(0, 0), (2, 2)],
        ids=["clean_drain", "force_aborted_drain"],
    )
    def test_drained_marker_reports_the_requests_the_drain_abandoned(
        self, pending_at_drain, expected_aborted
    ):
        """A drain the coordinator cut short at its timeout reaches TERMINATED
        too, so it satisfies the same ``wait_for_shutdown()`` predicate and
        emits the same event name as a drain that finished. Without the count
        of what it abandoned the two lines are identical, and an operator
        cannot answer "did anything get dropped" from the terminal marker."""
        from baldur.adapters.gunicorn.hooks import worker_exit
        from baldur.core.shutdown_coordinator import ShutdownPhase

        # Given — one real drain of each kind, both ended in the same phase
        coordinator = _coordinator_after_a_real_drain(pending_at_drain)
        assert coordinator.phase is ShutdownPhase.TERMINATED

        # When
        with capture_logs() as cap_logs:
            worker_exit(_arbiter(), _exiting_worker())

        # Then
        matching = [e for e in cap_logs if e.get("event") == "shutdown.worker_drained"]
        assert len(matching) == 1
        assert matching[0]["aborted"] == expected_aborted

    def test_a_handler_forced_drain_still_reports_zero_abandoned(self):
        """The abandoned count is the *request tracker's*, so it answers "was
        this drain cut short" only when the force had requests to abandon. A
        drain the coordinator cut short because a registered handler never
        reported drained — with nothing in flight — reports ``aborted=0``, the
        same value a clean drain reports.

        Pinned, not fixed: which marker a forced drain should carry is the
        category question this change deliberately leaves open. What the pair
        of assertions below fixes is the *reading*: the discriminator in this
        case is the coordinator's own ``shutdown.drain_timeout_reached``, and
        it is a WARNING, so it survives the default level floor that hides the
        terminal INFO marker entirely."""
        from baldur.adapters.gunicorn.hooks import worker_exit
        from baldur.core.shutdown_coordinator import (
            GracefulShutdownCoordinator,
            RequestTracker,
            ShutdownHandler,
            ShutdownPhase,
            configure_shutdown_coordinator,
        )

        class _NeverDrains(ShutdownHandler):
            """A handler whose drain never completes, which is what pushes the
            coordinator onto its forced path with an empty tracker."""

            def on_shutdown_start(self) -> None: ...

            def is_drain_complete(self) -> bool:
                return False

            def on_drain_complete(self) -> None: ...

            def on_force_shutdown(self, pending_requests) -> None: ...

        # Given — a drain forced by the handler, not by pending requests
        coordinator = GracefulShutdownCoordinator(
            request_tracker=RequestTracker(),
            drain_timeout=0.2,
            check_interval=0.01,
        )
        coordinator.register_handler(_NeverDrains())

        # When — the drain and the hook share one capture, so the WARNING the
        # drain thread emits is observable next to the marker the hook emits
        with capture_logs() as cap_logs:
            coordinator.initiate_shutdown()
            assert coordinator.wait_for_shutdown(timeout=5.0)
            configure_shutdown_coordinator(coordinator)
            worker_exit(_arbiter(), _exiting_worker())

        # Then
        assert coordinator.phase is ShutdownPhase.TERMINATED

        drained = [e for e in cap_logs if e.get("event") == "shutdown.worker_drained"]
        assert len(drained) == 1
        assert drained[0]["aborted"] == 0

        forced = [
            e for e in cap_logs if e.get("event") == "shutdown.drain_timeout_reached"
        ]
        assert len(forced) == 1
        assert forced[0]["log_level"] == "warning"

    def test_a_failing_stats_read_replaces_the_marker_with_its_warning(self):
        """The abandoned-count read sits inside the drain try-block, so a
        coordinator whose stats read raises loses the terminal INFO marker
        rather than emitting it without the field.

        Pinned rather than guarded: the line that replaces it is
        ``shutdown.drain_wait_failed`` at WARNING, which survives the default
        level floor the INFO marker does not, so the failure is loud rather
        than silent — and the completion marker still reports that the exit
        pipeline ran to its end."""
        from baldur.adapters.gunicorn.hooks import worker_exit
        from baldur.core.shutdown_coordinator import get_shutdown_coordinator

        coordinator = get_shutdown_coordinator()
        with (
            patch.object(coordinator, "wait_for_shutdown", return_value=True),
            patch.object(
                coordinator,
                "get_stats",
                side_effect=RuntimeError("stats read blew up"),
            ),
            capture_logs() as cap_logs,
        ):
            worker_exit(_arbiter(), _exiting_worker())

        shutdown_events = [
            e["event"]
            for e in cap_logs
            if str(e.get("event", "")).startswith("shutdown.")
        ]
        assert "shutdown.worker_drained" not in shutdown_events
        assert "shutdown.worker_exit_completed" in shutdown_events

        failures = [
            e for e in cap_logs if e.get("event") == "shutdown.drain_wait_failed"
        ]
        assert len(failures) == 1
        assert failures[0]["log_level"] == "warning"

    def test_logs_drain_incomplete_when_initiated_but_not_terminated(self):
        """Shutdown initiated but drain did not reach TERMINATED within the
        wait timeout -> WARNING ``shutdown.worker_drain_incomplete`` with the
        observed phase."""
        from baldur.adapters.gunicorn.hooks import worker_exit
        from baldur.core.shutdown_coordinator import (
            ShutdownPhase,
            get_shutdown_coordinator,
        )

        coordinator = get_shutdown_coordinator()
        coordinator._phase = ShutdownPhase.DRAINING
        with (
            patch.object(coordinator, "wait_for_shutdown", return_value=False),
            capture_logs() as cap_logs,
        ):
            worker_exit(_arbiter(), _exiting_worker())

        matching = [
            e for e in cap_logs if e.get("event") == "shutdown.worker_drain_incomplete"
        ]
        assert len(matching) == 1
        assert matching[0]["log_level"] == "warning"
        assert matching[0]["phase"] == "draining"

    def test_no_drain_log_when_drain_not_initiated(self):
        """A normal worker exit with no shutdown initiated (phase RUNNING)
        must not report on a drain that never started — otherwise routine
        worker recycles / reloads would warn spuriously. The completion
        marker is the one line this path does emit: on a recycle it is the
        only evidence the exit pipeline ran at all."""
        from baldur.adapters.gunicorn.hooks import worker_exit
        from baldur.core.shutdown_coordinator import get_shutdown_coordinator

        coordinator = get_shutdown_coordinator()  # fresh ⇒ phase RUNNING
        with (
            patch.object(coordinator, "wait_for_shutdown", return_value=False),
            capture_logs() as cap_logs,
        ):
            worker_exit(_arbiter(), _exiting_worker())

        shutdown_events = [
            e["event"]
            for e in cap_logs
            if str(e.get("event", "")).startswith("shutdown.")
        ]
        assert shutdown_events == ["shutdown.worker_exit_completed"]

    def test_audit_flush_still_runs_when_the_django_reset_raises(self):
        """The audit flush is the one guarantee only this hook can offer on a
        recycle exit, so a Django-side failure must not be allowed to skip
        it — which is why the steps are isolated rather than sequential."""
        from baldur.adapters.gunicorn.hooks import worker_exit

        with (
            patch(
                "baldur.adapters.django.apps.BaldurConfig.stop_background_threads",
                side_effect=RuntimeError("django teardown blew up"),
            ),
            patch(
                "baldur.audit.async_audit_lifecycle.graceful_shutdown_audit_system"
            ) as m_flush,
            capture_logs() as cap_logs,
        ):
            worker_exit(_arbiter(), _exiting_worker())

        m_flush.assert_called_once_with()
        events = [e["event"] for e in cap_logs]
        assert "shutdown.django_thread_guards_reset_failed" in events
        assert "shutdown.worker_exit_completed" in events

    @pytest.mark.parametrize(
        "failing_steps",
        [
            ("audit",),
            ("django", "audit"),
        ],
        ids=["audit_flush_raises", "both_steps_raise"],
    )
    def test_completion_marker_is_emitted_even_when_steps_raise(self, failing_steps):
        """``shutdown.worker_exit_completed`` marks that the pipeline reached
        its end, not that every step succeeded. Suppressing it on a step
        failure would make it indistinguishable from the case it exists to
        detect: gunicorn killing the worker inside the hook."""
        from baldur.adapters.gunicorn.hooks import worker_exit

        django_effect = (
            RuntimeError("django teardown blew up")
            if "django" in failing_steps
            else None
        )
        with (
            patch(
                "baldur.adapters.django.apps.BaldurConfig.stop_background_threads",
                side_effect=django_effect,
            ),
            patch(
                "baldur.audit.async_audit_lifecycle.graceful_shutdown_audit_system",
                side_effect=RuntimeError("flush blew up"),
            ),
            capture_logs() as cap_logs,
        ):
            worker_exit(_arbiter(), _exiting_worker())

        events = [e["event"] for e in cap_logs]
        assert "shutdown.audit_flush_failed" in events
        assert "shutdown.worker_exit_completed" in events

    @pytest.mark.parametrize(
        ("drained", "phase_value", "expected_event"),
        [
            (True, "terminated", "shutdown.worker_drained"),
            (False, "draining", "shutdown.worker_drain_incomplete"),
        ],
        ids=["drained", "drain_incomplete"],
    )
    def test_terminal_drain_logs_identify_the_worker(
        self, drained, phase_value, expected_event
    ):
        """Without ``worker_id`` these lines are byte-identical across every
        worker of the pool — structlog's processor chain adds no pid — so an
        operator cannot tell which worker drained and which did not."""
        from baldur.adapters.gunicorn.hooks import worker_exit
        from baldur.core.shutdown_coordinator import (
            ShutdownPhase,
            get_shutdown_coordinator,
        )

        coordinator = get_shutdown_coordinator()
        coordinator._phase = ShutdownPhase(phase_value)
        with (
            patch.object(coordinator, "wait_for_shutdown", return_value=drained),
            capture_logs() as cap_logs,
        ):
            worker_exit(_arbiter(), _exiting_worker())

        matching = [e for e in cap_logs if e.get("event") == expected_event]
        assert len(matching) == 1
        assert matching[0]["worker_id"] == os.getpid()

    def test_completion_marker_identifies_the_worker(self):
        from baldur.adapters.gunicorn.hooks import worker_exit
        from baldur.core.shutdown_coordinator import get_shutdown_coordinator

        coordinator = get_shutdown_coordinator()
        with (
            patch.object(coordinator, "wait_for_shutdown", return_value=False),
            capture_logs() as cap_logs,
        ):
            worker_exit(_arbiter(), _exiting_worker())

        matching = [
            e for e in cap_logs if e.get("event") == "shutdown.worker_exit_completed"
        ]
        assert len(matching) == 1
        assert matching[0]["log_level"] == "info"
        assert matching[0]["worker_id"] == os.getpid()


class TestPostWorkerInitForkResetsBehavior:
    """The inherited-resource reset carried onto this surface.

    Exactly one member survived the consolidation — the event-producer
    reset, which is a no-op on a stock install. It rides ``post_worker_init``
    rather than a fourth hook name whose omission would be silent, and it is
    gated on preload because that is the only branch where "drop what the
    master left me" describes anything real: without ``--preload`` gunicorn
    runs ``load_wsgi()`` in the child, so ``baldur.init()`` built this
    process's own state moments earlier.
    """

    @pytest.mark.parametrize(
        ("preload_app", "expect_reset"),
        [
            (True, True),
            (False, False),
            (None, True),
        ],
        ids=["preloaded", "not_preloaded", "preload_attribute_absent"],
    )
    def test_reset_runs_only_when_the_app_was_preloaded(
        self, preload_app, expect_reset
    ):
        from baldur.adapters.gunicorn.hooks import post_worker_init

        with patch("baldur.adapters.gunicorn.hooks._reset_kafka_after_fork") as m_reset:
            post_worker_init(_worker_with_preload(preload_app))

        assert m_reset.called is expect_reset

    def test_reset_failure_does_not_skip_the_coordinator_wiring(self):
        """The reset is isolated: it runs before the coordinator init and the
        background-worker restart, so an unhandled failure there would take
        the whole hook — and with it SIGTERM's route to the drain — down."""
        from baldur.adapters.gunicorn.hooks import post_worker_init
        from baldur.core.shutdown_coordinator import get_shutdown_coordinator

        with (
            patch(
                "baldur.adapters.gunicorn.hooks._reset_kafka_after_fork",
                side_effect=RuntimeError("producer reset blew up"),
            ),
            patch("baldur.bootstrap.start_background_workers") as m_start,
            capture_logs() as cap_logs,
        ):
            post_worker_init(_worker_with_preload(True))

        m_start.assert_called_once()
        assert get_shutdown_coordinator()._tracker is not None
        assert "worker.postfork_reset_failed" in [e["event"] for e in cap_logs]

    def test_does_not_invalidate_the_cache_registry_slot(self):
        """The legacy surface invalidated the resolved cache singleton after
        fork. It was dropped, not moved: ``init()`` hands the resolved adapter
        by reference to the idempotency gate and the resilient storage
        backend, so popping the registry slot cannot reach either — while the
        next lazy resolver on a serving path builds a second adapter and a
        second connection pool per preloaded worker.
        """
        from baldur.adapters.gunicorn.hooks import post_worker_init
        from baldur.factory import ProviderRegistry

        with patch.object(
            ProviderRegistry.cache, "invalidate_instance", autospec=True
        ) as m_invalidate:
            post_worker_init(_worker_with_preload(True))

        m_invalidate.assert_not_called()

    def test_does_not_reseed_the_random_module(self):
        """gunicorn's own ``Worker.init_process()`` calls ``util.seed()`` in
        every worker, after ``fork()`` and before this hook runs, and baldur's
        backoff jitter draws from exactly those module globals. A second
        reseed here asserted a defect that cannot occur under gunicorn.
        """
        import random

        from baldur.adapters.gunicorn.hooks import post_worker_init

        with patch.object(random, "seed", autospec=True) as m_seed:
            post_worker_init(_worker_with_preload(True))

        m_seed.assert_not_called()


class TestResetKafkaAfterForkBehavior:
    """``_reset_kafka_after_fork`` branch coverage.

    Re-authored from the deleted second surface's suite: the producer reset
    is the one member the consolidation carried, so its three branches must
    keep being exercised against the surface that now owns it.
    """

    @pytest.fixture(autouse=True)
    def _require_dormant(self):
        pytest.importorskip("baldur_dormant")

    def test_configured_producer_is_dropped_without_a_close(self):
        """``cleanup=False`` drops the reference without issuing
        ``close()``/``flush()``: the producer's background threads did not
        survive ``fork()``, so a call into them would deadlock."""
        from baldur.adapters.gunicorn.hooks import _reset_kafka_after_fork

        with (
            patch(
                "baldur_dormant.adapters.kafka.config.get_kafka_settings",
                autospec=True,
                return_value=SimpleNamespace(bootstrap_servers="kafka:9092"),
            ),
            patch(
                "baldur_dormant.adapters.kafka.producer.reset_kafka_producer",
                autospec=True,
            ) as m_reset,
            capture_logs() as cap_logs,
        ):
            _reset_kafka_after_fork(_worker_with_preload(True))

        m_reset.assert_called_once_with(cleanup=False)
        matching = [
            e for e in cap_logs if e.get("event") == "worker.postfork_kafka_reset"
        ]
        assert len(matching) == 1
        assert matching[0]["worker_id"] == os.getpid()

    def test_unconfigured_producer_is_left_alone(self):
        from baldur.adapters.gunicorn.hooks import _reset_kafka_after_fork

        with (
            patch(
                "baldur_dormant.adapters.kafka.config.get_kafka_settings",
                autospec=True,
                return_value=SimpleNamespace(bootstrap_servers=""),
            ),
            patch(
                "baldur_dormant.adapters.kafka.producer.reset_kafka_producer",
                autospec=True,
            ) as m_reset,
            capture_logs() as cap_logs,
        ):
            _reset_kafka_after_fork(_worker_with_preload(True))

        m_reset.assert_not_called()
        assert "worker.postfork_kafka_skipped" in [e["event"] for e in cap_logs]


class TestResetKafkaWithoutTheProducerPackageBehavior:
    """The stock-install branch: the producer adapter ships separately."""

    def test_missing_producer_package_is_a_logged_no_op(self, monkeypatch):
        """Blocking the import at ``sys.modules`` reproduces an open-source
        install, where this branch is the only one that ever runs."""
        from baldur.adapters.gunicorn.hooks import _reset_kafka_after_fork

        # A None entry makes ``import`` raise ImportError for that exact name.
        monkeypatch.setitem(sys.modules, "baldur_dormant.adapters.kafka.config", None)
        monkeypatch.setitem(sys.modules, "baldur_dormant.adapters.kafka.producer", None)

        with capture_logs() as cap_logs:
            _reset_kafka_after_fork(_worker_with_preload(True))

        matching = [
            e
            for e in cap_logs
            if e.get("event") == "worker.postfork_kafka_skipped_no_dormant"
        ]
        assert len(matching) == 1
        assert matching[0]["log_level"] == "debug"


class TestWorkerExitOutboxTeardownBehavior:
    """``worker_exit`` tears the DLQ outbox down, on every exit lane.

    A ``max_requests`` recycle initiates no shutdown at all, so the
    coordinator's handler never runs and this hook is the only teardown the
    outbox gets. Without it the buffered DLQ entries die with the daemon drain
    thread, silently, on default config.
    """

    _TEARDOWN = "baldur.services.dlq_outbox.outbox.stop_outbox_for_shutdown"

    def _result(self):
        from baldur.services.dlq_outbox.outbox import OutboxShutdownResult

        return OutboxShutdownResult(0, 0, 0, 0, 0, 0, 0)

    def test_recycle_exit_tears_the_outbox_down(self):
        """The lane with no drain window: nothing else in the process will do
        this before the interpreter exits."""
        from baldur.adapters.gunicorn.hooks import worker_exit
        from baldur.core.shutdown_coordinator import (
            ShutdownPhase,
            get_shutdown_coordinator,
        )

        coordinator = get_shutdown_coordinator()  # fresh ⇒ phase RUNNING
        assert coordinator.phase == ShutdownPhase.RUNNING

        with (
            patch.object(coordinator, "wait_for_shutdown", return_value=False),
            patch(self._TEARDOWN, return_value=self._result()) as m_teardown,
        ):
            worker_exit(_arbiter(), _exiting_worker())

        m_teardown.assert_called_once_with()

    def test_recycle_exit_tears_the_outbox_down_before_flushing_audit(self):
        """The outbox's final writes have to land while the audit WAL is still
        open, which is an ordering claim and not a "both ran" claim."""
        from baldur.adapters.gunicorn.hooks import worker_exit

        order: list[str] = []

        with (
            patch(
                self._TEARDOWN,
                side_effect=lambda: order.append("outbox") or self._result(),
            ),
            patch(
                "baldur.audit.async_audit_lifecycle.graceful_shutdown_audit_system",
                side_effect=lambda: order.append("audit"),
            ),
        ):
            worker_exit(_arbiter(), _exiting_worker())

        assert order == ["outbox", "audit"]

    def test_a_failing_teardown_does_not_cost_the_worker_its_audit_flush(self):
        """Step isolation, same reasoning as the Django reset above it."""
        from baldur.adapters.gunicorn.hooks import worker_exit

        with (
            patch(self._TEARDOWN, side_effect=RuntimeError("teardown blew up")),
            patch(
                "baldur.audit.async_audit_lifecycle.graceful_shutdown_audit_system"
            ) as m_flush,
            capture_logs() as cap_logs,
        ):
            worker_exit(_arbiter(), _exiting_worker())

        m_flush.assert_called_once_with()
        matching = [
            e
            for e in cap_logs
            if e.get("event") == "dlq_outbox.worker_exit_teardown_failed"
        ]
        assert len(matching) == 1
        assert matching[0]["log_level"] == "warning"

    def test_the_master_side_invocation_does_not_tear_down_an_outbox(self):
        """The teardown is process-global. Running it in the master would set
        the producer-coercion flag in the supervising process, and every worker
        forked afterwards would inherit an outbox it must not use."""
        from baldur.adapters.gunicorn.hooks import worker_exit

        with patch(self._TEARDOWN, return_value=self._result()) as m_teardown:
            worker_exit(_arbiter(), _foreign_worker())

        m_teardown.assert_not_called()

    def test_recycle_exit_completed_marker_carries_the_process_role(self):
        """One event name answers "did this worker's exit pipeline run to the
        end" on any adapter; the role is what tells the adapters apart."""
        from baldur.adapters.gunicorn.hooks import worker_exit

        with (
            patch(self._TEARDOWN, return_value=self._result()),
            capture_logs() as cap_logs,
        ):
            worker_exit(_arbiter(), _exiting_worker())

        matching = [
            e for e in cap_logs if e.get("event") == "shutdown.worker_exit_completed"
        ]
        assert len(matching) == 1
        assert matching[0]["process_role"] == "gunicorn_worker"

    def test_recycle_exit_completed_is_still_the_only_shutdown_prefixed_event(self):
        """**Negative** — this hook's ``shutdown.``-prefixed event list is an
        operator-facing contract, and the teardown must not add to it. Every
        line the outbox emits belongs in its own ``dlq_outbox.*`` namespace.
        """
        from baldur.adapters.gunicorn.hooks import worker_exit
        from baldur.core.shutdown_coordinator import get_shutdown_coordinator

        coordinator = get_shutdown_coordinator()  # fresh ⇒ phase RUNNING

        with (
            patch.object(coordinator, "wait_for_shutdown", return_value=False),
            capture_logs() as cap_logs,
        ):
            # The REAL teardown runs here — a stubbed one could not surface a
            # stray ``shutdown.*`` line from inside it.
            worker_exit(_arbiter(), _exiting_worker())

        shutdown_events = [
            e["event"]
            for e in cap_logs
            if str(e.get("event", "")).startswith("shutdown.")
        ]
        assert shutdown_events == ["shutdown.worker_exit_completed"]
