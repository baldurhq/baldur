"""604 D1 — bootstrap._start_precomputed_cache_if_enabled unit tests.

Framework-independent start path for the Precomputed Cache proactive-refresh
worker, called from ``baldur.init()`` so Flask / FastAPI / plain-Python CLI get
the same proactive 3-tier refresh + L1↔L2 drift detection Django gets. Branch
coverage:

- ``BALDUR_PRECOMPUTED_CACHE_AUTOSTART`` in {0,false,no} → skip (test hatch).
- Gunicorn master (``SERVER_SOFTWARE`` contains "gunicorn", no ``GUNICORN_WORKER``)
  → skip (threads die after fork(); ``init()`` is not re-run in workers).
- ``PrecomputedCacheSettings.enabled=False`` → skip.
- Otherwise → ``register_default_compute_functions()`` + ``start_precomputed_cache()``.
- ImportError / runtime Exception → swallowed (init() must continue).

Plus a real-worker double-call idempotency check (D5) and the D6
reset/register no-divergence assertion. Real-worker tests cancel the daemon
``threading.Timer`` via ``reset_precomputed_cache_worker()`` on teardown so the
suite leaks no timer thread (the failure D3 guards against).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from baldur.bootstrap import _start_precomputed_cache_if_enabled


@pytest.fixture
def non_gunicorn_autostart(monkeypatch):
    """Pass the autostart + non-master gates so the body runs.

    The unit-test process sets ``BALDUR_PRECOMPUTED_CACHE_AUTOSTART=0`` globally
    (tests/conftest.py); re-enable it here and strip any gunicorn env so
    ``is_gunicorn_master()`` returns False.
    """
    monkeypatch.setenv("BALDUR_PRECOMPUTED_CACHE_AUTOSTART", "1")
    monkeypatch.delenv("SERVER_SOFTWARE", raising=False)
    monkeypatch.delenv("GUNICORN_WORKER", raising=False)


@pytest.fixture
def fresh_real_worker():
    """Provide a freshly-reset global worker; cancel its timer on teardown.

    ``reset_precomputed_cache_worker()`` stops the worker (cancelling the daemon
    ``threading.Timer`` and unsubscribing the EventBus handler) and rebinds a
    fresh instance — both before and after the test, so a real ``start()`` never
    leaks a background thread into the rest of the suite.
    """
    from baldur.services.precomputed_cache import reset_precomputed_cache_worker

    reset_precomputed_cache_worker()
    yield
    reset_precomputed_cache_worker()


class TestStartPrecomputedCacheIfEnabled:
    """604 D1: framework-independent precomputed-cache start gating."""

    @pytest.mark.parametrize("disabled_value", ["0", "false", "no"])
    def test_autostart_disabled_returns_before_master_check(
        self, monkeypatch, disabled_value
    ):
        """An autostart escape-hatch value returns before any further work."""
        monkeypatch.setenv("BALDUR_PRECOMPUTED_CACHE_AUTOSTART", disabled_value)

        with patch(
            "baldur.core.process_utils.is_gunicorn_master", autospec=True
        ) as is_master:
            _start_precomputed_cache_if_enabled()

        is_master.assert_not_called()

    def test_gunicorn_master_skips_start(self, monkeypatch):
        """In the Gunicorn master the start is skipped (fork-safety)."""
        monkeypatch.setenv("BALDUR_PRECOMPUTED_CACHE_AUTOSTART", "1")
        monkeypatch.setenv("SERVER_SOFTWARE", "gunicorn/21.2.0")
        monkeypatch.delenv("GUNICORN_WORKER", raising=False)

        with patch(
            "baldur.settings.precomputed_cache.get_precomputed_cache_settings",
            autospec=True,
        ) as get_settings:
            _start_precomputed_cache_if_enabled()

        # Master-skip happens before the settings lookup.
        get_settings.assert_not_called()

    def test_disabled_settings_skips_register_and_start(self, non_gunicorn_autostart):
        """enabled=False stops before register/start collaborators."""
        with (
            patch(
                "baldur.settings.precomputed_cache.get_precomputed_cache_settings",
                return_value=MagicMock(enabled=False),
            ),
            patch(
                "baldur.services.precomputed_cache.register_default_compute_functions"
            ) as register,
            patch("baldur.services.precomputed_cache.start_precomputed_cache") as start,
        ):
            _start_precomputed_cache_if_enabled()

        register.assert_not_called()
        start.assert_not_called()

    def test_enabled_starts_worker(self, non_gunicorn_autostart):
        """enabled → register + start collaborators are each invoked once."""
        with (
            patch(
                "baldur.settings.precomputed_cache.get_precomputed_cache_settings",
                return_value=MagicMock(enabled=True),
            ),
            patch(
                "baldur.services.precomputed_cache.register_default_compute_functions"
            ) as register,
            patch("baldur.services.precomputed_cache.start_precomputed_cache") as start,
        ):
            _start_precomputed_cache_if_enabled()

        register.assert_called_once()
        start.assert_called_once()

    def test_import_error_swallowed(self, non_gunicorn_autostart):
        """An ImportError inside the body is swallowed (init() continues)."""
        with patch(
            "baldur.settings.precomputed_cache.get_precomputed_cache_settings",
            side_effect=ImportError("module missing"),
        ):
            _start_precomputed_cache_if_enabled()  # must not raise

    def test_runtime_error_swallowed(self, non_gunicorn_autostart):
        """A start_precomputed_cache() crash is swallowed (init() continues)."""
        with (
            patch(
                "baldur.settings.precomputed_cache.get_precomputed_cache_settings",
                return_value=MagicMock(enabled=True),
            ),
            patch(
                "baldur.services.precomputed_cache.register_default_compute_functions"
            ),
            patch(
                "baldur.services.precomputed_cache.start_precomputed_cache",
                side_effect=RuntimeError("thread spawn boom"),
            ),
        ):
            _start_precomputed_cache_if_enabled()  # must not raise


class TestStartPrecomputedCacheRealWorker:
    """604 D1/D5/D6: real-worker start, double-call idempotency, reset divergence."""

    def test_all_gates_pass_starts_real_worker(
        self, non_gunicorn_autostart, fresh_real_worker
    ):
        """All gates pass → 3 compute functions registered and worker running."""
        from baldur.services.precomputed_cache import get_precomputed_cache_worker

        _start_precomputed_cache_if_enabled()

        worker = get_precomputed_cache_worker()
        assert worker.is_running() is True
        assert len(worker.get_stats()["registered_keys"]) == 3

    def test_double_call_idempotent(self, non_gunicorn_autostart, fresh_real_worker):
        """D5: two helper invocations yield exactly one running worker.

        Both production call sites — ``init()`` and the gunicorn
        ``post_worker_init`` hook — funnel through
        ``start_background_workers()`` into this same helper, so calling it twice
        reproduces the init()-then-per-worker double-start at the worker layer.
        The worker's ``_running`` guard makes the second ``start()`` a no-op;
        ``register`` dict-overwrite keeps exactly 3 keys.
        """
        from baldur.services.precomputed_cache import get_precomputed_cache_worker

        _start_precomputed_cache_if_enabled()
        first = get_precomputed_cache_worker()
        started_at = first._started_at

        _start_precomputed_cache_if_enabled()
        second = get_precomputed_cache_worker()

        assert second is first
        assert second.is_running() is True
        assert len(second.get_stats()["registered_keys"]) == 3
        # _running guard short-circuits start() → _started_at is not refreshed.
        assert second._started_at == started_at

    def test_reset_then_register_targets_live_worker(self, fresh_real_worker):
        """D6: after a reset, register targets the worker get_*() returns.

        Pre-D6, ``register_default_compute_functions`` bound the worker via a
        module-level ``_worker`` import, so a ``reset_precomputed_cache_worker()``
        rebind left registration pointing at the stale instance. Lazy resolution
        via ``get_precomputed_cache_worker()`` removes that divergence.
        """
        from baldur.services.precomputed_cache import (
            get_precomputed_cache_worker,
            register_default_compute_functions,
            reset_precomputed_cache_worker,
        )

        reset_precomputed_cache_worker()
        register_default_compute_functions()

        assert len(get_precomputed_cache_worker().get_stats()["registered_keys"]) == 3
