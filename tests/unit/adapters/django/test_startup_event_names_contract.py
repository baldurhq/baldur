"""
Contract tests for Django startup event names.

Verifies fix(356) semantic inversion corrections:
- _unavailable in ImportError context (was _available / wrong semantic)
- _disabled for disabled features (was _enabled)
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch


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


class TestEnvAuditorEventNameContract:
    """EnvironmentAuditor event names: semantic inversion fixes.

    Note:
        env_snapshot_unavailable test was removed in 416 D21 — env_var snapshot
        logging was relocated from EnvironmentAuditor.audit() (deleted) to
        baldur.bootstrap.init(). Coverage now lives in
        tests/unit/audit/test_env_snapshot.py.
    """

    def test_distributed_hash_chain_disabled_event_name(self) -> None:
        """Logs 'baldur.distributed_hash_chain_disabled' (not _enabled)."""
        from baldur.adapters.django.startup.env_auditor import (
            EnvironmentAuditor,
        )

        with (
            patch(
                "baldur.adapters.django.startup.env_auditor.logger",
            ) as mock_logger,
            patch(
                "baldur.adapters.django.startup.env_auditor.settings",
                BALDUR_DISTRIBUTED_HASH_CHAIN=False,
            ),
        ):
            EnvironmentAuditor.sync_hash_chain_on_startup()

        mock_logger.debug.assert_called()
        event_names = [c[0][0] for c in mock_logger.debug.call_args_list]
        assert "baldur.distributed_hash_chain_disabled" in event_names

    def test_integrity_module_unavailable_event_name(self) -> None:
        """Logs 'baldur.integrity_module_unavailable' (not integrity_module_available_hash)."""
        from baldur.adapters.django.startup.env_auditor import (
            EnvironmentAuditor,
        )

        mock_settings = MagicMock()
        mock_settings.BALDUR_DISTRIBUTED_HASH_CHAIN = True

        with (
            patch(
                "baldur.adapters.django.startup.env_auditor.logger",
            ) as mock_logger,
            patch(
                "baldur.adapters.django.startup.env_auditor.settings",
                mock_settings,
            ),
            patch.object(
                EnvironmentAuditor,
                "_get_redis_client_for_hash_chain",
                return_value=MagicMock(),
            ),
            patch.dict("sys.modules", {"baldur.audit.integrity": None}),
        ):
            EnvironmentAuditor.sync_hash_chain_on_startup()

        mock_logger.debug.assert_called()
        event_names = [c[0][0] for c in mock_logger.debug.call_args_list]
        assert "baldur.integrity_module_unavailable" in event_names


class TestMetricHydratorEventNameContract:
    """MetricHydrator event names: semantic inversion fixes."""

    def test_metric_hydrator_source_has_correct_event_names(self) -> None:
        """MetricHydrator source uses correct event names (not old _available / _non suffixes)."""
        import inspect

        from baldur.adapters.django.startup import metric_hydrator

        source = inspect.getsource(metric_hydrator)
        # Fixed event names present
        assert '"baldur.reconciler_module_unavailable"' in source
        assert '"baldur.gauge_hydration_failed"' in source
        # (system_metrics_cache events relocated to baldur.bootstrap — 608 D6)
        # Old incorrect names absent
        assert '"baldur.reconciler_module_available"' not in source
        assert '"baldur.gauge_hydration_failed_non"' not in source
        assert '"baldur.module_available"' not in source
