"""Unit tests for BaldurConfig fork-safety methods (Section 5.2).

Tests _should_start_background_threads(), start_background_threads(),
and _reset_all_background_state() added in commit cf89883a.

Reference:
    docs/baldur/middleware_system/316_GUNICORN_PRELOAD_OPTIMIZATION.md §5.2
"""

from __future__ import annotations

import threading
from unittest.mock import MagicMock, patch

from baldur.adapters.django.apps import BaldurConfig
from baldur.adapters.django.startup.metric_hydrator import MetricHydrator


class TestShouldStartBackgroundThreadsContract:
    """Contract: thread start decision based on env vars per §5.2."""

    def test_gunicorn_master_returns_false(self):
        """Gunicorn Master (SERVER_SOFTWARE set, no GUNICORN_WORKER) → False."""
        env = {"SERVER_SOFTWARE": "gunicorn/21.2.0"}
        with patch.dict("os.environ", env, clear=True), patch("sys.argv", ["gunicorn"]):
            assert BaldurConfig._should_start_background_threads() is False

    def test_gunicorn_worker_returns_true(self):
        """Gunicorn Worker (GUNICORN_WORKER='1') → True."""
        env = {
            "SERVER_SOFTWARE": "gunicorn/21.2.0",
            "GUNICORN_WORKER": "1",
        }
        with patch.dict("os.environ", env, clear=True), patch("sys.argv", ["gunicorn"]):
            assert BaldurConfig._should_start_background_threads() is True

    def test_dev_server_runserver_returns_true(self):
        """Dev server (runserver in sys.argv) → True."""
        with (
            patch.dict("os.environ", {}, clear=True),
            patch("sys.argv", ["manage.py", "runserver"]),
        ):
            assert BaldurConfig._should_start_background_threads() is True

    def test_dev_server_env_var_returns_true(self):
        """Dev server (DJANGO_DEV_SERVER='1') → True."""
        env = {"DJANGO_DEV_SERVER": "1"}
        with (
            patch.dict("os.environ", env, clear=True),
            patch("sys.argv", ["manage.py"]),
        ):
            assert BaldurConfig._should_start_background_threads() is True

    def test_non_gunicorn_non_dev_returns_true(self):
        """Regular process (no gunicorn, no runserver) → True (default)."""
        with (
            patch.dict("os.environ", {}, clear=True),
            patch("sys.argv", ["manage.py", "migrate"]),
        ):
            assert BaldurConfig._should_start_background_threads() is True


class TestResetAllBackgroundStateBehavior:
    """Behavior: _reset_all_background_state resets all guards."""

    def test_resets_all_guards_to_false(self):
        """The Django-only duplicate-start guards must be reset to False.

        The OSS-5 init()-started workers (precomputed-cache, meta-watchdog,
        system-metrics-cache, capacity-reservation, cell-topology) no longer keep
        a Django-side guard — they are started via
        ``baldur.bootstrap.start_background_workers()`` and carry their own
        service-level ``_running``/``_active`` idempotency. The only remaining
        Django-side guard is the gauge-hydration flag (the correlation loop's flag
        is reset under its own lock).
        """
        # Given — set the guard to True
        MetricHydrator._hydration_done = True

        # When
        BaldurConfig._reset_all_background_state()

        # Then
        assert MetricHydrator._hydration_done is False

    def test_idempotent_double_reset_no_error(self):
        """Resetting twice in a row does not raise."""
        BaldurConfig._reset_all_background_state()
        BaldurConfig._reset_all_background_state()
        assert MetricHydrator._hydration_done is False


class TestStartBackgroundThreadsBehavior:
    """Behavior: start_background_threads() resets guards and starts threads."""

    @patch("django.apps.apps")
    def test_calls_reset_then_start(self, mock_apps):
        """Resets state, gets app config, and calls _start_all_background_threads."""
        mock_config = MagicMock()
        mock_apps.get_app_config.return_value = mock_config

        # Given — guards are set
        MetricHydrator._hydration_done = True

        # When
        BaldurConfig.start_background_threads()

        # Then — guards are reset
        assert MetricHydrator._hydration_done is False
        mock_apps.get_app_config.assert_called_once_with("baldur")
        mock_config._start_all_background_threads.assert_called_once()

    @patch("django.apps.apps")
    def test_handles_app_config_error_gracefully(self, mock_apps):
        """If apps.get_app_config raises, logs warning but does not crash."""
        mock_apps.get_app_config.side_effect = LookupError("not found")

        # Should not raise
        BaldurConfig.start_background_threads()

    def test_thread_safety_concurrent_reset(self):
        """Multiple threads calling _reset_all_background_state concurrently."""
        errors = []

        def worker():
            try:
                for _ in range(100):
                    BaldurConfig._reset_all_background_state()
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0
