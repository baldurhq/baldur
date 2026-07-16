"""
Bulkhead Metrics - poll-based updater + reject-path counter wiring.

The Prometheus series themselves are defined once by the
``BulkheadMetricRecorder`` (``baldur.metrics.recorders.bulkhead``) and
instantiated by the metrics facade, so this module does not redefine them
— it only *writes* through that single source of truth. ``update_bulkhead_metrics``
is the poll path (driven by ``BulkheadMetricsUpdater`` below) and
``increment_rejected_count`` is the event path (called at each bulkhead reject
site).

Metrics (owned by ``BulkheadMetricRecorder``):
- baldur_bulkhead_active_count: current number of active requests
- baldur_bulkhead_max_concurrent: maximum concurrent capacity
- baldur_bulkhead_rejected_total: total rejected requests
- baldur_bulkhead_utilization_percent: utilization (%)
- baldur_bulkhead_waiting_count: number of waiting requests
"""

from __future__ import annotations

import threading
import time

import structlog

logger = structlog.get_logger()

__all__ = [
    "BulkheadMetricsUpdater",
    "get_metrics_updater",
    "increment_rejected_count",
    "reset_bulkhead_metrics",
    "start_metrics_updater",
    "stop_metrics_updater",
    "update_bulkhead_metrics",
]


# =============================================================================
# Metric write helpers — delegate to the BulkheadMetricRecorder
# =============================================================================


def _recorder():
    """Resolve the bulkhead recorder off the active metrics backend.

    Returns ``None`` when prometheus_client is unavailable (the facade skips
    recorder construction) or the OTEL meter is unavailable — callers no-op,
    preserving the fail-soft semantics the daemon relies on.
    """
    try:
        from baldur.metrics.prometheus import get_metrics

        return getattr(get_metrics(), "bulkhead", None)
    except Exception:
        return None


def update_bulkhead_metrics(
    bulkhead_name: str,
    bulkhead_type: str,
    active_count: int,
    max_concurrent: int,
    waiting_count: int,
) -> None:
    """
    Update bulkhead state gauges (poll path).

    Args:
        bulkhead_name: bulkhead name
        bulkhead_type: bulkhead type (semaphore, thread_pool)
        active_count: current number of active requests
        max_concurrent: maximum concurrent capacity
        waiting_count: number of waiting requests
    """
    rec = _recorder()
    if rec is not None:
        rec.update_metrics(
            bulkhead_name=bulkhead_name,
            bulkhead_type=bulkhead_type,
            active_count=active_count,
            max_concurrent=max_concurrent,
            waiting_count=waiting_count,
        )


def increment_rejected_count(bulkhead_name: str) -> None:
    """
    Increment the rejected counter (event path).

    Args:
        bulkhead_name: bulkhead name
    """
    rec = _recorder()
    if rec is not None:
        rec.increment_rejected(bulkhead_name)


class BulkheadMetricsUpdater:
    """
    Background thread that periodically refreshes every bulkhead's metrics.

    Pulls the current state of every registered bulkhead and writes it into
    the Prometheus metrics on a schedule.

    Usage:
        updater = BulkheadMetricsUpdater(interval=10.0)
        updater.start()
        # ... application runs ...
        updater.stop()
    """

    def __init__(self, interval: float = 10.0):
        """
        Args:
            interval: metric update interval (seconds)
        """
        self._interval = interval
        self._running = False
        self._thread: threading.Thread | None = None
        self._handle = None  # DaemonWorkerHandle (impl 489 D9)
        self._stop_event = threading.Event()

    def start(self) -> None:
        """Start the metrics updater."""
        from baldur.meta.daemon_worker import DaemonWorkerHandle
        from baldur.metrics.recorders.daemon_worker import register_daemon_worker

        if self._running:
            return

        self._stop_event.clear()
        self._running = True
        self._spawn_thread()
        self._handle = DaemonWorkerHandle(
            thread=self._thread,
            tick_interval_seconds=self._interval,
            restart_callback=self._spawn_thread,
        )
        register_daemon_worker("bulkhead_metrics_updater", self._handle)
        logger.info(
            "bulkhead_metrics_updater.started",
            interval=self._interval,
        )

    def _spawn_thread(self) -> None:
        """Construct + start a fresh updater thread."""
        self._thread = threading.Thread(
            target=self._update_loop_with_crash_capture,
            name="bulkhead_metrics_updater",
            daemon=True,
        )
        self._thread.start()
        if self._handle is not None:
            self._handle.thread = self._thread

    def _update_loop_with_crash_capture(self) -> None:
        try:
            self._update_loop()
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException as e:
            if self._handle is not None:
                self._handle.record_crash(e)
            raise

    def stop(self) -> None:
        """Stop the metrics updater."""
        from baldur.metrics.recorders.daemon_worker import unregister_daemon_worker

        if self._handle is not None:
            self._handle.is_stopping = True
        self._running = False
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=self._interval + 1)
            unregister_daemon_worker("bulkhead_metrics_updater")
            if self._thread.is_alive():
                logger.critical(
                    "daemon_worker.stop_join_timeout",
                    worker_name="bulkhead_metrics_updater",
                    join_timeout_seconds=self._interval + 1,
                )
        else:
            unregister_daemon_worker("bulkhead_metrics_updater")
        logger.info("bulkhead_metrics_updater.stopped")

    def _update_loop(self) -> None:
        """Metric update loop."""
        while self._running:
            iter_start = time.monotonic()
            try:
                self._update_all_metrics()
            except Exception as e:
                logger.warning(
                    "bulkhead_metrics_updater.update_failed",
                    error=e,
                )

            if self._handle is not None:
                self._handle.observe_iteration(time.monotonic() - iter_start)
                self._handle.heartbeat()

            # Event.wait sleeps for the interval but returns immediately
            # when stop() sets the event — graceful shutdown takes ms, not
            # up to self._interval seconds.
            if self._stop_event.wait(timeout=self._interval):
                break

    def _update_all_metrics(self) -> None:
        """Update metrics for every bulkhead.

        Resolves the registry through the chain getter, so a populated
        provider slot (a richer registry implementation, e.g. with per-tier
        compartments) is observed transparently.
        """
        try:
            from baldur.services.bulkhead.registry import get_bulkhead_registry

            registry = get_bulkhead_registry()
            states = registry.get_all_states()

            for name, state in states.items():
                update_bulkhead_metrics(
                    bulkhead_name=name,
                    bulkhead_type=state.bulkhead_type.value,
                    active_count=state.active_count,
                    max_concurrent=state.max_concurrent,
                    waiting_count=state.waiting_count,
                )

        except Exception as e:
            logger.debug(
                "bulkhead_metrics_updater.update_failed",
                error=e,
            )


# =============================================================================
# Singleton
# =============================================================================

_updater: BulkheadMetricsUpdater | None = None
_updater_lock = threading.Lock()


def get_metrics_updater(interval: float = 10.0) -> BulkheadMetricsUpdater:
    """Return the BulkheadMetricsUpdater singleton."""
    global _updater
    if _updater is None:
        with _updater_lock:
            if _updater is None:
                _updater = BulkheadMetricsUpdater(interval=interval)
    return _updater


def start_metrics_updater(interval: float = 10.0) -> BulkheadMetricsUpdater:
    """Start the metrics updater (convenience function)."""
    updater = get_metrics_updater(interval)
    updater.start()
    return updater


def stop_metrics_updater() -> None:
    """Stop the metrics updater (convenience function)."""
    global _updater
    if _updater is not None:
        _updater.stop()


def reset_bulkhead_metrics() -> None:
    """Reset the updater singleton (for testing).

    The Prometheus series are owned by ``BulkheadMetricRecorder`` and reset via
    the metrics-facade singleton (``reset_metrics``); only the daemon singleton
    needs tearing down here.
    """
    global _updater

    with _updater_lock:
        if _updater is not None:
            _updater.stop()
            _updater = None
