"""
Contract tests for Django startup event names.

Verifies fix(356) semantic inversion corrections:
- _unavailable in ImportError context (was _available / wrong semantic)
- _disabled for disabled features (was _enabled)
"""

from __future__ import annotations

from unittest.mock import patch


class TestDjangoAppsEventNameContract:
    """Django BaldurConfig event names follow logging standard semantics."""

    def test_celery_not_installed_event_name(self) -> None:
        """Logs 'baldur.celery_not_installed' (not celery_installed_skipping_task)."""
        from baldur.adapters.django.apps import BaldurConfig

        config = BaldurConfig.__new__(BaldurConfig)

        with (
            patch(
                "baldur.adapters.django.apps.logger",
            ) as mock_logger,
            patch.dict("sys.modules", {"celery": None}),
        ):
            config._autodiscover_celery_tasks()

        mock_logger.debug.assert_called()
        event_names = [c[0][0] for c in mock_logger.debug.call_args_list]
        assert "baldur.celery_not_installed" in event_names

    def test_quarantine_module_unavailable_event_name(self) -> None:
        """Logs 'baldur.quarantine_module_unavailable' (not module_available_quarantine)."""
        from baldur.adapters.django.apps import BaldurConfig

        config = BaldurConfig.__new__(BaldurConfig)

        with (
            patch(
                "baldur.adapters.django.apps.logger",
            ) as mock_logger,
            patch.dict(
                "sys.modules",
                {"baldur_pro.services.emergency_mode": None},
            ),
        ):
            config._activate_quarantine_mode(RuntimeError("test"))

        mock_logger.warning.assert_called()
        event_names = [c[0][0] for c in mock_logger.warning.call_args_list]
        assert "baldur.quarantine_module_unavailable" in event_names


class TestCacheWorkerEventNameContract:
    """Precomputed-cache start event names follow logging standard.

    604 D4 relocated the start from the Django glue into the framework-agnostic
    ``baldur.bootstrap._start_precomputed_cache_if_enabled`` helper, which emits
    ``baldur.precomputed_cache_module_not_available`` (DEBUG) on ImportError —
    the ``_module_not_available`` form mirrors the sibling meta_watchdog helper.
    """

    def test_precomputed_cache_module_not_available_event_name(
        self, monkeypatch
    ) -> None:
        """Helper logs 'baldur.precomputed_cache_module_not_available' on ImportError."""
        from baldur import bootstrap

        # Pass the autostart + non-master gates so the body reaches the import.
        monkeypatch.setenv("BALDUR_PRECOMPUTED_CACHE_AUTOSTART", "1")
        monkeypatch.delenv("SERVER_SOFTWARE", raising=False)
        monkeypatch.delenv("GUNICORN_WORKER", raising=False)

        with (
            patch.object(bootstrap, "logger") as mock_logger,
            patch(
                "baldur.settings.precomputed_cache.get_precomputed_cache_settings",
                side_effect=ImportError("module missing"),
            ),
        ):
            bootstrap._start_precomputed_cache_if_enabled()

        mock_logger.debug.assert_called()
        event_names = [c[0][0] for c in mock_logger.debug.call_args_list]
        assert "baldur.precomputed_cache_module_not_available" in event_names
