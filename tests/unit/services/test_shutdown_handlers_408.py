"""
Shutdown Handler Unit Tests (408 — C9).

Test targets:
    - scaling.shutdown.RateControllerShutdownHandler
    - scaling.shutdown.HPAExporterShutdownHandler
    - meta.shutdown.WatchdogShutdownHandler
    - Factory integrate_*_with_shutdown_coordinator functions

Test Categories:
    A. Contract: ShutdownHandler interface compliance
    B. Behavior: stop() delegation, is_drain_complete() thread polling, factory creation

Reference:
    408
"""

from __future__ import annotations

import threading
from unittest.mock import MagicMock, patch

import pytest

# =============================================================================
# Fixtures
# =============================================================================


def _make_mock_with_worker(alive: bool = True):
    """Create a mock service with a _worker thread attribute."""
    mock = MagicMock()
    worker = MagicMock(spec=threading.Thread)
    worker.is_alive.return_value = alive
    mock._worker = worker
    return mock


# =============================================================================
# A. Contract Tests — Interface Compliance
# =============================================================================


class TestShutdownHandlerInterfaceContract:
    """C9: All three handlers implement the ShutdownHandler interface."""

    def test_rate_controller_handler_is_shutdown_handler(self):
        """RateControllerShutdownHandler is a ShutdownHandler."""
        from baldur.core.shutdown_coordinator import ShutdownHandler
        from baldur.scaling.shutdown import RateControllerShutdownHandler

        assert issubclass(RateControllerShutdownHandler, ShutdownHandler)

    def test_hpa_exporter_handler_is_shutdown_handler(self):
        """HPAExporterShutdownHandler is a ShutdownHandler."""
        from baldur.core.shutdown_coordinator import ShutdownHandler
        from baldur.scaling.shutdown import HPAExporterShutdownHandler

        assert issubclass(HPAExporterShutdownHandler, ShutdownHandler)

    def test_watchdog_handler_is_shutdown_handler(self):
        """WatchdogShutdownHandler is a ShutdownHandler."""
        from baldur.core.shutdown_coordinator import ShutdownHandler
        from baldur.meta.shutdown import WatchdogShutdownHandler

        assert issubclass(WatchdogShutdownHandler, ShutdownHandler)


# =============================================================================
# B. Behavior Tests — RateControllerShutdownHandler
# =============================================================================


class TestRateControllerShutdownHandlerBehavior:
    """C9: RateControllerShutdownHandler delegates to controller."""

    def test_on_shutdown_start_calls_stop(self):
        """on_shutdown_start delegates to controller.stop()."""
        from baldur.scaling.shutdown import RateControllerShutdownHandler

        controller = MagicMock()
        handler = RateControllerShutdownHandler(controller)

        handler.on_shutdown_start()

        controller.stop.assert_called_once()

    def test_on_force_shutdown_calls_stop(self):
        """on_force_shutdown delegates to controller.stop()."""
        from baldur.scaling.shutdown import RateControllerShutdownHandler

        controller = MagicMock()
        handler = RateControllerShutdownHandler(controller)

        handler.on_force_shutdown([])

        controller.stop.assert_called_once()

    def test_is_drain_complete_true_when_worker_not_alive(self):
        """is_drain_complete returns True when worker thread is dead."""
        from baldur.scaling.shutdown import RateControllerShutdownHandler

        controller = _make_mock_with_worker(alive=False)
        handler = RateControllerShutdownHandler(controller)

        assert handler.is_drain_complete() is True

    def test_is_drain_complete_false_when_worker_alive(self):
        """is_drain_complete returns False when worker thread is still alive."""
        from baldur.scaling.shutdown import RateControllerShutdownHandler

        controller = _make_mock_with_worker(alive=True)
        handler = RateControllerShutdownHandler(controller)

        assert handler.is_drain_complete() is False
        controller._worker.join.assert_called_once_with(timeout=0.1)

    def test_is_drain_complete_true_when_worker_is_none(self):
        """is_drain_complete returns True when _worker is None (never started)."""
        from baldur.scaling.shutdown import RateControllerShutdownHandler

        controller = MagicMock()
        controller._worker = None
        handler = RateControllerShutdownHandler(controller)

        assert handler.is_drain_complete() is True

    def test_on_drain_complete_is_noop(self):
        """on_drain_complete does nothing (no exception)."""
        from baldur.scaling.shutdown import RateControllerShutdownHandler

        handler = RateControllerShutdownHandler(MagicMock())
        handler.on_drain_complete()  # Should not raise


# =============================================================================
# C. Behavior Tests — HPAExporterShutdownHandler
# =============================================================================


class TestHPAExporterShutdownHandlerBehavior:
    """C9: HPAExporterShutdownHandler delegates to exporter."""

    def test_on_shutdown_start_calls_stop(self):
        """on_shutdown_start delegates to exporter.stop()."""
        from baldur.scaling.shutdown import HPAExporterShutdownHandler

        exporter = MagicMock()
        handler = HPAExporterShutdownHandler(exporter)

        handler.on_shutdown_start()

        exporter.stop.assert_called_once()

    def test_is_drain_complete_true_when_worker_not_alive(self):
        """is_drain_complete returns True when worker thread is dead."""
        from baldur.scaling.shutdown import HPAExporterShutdownHandler

        exporter = _make_mock_with_worker(alive=False)
        handler = HPAExporterShutdownHandler(exporter)

        assert handler.is_drain_complete() is True

    def test_is_drain_complete_false_when_worker_alive(self):
        """is_drain_complete returns False when worker thread is still alive."""
        from baldur.scaling.shutdown import HPAExporterShutdownHandler

        exporter = _make_mock_with_worker(alive=True)
        handler = HPAExporterShutdownHandler(exporter)

        assert handler.is_drain_complete() is False
        exporter._worker.join.assert_called_once_with(timeout=0.1)


# =============================================================================
# D. Behavior Tests — WatchdogShutdownHandler
# =============================================================================


class TestWatchdogShutdownHandlerBehavior:
    """C9: WatchdogShutdownHandler delegates to watchdog."""

    def test_on_shutdown_start_calls_stop(self):
        """on_shutdown_start delegates to watchdog.stop()."""
        from baldur.meta.shutdown import WatchdogShutdownHandler

        watchdog = MagicMock()
        handler = WatchdogShutdownHandler(watchdog)

        handler.on_shutdown_start()

        watchdog.stop.assert_called_once()

    def test_on_force_shutdown_calls_stop(self):
        """on_force_shutdown delegates to watchdog.stop()."""
        from baldur.meta.shutdown import WatchdogShutdownHandler

        watchdog = MagicMock()
        handler = WatchdogShutdownHandler(watchdog)

        handler.on_force_shutdown([])

        watchdog.stop.assert_called_once()

    def test_is_drain_complete_true_when_worker_not_alive(self):
        """is_drain_complete returns True when worker thread is dead."""
        from baldur.meta.shutdown import WatchdogShutdownHandler

        watchdog = _make_mock_with_worker(alive=False)
        handler = WatchdogShutdownHandler(watchdog)

        assert handler.is_drain_complete() is True

    def test_is_drain_complete_false_when_worker_alive(self):
        """is_drain_complete returns False when worker thread is still alive."""
        from baldur.meta.shutdown import WatchdogShutdownHandler

        watchdog = _make_mock_with_worker(alive=True)
        handler = WatchdogShutdownHandler(watchdog)

        assert handler.is_drain_complete() is False
        watchdog._worker.join.assert_called_once_with(timeout=0.1)


# =============================================================================
# E. Behavior Tests — Factory Functions
# =============================================================================


class TestShutdownFactoryFunctionsBehavior:
    """C9: Factory functions create handlers or return None on failure."""

    def test_rate_controller_factory_returns_handler(self):
        """integrate_rate_controller returns handler on success."""
        from baldur.scaling.shutdown import (
            RateControllerShutdownHandler,
            integrate_rate_controller_with_shutdown_coordinator,
        )

        mock_controller = MagicMock()
        with patch(
            "baldur.scaling.rate_controller.get_rate_controller",
            return_value=mock_controller,
        ):
            handler = integrate_rate_controller_with_shutdown_coordinator()

        assert isinstance(handler, RateControllerShutdownHandler)

    def test_rate_controller_factory_returns_none_on_error(self):
        """integrate_rate_controller returns None when get_rate_controller raises."""
        from baldur.scaling.shutdown import (
            integrate_rate_controller_with_shutdown_coordinator,
        )

        with patch(
            "baldur.scaling.rate_controller.get_rate_controller",
            side_effect=RuntimeError("test"),
        ):
            handler = integrate_rate_controller_with_shutdown_coordinator()

        assert handler is None

    def test_hpa_factory_returns_handler(self):
        """integrate_hpa_exporter returns handler on success."""
        from baldur.scaling.shutdown import (
            HPAExporterShutdownHandler,
            integrate_hpa_exporter_with_shutdown_coordinator,
        )

        mock_exporter = MagicMock()
        with patch(
            "baldur.scaling.hpa_exporter.get_hpa_metrics_exporter",
            return_value=mock_exporter,
        ):
            handler = integrate_hpa_exporter_with_shutdown_coordinator()

        assert isinstance(handler, HPAExporterShutdownHandler)

    def test_watchdog_factory_returns_handler(self):
        """integrate_with_shutdown_coordinator returns handler on success."""
        pytest.importorskip("baldur_pro")
        from baldur.meta.shutdown import (
            WatchdogShutdownHandler,
            integrate_with_shutdown_coordinator,
        )

        mock_watchdog = MagicMock()
        with patch(
            "baldur_pro.services.meta_watchdog.get_selfhealer_watchdog",
            return_value=mock_watchdog,
        ):
            handler = integrate_with_shutdown_coordinator()

        assert isinstance(handler, WatchdogShutdownHandler)

    def test_watchdog_factory_returns_none_on_error(self):
        """integrate_with_shutdown_coordinator returns None when getter raises."""
        pytest.importorskip("baldur_pro")
        from baldur.meta.shutdown import integrate_with_shutdown_coordinator

        with patch(
            "baldur_pro.services.meta_watchdog.get_selfhealer_watchdog",
            side_effect=RuntimeError("test"),
        ):
            handler = integrate_with_shutdown_coordinator()

        assert handler is None
