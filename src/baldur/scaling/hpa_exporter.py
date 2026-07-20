"""
HPA Metrics Exporter.

Exposes custom metrics for the Kubernetes HPA in Prometheus format.
A background thread refreshes the metrics periodically.

Main metrics:
- baldur_queue_depth: current queue depth
- baldur_processing_rate: processing rate (items/second)
- baldur_backpressure_level: backpressure level (0-4)
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import TYPE_CHECKING

import structlog

from baldur.scaling.config import (
    BackpressureLevel,
    BackpressureSettings,
    get_backpressure_settings,
)
from baldur.scaling.metrics import BackpressureMetrics, get_backpressure_metrics
from baldur.scaling.rate_controller import RateController, get_rate_controller

if TYPE_CHECKING:
    from baldur.meta.daemon_worker import (  # noqa: F401
        DaemonWorkerHandle,
    )

logger = structlog.get_logger()


# Backpressure level as an integer (for the Prometheus metric)
LEVEL_TO_INT: dict[BackpressureLevel, int] = {
    BackpressureLevel.NONE: 0,
    BackpressureLevel.LOW: 1,
    BackpressureLevel.MEDIUM: 2,
    BackpressureLevel.HIGH: 3,
    BackpressureLevel.CRITICAL: 4,
}


class HPAMetricsExporter:
    """
    HPA Metrics Exporter.

    Refreshes Prometheus metrics periodically in the background.
    The Kubernetes HPA uses these metrics to adjust the pod count.

    Usage:
        def get_queue_size() -> int:
            return redis.llen("my_queue")

        exporter = HPAMetricsExporter(queue_size_provider=get_queue_size)
        exporter.start()

        # On application shutdown
        exporter.stop()
    """

    DEFAULT_COMPONENT_NAME = "baldur"
    DEFAULT_QUEUE_NAME = "default"
    DEFAULT_UPDATE_INTERVAL = 5.0  # seconds

    def __init__(
        self,
        queue_size_provider: Callable[[], int] | None = None,
        rate_controller: RateController | None = None,
        metrics: BackpressureMetrics | None = None,
        settings: BackpressureSettings | None = None,
        component_name: str | None = None,
        queue_name: str | None = None,
        update_interval: float | None = None,
    ):
        """
        Args:
            queue_size_provider: Queue size lookup function
            rate_controller: RateController instance
            metrics: BackpressureMetrics instance
            settings: Backpressure settings
            component_name: Component name (metric label)
            queue_name: Queue name (metric label)
            update_interval: Metric refresh interval (seconds)
        """
        self._settings = settings or get_backpressure_settings()
        self._queue_size_provider = queue_size_provider or (lambda: 0)
        self._rate_controller = rate_controller or get_rate_controller()
        self._metrics = metrics or get_backpressure_metrics()
        self._component_name = component_name or self.DEFAULT_COMPONENT_NAME
        self._queue_name = queue_name or self.DEFAULT_QUEUE_NAME
        self._update_interval = update_interval or self.DEFAULT_UPDATE_INTERVAL

        self._running = False
        self._worker: threading.Thread | None = None
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._handle: DaemonWorkerHandle | None = None  # impl 489 D9

    def _update_metrics(self) -> None:
        """Refresh the Prometheus metrics."""
        try:
            # Queue depth
            queue_size = self._queue_size_provider()
            self._metrics.set_queue_depth(self._queue_name, queue_size)

            # Current state
            state = self._rate_controller.get_state()

            # Processing rate
            self._metrics.set_processing_rate(self._component_name, state.current_rate)

            # Backpressure level (converted to an integer)
            level_int = LEVEL_TO_INT.get(state.level, 0)
            self._metrics.set_backpressure_level(self._component_name, level_int)

            logger.debug(
                "hpa_metrics_exporter.updated",
                queue_size=queue_size,
                current_rate=state.current_rate,
                degradation_level=state.level.value,
            )

        except Exception as e:
            logger.exception(
                "hpa_metrics_exporter.update_error",
                error=e,
            )

    def _run_loop(self) -> None:
        """Metric refresh loop."""
        import time as _time

        while self._running:
            iter_start = _time.monotonic()
            self._update_metrics()
            if self._handle is not None:
                self._handle.observe_iteration(_time.monotonic() - iter_start)
                self._handle.heartbeat()
            self._stop_event.wait(self._update_interval)
            if self._stop_event.is_set():
                break

    def _run_loop_with_crash_capture(self) -> None:
        try:
            self._run_loop()
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException as e:
            if self._handle is not None:
                self._handle.record_crash(e)
            raise

    def start(self) -> None:
        """Start the exporter."""
        from baldur.meta.daemon_worker import DaemonWorkerHandle
        from baldur.metrics.recorders.daemon_worker import register_daemon_worker

        if not self._settings.hpa_enabled:
            logger.info("hpa_metrics_exporter.hpa_disabled")
            return

        if not self._settings.metrics_enabled:
            logger.info("hpa_metrics_exporter.metrics_disabled")
            return

        with self._lock:
            if self._running:
                return

            self._stop_event.clear()
            self._running = True
            self._spawn_worker_thread()
            assert self._worker is not None  # populated by _spawn_worker_thread
            self._handle = DaemonWorkerHandle(
                thread=self._worker,
                tick_interval_seconds=float(self._update_interval),
                restart_callback=self._spawn_worker_thread,
            )
            register_daemon_worker("HPAMetricsExporter", self._handle)
            logger.info("hpa_exporter.started")

    def _spawn_worker_thread(self) -> None:
        """Construct + start a fresh exporter thread (impl 489 D9)."""
        self._worker = threading.Thread(
            target=self._run_loop_with_crash_capture,
            name="HPAMetricsExporter",
            daemon=True,
        )
        self._worker.start()
        if self._handle is not None:
            self._handle.thread = self._worker

    def stop(self) -> None:
        """Stop the exporter."""
        from baldur.metrics.recorders.daemon_worker import unregister_daemon_worker

        with self._lock:
            if self._handle is not None:
                self._handle.is_stopping = True
            self._running = False
            self._stop_event.set()

        if self._worker:
            self._worker.join(timeout=2.0)
            unregister_daemon_worker("HPAMetricsExporter")
            if self._worker.is_alive():
                logger.critical(
                    "daemon_worker.stop_join_timeout",
                    worker_name="HPAMetricsExporter",
                    join_timeout_seconds=2.0,
                )
            self._worker = None

        logger.info("hpa_exporter.stopped")

    def is_running(self) -> bool:
        """Return whether the exporter is running."""
        return self._running

    def update_now(self) -> None:
        """Refresh the metrics immediately (for testing/debugging)."""
        self._update_metrics()


# =============================================================================
# Singleton
# =============================================================================

from baldur.utils.singleton import CLEANUP_STOP, make_singleton_factory

get_hpa_metrics_exporter, configure_hpa_metrics_exporter, reset_hpa_metrics_exporter = (
    make_singleton_factory(
        "hpa_metrics_exporter", HPAMetricsExporter, cleanup_fn=CLEANUP_STOP
    )
)
