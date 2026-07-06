"""Scaling daemon thread shutdown handlers.

Ensures RateController and HPAMetricsExporter daemon threads are stopped
during centralized shutdown coordination. Per-service separation enables
independent drain reporting — if one thread hangs, the other's drain
status is reported correctly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from baldur.core.shutdown_coordinator import ShutdownHandler, TrackedRequest

if TYPE_CHECKING:
    from baldur.scaling.hpa_exporter import HPAMetricsExporter
    from baldur.scaling.rate_controller import RateController

logger = structlog.get_logger()

__all__ = [
    "RateControllerShutdownHandler",
    "HPAExporterShutdownHandler",
    "integrate_rate_controller_with_shutdown_coordinator",
    "integrate_hpa_exporter_with_shutdown_coordinator",
]


class RateControllerShutdownHandler(ShutdownHandler):
    """RateController daemon thread shutdown handler."""

    def __init__(self, controller: RateController) -> None:
        self._controller = controller

    def on_shutdown_start(self) -> None:
        self._controller.stop()

    def is_drain_complete(self) -> bool:
        worker = self._controller._worker
        if worker is None or not worker.is_alive():
            return True
        worker.join(timeout=0.1)
        return not worker.is_alive()

    def on_drain_complete(self) -> None:
        pass

    def on_force_shutdown(self, pending_requests: list[TrackedRequest]) -> None:
        self._controller.stop()


class HPAExporterShutdownHandler(ShutdownHandler):
    """HPAMetricsExporter daemon thread shutdown handler."""

    def __init__(self, exporter: HPAMetricsExporter) -> None:
        self._exporter = exporter

    def on_shutdown_start(self) -> None:
        self._exporter.stop()

    def is_drain_complete(self) -> bool:
        worker = self._exporter._worker
        if worker is None or not worker.is_alive():
            return True
        worker.join(timeout=0.1)
        return not worker.is_alive()

    def on_drain_complete(self) -> None:
        pass

    def on_force_shutdown(self, pending_requests: list[TrackedRequest]) -> None:
        self._exporter.stop()


def integrate_rate_controller_with_shutdown_coordinator() -> (
    RateControllerShutdownHandler | None
):
    """Create RateControllerShutdownHandler for external registration."""
    try:
        from baldur.scaling.rate_controller import get_rate_controller

        return RateControllerShutdownHandler(get_rate_controller())
    except Exception as e:
        logger.debug(
            "scaling.rate_controller_shutdown_handler_creation_skipped", error=e
        )
        return None


def integrate_hpa_exporter_with_shutdown_coordinator() -> (
    HPAExporterShutdownHandler | None
):
    """Create HPAExporterShutdownHandler for external registration."""
    try:
        from baldur.scaling.hpa_exporter import get_hpa_metrics_exporter

        return HPAExporterShutdownHandler(get_hpa_metrics_exporter())
    except Exception as e:
        logger.debug("scaling.hpa_exporter_shutdown_handler_creation_skipped", error=e)
        return None
