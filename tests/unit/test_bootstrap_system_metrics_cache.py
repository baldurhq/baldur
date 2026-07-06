"""608 D6 — bootstrap._start_system_metrics_cache_if_enabled unit tests.

Framework-independent start path for the psutil-backed SystemMetricsCache,
called from ``baldur.init()`` so Flask / FastAPI / plain-Python CLI get the same
background CPU/Memory cache Django gets (the live-CPU source for the
emergency-mode recovery gate + rate_controller starvation relief). Branch
coverage:

- ``BALDUR_SYSTEM_METRICS_CACHE_AUTOSTART`` in {0,false,no} -> skip (test hatch).
- Gunicorn master (``SERVER_SOFTWARE`` contains "gunicorn", no ``GUNICORN_WORKER``)
  -> skip (threads die after fork(); ``init()`` is not re-run in workers).
- ``SystemMetricsCacheSettings.enabled=False`` -> skip.
- Otherwise -> apply intervals from settings + ``start_system_metrics_cache()``.
- ImportError / runtime Exception -> swallowed (init() must continue).

609 D7 converged this helper onto the group-A pattern: it now carries the
``is_gunicorn_master()`` skip — the framework-agnostic ``post_worker_init`` hook
re-runs ``start_background_workers()`` per worker after fork(), so the cache no
longer needs to start in the master under ``--preload``.

Plus a real-worker start + double-call idempotency check (the cache's
``_running`` start-guard). Real-worker tests reset the global cache via
``reset_system_metrics_cache()`` on setup/teardown so the suite leaks no
``threading.Timer`` refresh thread.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from baldur.bootstrap import _start_system_metrics_cache_if_enabled


@pytest.fixture
def enable_autostart(monkeypatch):
    """Re-enable the autostart hatch (the unit-test process sets it to ``0`` in
    tests/conftest.py to keep ``init()`` from spawning the psutil timer)."""
    monkeypatch.setenv("BALDUR_SYSTEM_METRICS_CACHE_AUTOSTART", "1")


@pytest.fixture
def fresh_metrics_cache():
    """Provide a freshly-reset global cache; stop its timer on teardown.

    ``reset_system_metrics_cache()`` stops the worker (cancelling the daemon
    ``threading.Timer``) and rebinds a fresh instance — both before and after
    the test, so a real ``start()`` never leaks a background thread.
    """
    from baldur.services.system_metrics_cache import reset_system_metrics_cache

    reset_system_metrics_cache()
    yield
    reset_system_metrics_cache()


class TestStartSystemMetricsCacheIfEnabled:
    """608 D6: framework-independent system-metrics-cache start gating."""

    @pytest.mark.parametrize("disabled_value", ["0", "false", "no"])
    def test_autostart_disabled_returns_before_settings_lookup(
        self, monkeypatch, disabled_value
    ):
        """An autostart escape-hatch value returns before any settings work."""
        monkeypatch.setenv("BALDUR_SYSTEM_METRICS_CACHE_AUTOSTART", disabled_value)

        with patch(
            "baldur.settings.system_metrics_cache.get_system_metrics_cache_settings",
            autospec=True,
        ) as get_settings:
            _start_system_metrics_cache_if_enabled()

        get_settings.assert_not_called()

    def test_gunicorn_master_skips_start(self, monkeypatch):
        """609 D7: under the Gunicorn master the start is skipped (fork-safety) —
        converged onto the group-A pattern. The per-worker post_worker_init hook
        re-runs the start after setting GUNICORN_WORKER=1."""
        monkeypatch.setenv("BALDUR_SYSTEM_METRICS_CACHE_AUTOSTART", "1")
        monkeypatch.setenv("SERVER_SOFTWARE", "gunicorn/21.2.0")
        monkeypatch.delenv("GUNICORN_WORKER", raising=False)

        with patch(
            "baldur.settings.system_metrics_cache.get_system_metrics_cache_settings",
            autospec=True,
        ) as get_settings:
            _start_system_metrics_cache_if_enabled()

        # Master-skip returns before the settings lookup.
        get_settings.assert_not_called()

    def test_disabled_settings_skips_start(self, enable_autostart):
        """enabled=False stops before the cache start."""
        with (
            patch(
                "baldur.settings.system_metrics_cache.get_system_metrics_cache_settings",
                return_value=MagicMock(enabled=False),
            ),
            patch(
                "baldur.services.system_metrics_cache.start_system_metrics_cache"
            ) as start,
        ):
            _start_system_metrics_cache_if_enabled()

        start.assert_not_called()

    def test_enabled_starts_cache(self, enable_autostart):
        """enabled -> start_system_metrics_cache() is invoked once."""
        settings = MagicMock(
            enabled=True,
            refresh_interval=1.0,
            sample_interval=0.1,
            max_age_seconds=5.0,
        )
        with (
            patch(
                "baldur.settings.system_metrics_cache.get_system_metrics_cache_settings",
                return_value=settings,
            ),
            patch(
                "baldur.services.system_metrics_cache.get_system_metrics_cache",
                return_value=MagicMock(),
            ),
            patch(
                "baldur.services.system_metrics_cache.start_system_metrics_cache"
            ) as start,
        ):
            _start_system_metrics_cache_if_enabled()

        start.assert_called_once()

    def test_enabled_applies_intervals_from_settings(self, enable_autostart):
        """The cache's intervals are taken from settings before start."""
        settings = MagicMock(
            enabled=True,
            refresh_interval=2.0,
            sample_interval=0.2,
            max_age_seconds=8.0,
        )
        cache = MagicMock()
        with (
            patch(
                "baldur.settings.system_metrics_cache.get_system_metrics_cache_settings",
                return_value=settings,
            ),
            patch(
                "baldur.services.system_metrics_cache.get_system_metrics_cache",
                return_value=cache,
            ),
            patch("baldur.services.system_metrics_cache.start_system_metrics_cache"),
        ):
            _start_system_metrics_cache_if_enabled()

        assert cache._refresh_interval == settings.refresh_interval
        assert cache._sample_interval == settings.sample_interval
        assert cache._max_age_seconds == settings.max_age_seconds

    def test_import_error_swallowed(self, enable_autostart):
        """An ImportError inside the body is swallowed (init() continues)."""
        with patch(
            "baldur.settings.system_metrics_cache.get_system_metrics_cache_settings",
            side_effect=ImportError("module missing"),
        ):
            _start_system_metrics_cache_if_enabled()  # must not raise

    def test_runtime_error_swallowed(self, enable_autostart):
        """A start_system_metrics_cache() crash is swallowed (init() continues)."""
        with (
            patch(
                "baldur.settings.system_metrics_cache.get_system_metrics_cache_settings",
                return_value=MagicMock(
                    enabled=True,
                    refresh_interval=1.0,
                    sample_interval=0.1,
                    max_age_seconds=5.0,
                ),
            ),
            patch(
                "baldur.services.system_metrics_cache.start_system_metrics_cache",
                side_effect=RuntimeError("thread spawn boom"),
            ),
        ):
            _start_system_metrics_cache_if_enabled()  # must not raise


class TestStartSystemMetricsCacheRealWorker:
    """608 D6: real-worker start + double-call idempotency (_running guard)."""

    def test_enabled_starts_real_worker(self, enable_autostart, fresh_metrics_cache):
        """All gates pass -> the global cache worker is running."""
        from baldur.services.system_metrics_cache import get_system_metrics_cache

        _start_system_metrics_cache_if_enabled()

        assert get_system_metrics_cache().is_running() is True

    def test_double_call_idempotent(self, enable_autostart, fresh_metrics_cache):
        """Two helper invocations yield exactly one running worker.

        The cache's ``_running`` start-guard makes the second ``start()`` a
        no-op, reproducing the Django runserver double-call at the worker layer.
        """
        from baldur.services.system_metrics_cache import get_system_metrics_cache

        _start_system_metrics_cache_if_enabled()
        first = get_system_metrics_cache()

        _start_system_metrics_cache_if_enabled()
        second = get_system_metrics_cache()

        assert second is first
        assert second.is_running() is True
