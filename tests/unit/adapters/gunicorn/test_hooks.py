"""Unit tests for ``baldur.adapters.gunicorn.hooks``.

Covers the contract documented in the module docstring:

- ``post_worker_init`` sets ``GUNICORN_WORKER=1`` and initializes the
  shutdown coordinator with a ``RequestTracker``.
- ``post_worker_init`` re-starts the framework-agnostic OSS background
  daemon workers via ``baldur.bootstrap.start_background_workers()`` for
  **all** adapters (even when Django is absent), then re-starts the
  Django-only extra threads when the Django adapter is importable, and
  silently no-ops the Django branch when it is not.
- ``worker_int`` calls ``coordinator.initiate_shutdown()``.
- ``worker_exit`` waits for drain (with the documented 30 s timeout)
  and stops Django background threads when available.
- The package re-exports the three hooks under stable names so users
  can ``from baldur.adapters.gunicorn import post_worker_init,
  worker_int, worker_exit``.
"""

from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock, patch

import pytest
from structlog.testing import capture_logs


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
    spawn daemon threads (MetricHydrator timer, precomputed-cache /
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


class TestWorkerInt:
    """``worker_int`` contract."""

    def test_calls_initiate_shutdown(self):
        from baldur.adapters.gunicorn.hooks import worker_int
        from baldur.core.shutdown_coordinator import get_shutdown_coordinator

        coordinator = get_shutdown_coordinator()
        with patch.object(coordinator, "initiate_shutdown") as m_initiate:
            worker_int(worker=MagicMock())

        m_initiate.assert_called_once_with()


class TestWorkerExit:
    """``worker_exit`` contract."""

    def test_waits_for_shutdown_with_30s_timeout(self):
        from baldur.adapters.gunicorn.hooks import worker_exit
        from baldur.core.shutdown_coordinator import get_shutdown_coordinator

        coordinator = get_shutdown_coordinator()
        with patch.object(coordinator, "wait_for_shutdown") as m_wait:
            worker_exit(worker=MagicMock(), server=MagicMock())

        m_wait.assert_called_once_with(timeout=30.0)

    def test_calls_django_stop_background_threads_when_available(self):
        from baldur.adapters.gunicorn.hooks import worker_exit

        with patch(
            "baldur.adapters.django.apps.BaldurConfig.stop_background_threads"
        ) as m_stop:
            worker_exit(worker=MagicMock(), server=MagicMock())

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
            worker_exit(worker=MagicMock(), server=MagicMock())

        matching = [e for e in cap_logs if e.get("event") == "shutdown.worker_drained"]
        assert len(matching) == 1
        assert matching[0]["log_level"] == "info"

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
            worker_exit(worker=MagicMock(), server=MagicMock())

        matching = [
            e for e in cap_logs if e.get("event") == "shutdown.worker_drain_incomplete"
        ]
        assert len(matching) == 1
        assert matching[0]["log_level"] == "warning"
        assert matching[0]["phase"] == "draining"

    def test_no_shutdown_log_when_drain_not_initiated(self):
        """A normal worker exit with no shutdown initiated (phase RUNNING)
        must not emit a ``shutdown.*`` terminal log — otherwise routine
        worker recycles / reloads would warn spuriously."""
        from baldur.adapters.gunicorn.hooks import worker_exit
        from baldur.core.shutdown_coordinator import get_shutdown_coordinator

        coordinator = get_shutdown_coordinator()  # fresh ⇒ phase RUNNING
        with (
            patch.object(coordinator, "wait_for_shutdown", return_value=False),
            capture_logs() as cap_logs,
        ):
            worker_exit(worker=MagicMock(), server=MagicMock())

        shutdown_events = [
            e["event"]
            for e in cap_logs
            if str(e.get("event", "")).startswith("shutdown.")
        ]
        assert shutdown_events == []
