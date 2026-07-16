"""
Tests for 5 shutdown handlers introduced in 399.

Sources:
- src/baldur/resilience/bulkhead/shutdown.py
- src/baldur/core/hedging/shutdown.py
- src/baldur/services/auto_tuning/shutdown.py
- src/baldur/services/precomputed_cache/shutdown.py

MLModelsShutdownHandler tests moved to
tests/dormant/unit/services/test_ml_models_shutdown.py (599 D12/D14 — the
ml_models feature relocated to the private distribution).

Each handler:
- Contract: integrate_with_shutdown_coordinator() factory returns handler or None
- Behavior: on_shutdown_start() calls expected cleanup
- Behavior: on_drain_complete() does not raise
- Behavior: integrate_with_shutdown_coordinator() returns None on creation error
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

# =============================================================================
# BulkheadShutdownHandler
# =============================================================================


class TestBulkheadShutdownHandlerContract:
    """Contract: BulkheadShutdownHandler factory returns handler or None."""

    @pytest.fixture(autouse=True)
    def _require_pro(self):
        pytest.importorskip("baldur_pro")

    def test_integrate_returns_handler_instance(self):
        """integrate_with_shutdown_coordinator() returns BulkheadShutdownHandler."""
        from baldur_pro.services.bulkhead.shutdown import (
            BulkheadShutdownHandler,
            integrate_with_shutdown_coordinator,
        )

        handler = integrate_with_shutdown_coordinator()
        assert isinstance(handler, BulkheadShutdownHandler)

    def test_integrate_returns_none_on_creation_error(self):
        """integrate_with_shutdown_coordinator() returns None when constructor raises."""
        with patch(
            "baldur_pro.services.bulkhead.shutdown.BulkheadShutdownHandler",
            autospec=True,
            side_effect=RuntimeError("init failed"),
        ):
            from baldur_pro.services.bulkhead.shutdown import (
                integrate_with_shutdown_coordinator,
            )

            result = integrate_with_shutdown_coordinator()
            assert result is None


class TestBulkheadShutdownHandlerBehavior:
    """Behavior tests for BulkheadShutdownHandler."""

    @pytest.fixture(autouse=True)
    def _require_pro(self):
        pytest.importorskip("baldur_pro")

    @patch(
        "baldur_pro.services.bulkhead.registry.get_bulkhead_registry",
        autospec=True,
    )
    @patch(
        "baldur.services.bulkhead.metrics.get_metrics_updater",
        autospec=True,
    )
    def test_on_shutdown_start_stops_metrics_updater(
        self, mock_get_updater, mock_get_registry
    ):
        """on_shutdown_start() stops the metrics updater."""
        from baldur_pro.services.bulkhead.shutdown import (
            BulkheadShutdownHandler,
        )

        mock_updater = MagicMock()
        mock_get_updater.return_value = mock_updater
        mock_registry = MagicMock()
        mock_registry.list_names.return_value = []
        mock_get_registry.return_value = mock_registry

        handler = BulkheadShutdownHandler()
        handler.on_shutdown_start()

        mock_updater.stop.assert_called_once()

    @patch(
        "baldur_pro.services.bulkhead.registry.get_bulkhead_registry",
        autospec=True,
    )
    @patch(
        "baldur.services.bulkhead.metrics.get_metrics_updater",
        autospec=True,
    )
    def test_on_shutdown_start_shuts_down_thread_pool_bulkheads(
        self, mock_get_updater, mock_get_registry
    ):
        """on_shutdown_start() calls shutdown(wait=False) on ThreadPoolBulkheads."""
        from baldur_pro.services.bulkhead.shutdown import (
            BulkheadShutdownHandler,
        )
        from baldur_pro.services.bulkhead.threadpool import ThreadPoolBulkhead

        mock_bulkhead = MagicMock(spec=ThreadPoolBulkhead)
        mock_registry = MagicMock()
        mock_registry.list_names.return_value = ["test_pool"]
        mock_registry.get.return_value = mock_bulkhead
        mock_get_registry.return_value = mock_registry

        handler = BulkheadShutdownHandler()
        handler.on_shutdown_start()

        mock_bulkhead.shutdown.assert_called_once_with(wait=False)

    def test_on_drain_complete_does_not_raise(self):
        """on_drain_complete() does not raise on import failures."""
        from baldur_pro.services.bulkhead.shutdown import (
            BulkheadShutdownHandler,
        )

        handler = BulkheadShutdownHandler()

        with patch(
            "baldur_pro.services.bulkhead.registry.get_bulkhead_registry",
            autospec=True,
            side_effect=ImportError("unavailable"),
        ):
            # Should not raise
            handler.on_drain_complete()


# =============================================================================
# HedgingShutdownHandler
# =============================================================================


class TestHedgingShutdownHandlerContract:
    """Contract: HedgingShutdownHandler factory returns handler or None."""

    @pytest.fixture(autouse=True)
    def _require_pro(self):
        pytest.importorskip("baldur_pro")

    def test_integrate_returns_handler_instance(self):
        """integrate_with_shutdown_coordinator() returns HedgingShutdownHandler."""
        from baldur_pro.services.hedging.shutdown import (
            HedgingShutdownHandler,
            integrate_with_shutdown_coordinator,
        )

        handler = integrate_with_shutdown_coordinator()
        assert isinstance(handler, HedgingShutdownHandler)

    def test_integrate_returns_none_on_creation_error(self):
        """integrate_with_shutdown_coordinator() returns None when constructor raises."""
        with patch(
            "baldur_pro.services.hedging.shutdown.HedgingShutdownHandler",
            autospec=True,
            side_effect=RuntimeError("init failed"),
        ):
            from baldur_pro.services.hedging.shutdown import (
                integrate_with_shutdown_coordinator,
            )

            result = integrate_with_shutdown_coordinator()
            assert result is None


class TestHedgingShutdownHandlerBehavior:
    """Behavior tests for HedgingShutdownHandler."""

    @pytest.fixture(autouse=True)
    def _require_pro(self):
        pytest.importorskip("baldur_pro")

    @patch(
        "baldur_pro.services.hedging.executor.HedgingExecutor",
        autospec=True,
    )
    def test_on_shutdown_start_calls_shutdown_executor(self, mock_executor_cls):
        """on_shutdown_start() calls HedgingExecutor.shutdown_executor()."""
        from baldur_pro.services.hedging.shutdown import HedgingShutdownHandler

        handler = HedgingShutdownHandler()
        handler.on_shutdown_start()

        mock_executor_cls.shutdown_executor.assert_called_once()

    def test_on_drain_complete_does_not_raise(self):
        """on_drain_complete() does not raise (noop)."""
        from baldur_pro.services.hedging.shutdown import HedgingShutdownHandler

        handler = HedgingShutdownHandler()
        handler.on_drain_complete()  # Should not raise

    @patch(
        "baldur_pro.services.hedging.executor.HedgingExecutor",
        autospec=True,
        side_effect=ImportError("not available"),
    )
    def test_on_shutdown_start_handles_import_error(self, mock_executor_cls):
        """on_shutdown_start() gracefully handles ImportError."""
        from baldur_pro.services.hedging.shutdown import HedgingShutdownHandler

        handler = HedgingShutdownHandler()
        handler.on_shutdown_start()  # Should not raise


# =============================================================================
# AutoTuningShutdownHandler
# =============================================================================


class TestAutoTuningShutdownHandlerContract:
    """Contract: AutoTuningShutdownHandler factory returns handler or None."""

    @pytest.fixture(autouse=True)
    def _require_pro(self):
        pytest.importorskip("baldur_pro")

    def test_integrate_returns_handler_instance(self):
        """integrate_with_shutdown_coordinator() returns AutoTuningShutdownHandler."""
        from baldur_pro.services.auto_tuning.shutdown import (
            AutoTuningShutdownHandler,
            integrate_with_shutdown_coordinator,
        )

        handler = integrate_with_shutdown_coordinator()
        assert isinstance(handler, AutoTuningShutdownHandler)

    def test_integrate_returns_none_on_creation_error(self):
        """integrate_with_shutdown_coordinator() returns None when constructor raises."""
        with patch(
            "baldur_pro.services.auto_tuning.shutdown.AutoTuningShutdownHandler",
            autospec=True,
            side_effect=RuntimeError("init failed"),
        ):
            from baldur_pro.services.auto_tuning.shutdown import (
                integrate_with_shutdown_coordinator,
            )

            result = integrate_with_shutdown_coordinator()
            assert result is None


class TestAutoTuningShutdownHandlerBehavior:
    """Behavior tests for AutoTuningShutdownHandler."""

    @pytest.fixture(autouse=True)
    def _require_pro(self):
        pytest.importorskip("baldur_pro")

    @patch(
        "baldur_pro.services.auto_tuning.service.get_auto_tuning_service",
        autospec=True,
    )
    def test_on_shutdown_start_calls_service_stop(self, mock_get_service):
        """on_shutdown_start() calls get_auto_tuning_service().stop()."""
        from baldur_pro.services.auto_tuning.shutdown import (
            AutoTuningShutdownHandler,
        )

        mock_service = MagicMock()
        mock_get_service.return_value = mock_service

        handler = AutoTuningShutdownHandler()
        handler.on_shutdown_start()

        mock_service.stop.assert_called_once()

    def test_on_drain_complete_does_not_raise(self):
        """on_drain_complete() does not raise (noop)."""
        from baldur_pro.services.auto_tuning.shutdown import (
            AutoTuningShutdownHandler,
        )

        handler = AutoTuningShutdownHandler()
        handler.on_drain_complete()  # Should not raise

    def test_on_shutdown_start_handles_import_error(self):
        """on_shutdown_start() gracefully handles exceptions from service import."""
        from baldur_pro.services.auto_tuning.shutdown import (
            AutoTuningShutdownHandler,
        )

        handler = AutoTuningShutdownHandler()
        with patch(
            "baldur_pro.services.auto_tuning.service.get_auto_tuning_service",
            autospec=True,
            side_effect=ImportError("not available"),
        ):
            handler.on_shutdown_start()  # Should not raise


# =============================================================================
# PrecomputedCacheShutdownHandler
# =============================================================================


class TestPrecomputedCacheShutdownHandlerContract:
    """Contract: PrecomputedCacheShutdownHandler factory returns handler or None."""

    def test_integrate_returns_handler_instance(self):
        """integrate_with_shutdown_coordinator() returns PrecomputedCacheShutdownHandler."""
        from baldur.services.precomputed_cache.shutdown import (
            PrecomputedCacheShutdownHandler,
            integrate_with_shutdown_coordinator,
        )

        handler = integrate_with_shutdown_coordinator()
        assert isinstance(handler, PrecomputedCacheShutdownHandler)

    def test_integrate_returns_none_on_creation_error(self):
        """integrate_with_shutdown_coordinator() returns None when constructor raises."""
        with patch(
            "baldur.services.precomputed_cache.shutdown.PrecomputedCacheShutdownHandler",
            autospec=True,
            side_effect=RuntimeError("init failed"),
        ):
            from baldur.services.precomputed_cache.shutdown import (
                integrate_with_shutdown_coordinator,
            )

            result = integrate_with_shutdown_coordinator()
            assert result is None


class TestPrecomputedCacheShutdownHandlerBehavior:
    """Behavior tests for PrecomputedCacheShutdownHandler."""

    @patch(
        "baldur.services.precomputed_cache.worker.stop_precomputed_cache",
        autospec=True,
    )
    def test_on_shutdown_start_calls_stop_precomputed_cache(self, mock_stop):
        """on_shutdown_start() calls stop_precomputed_cache()."""
        from baldur.services.precomputed_cache.shutdown import (
            PrecomputedCacheShutdownHandler,
        )

        handler = PrecomputedCacheShutdownHandler()
        handler.on_shutdown_start()

        mock_stop.assert_called_once()

    def test_on_drain_complete_does_not_raise(self):
        """on_drain_complete() does not raise (noop)."""
        from baldur.services.precomputed_cache.shutdown import (
            PrecomputedCacheShutdownHandler,
        )

        handler = PrecomputedCacheShutdownHandler()
        handler.on_drain_complete()  # Should not raise

    def test_on_shutdown_start_handles_import_error(self):
        """on_shutdown_start() gracefully handles exceptions from worker import."""
        from baldur.services.precomputed_cache.shutdown import (
            PrecomputedCacheShutdownHandler,
        )

        handler = PrecomputedCacheShutdownHandler()
        with patch(
            "baldur.services.precomputed_cache.worker.stop_precomputed_cache",
            autospec=True,
            side_effect=ImportError("not available"),
        ):
            handler.on_shutdown_start()  # Should not raise
