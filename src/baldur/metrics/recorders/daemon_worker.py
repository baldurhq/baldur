"""DaemonWorkerMetricRecorder — scrape-time metrics for daemon worker singletons.

Slot-for-slot mirror of ``baldur/metrics/recorders/executor.py``. Where the
executor recorder observes ``ThreadPoolExecutor`` singletons, this recorder
observes the ~40 long-lived ``threading.Thread(daemon=True)`` workers that
register a ``DaemonWorkerHandle`` via ``register_daemon_worker(name, handle)``.

Metrics emitted (all labelled by ``name``):

- ``baldur_daemon_worker_alive`` Gauge — 0/1, derived from
  ``handle.thread.is_alive()`` at scrape time.
- ``baldur_daemon_worker_last_heartbeat_age_seconds`` Gauge — universal
  staleness signal (covers tick-lag for pollers AND liveness for buffer
  workers).
- ``baldur_daemon_worker_processing_delay_seconds`` Gauge — buffer-only;
  current snapshot of the buffer's enqueue→pop residency time, supplied
  by ``handle.processing_delay_provider``.
- ``baldur_daemon_worker_iteration_duration_seconds`` Histogram — universal
  sleep-excluded execution time per loop iteration. Detects gradual
  slowdown before the staleness threshold fires. Updated by the worker
  via ``handle.observe_iteration(d)``; the recorder injects the per-name
  ``observe`` callable into ``handle._iteration_duration_observer`` at
  ``register_daemon_worker`` time so the handle module stays decoupled
  from ``prometheus_client``.
- ``baldur_daemon_worker_restarts_total`` Counter — lifetime monotonic
  per Prometheus convention. Incremented by the respawn coordinator on
  every successful restart, even when the handle's resettable
  ``restart_count`` field has been reset by the sustained-health gate.

Per impl 489 D2 + D8.
"""

from __future__ import annotations

import threading
import time
from typing import cast

import structlog

from baldur.meta.daemon_worker import DaemonWorkerHandle

logger = structlog.get_logger()

__all__ = [
    "DaemonWorkerMetricRecorder",
    "register_daemon_worker",
    "unregister_daemon_worker",
    "get_registered_daemon_workers",
]

try:
    from prometheus_client import REGISTRY, Counter, Histogram
    from prometheus_client.core import GaugeMetricFamily

    PROMETHEUS_AVAILABLE = True
except ImportError:
    PROMETHEUS_AVAILABLE = False
    REGISTRY = None  # type: ignore[assignment]
    Counter = None  # type: ignore[assignment,misc]
    Histogram = None  # type: ignore[assignment,misc]
    GaugeMetricFamily = None  # type: ignore[assignment,misc]


# Module-level registry of named daemon worker handles. Maps the handle's
# registration name (used as the metric label) to the DaemonWorkerHandle.
# Reads inside ``collect()`` snapshot the dict via ``list(...)`` so a
# concurrent register/unregister does not mutate the iterator mid-collection.
_handle_registry: dict[str, DaemonWorkerHandle] = {}
_registry_lock = threading.Lock()

# Collector singleton — registered with prometheus REGISTRY exactly once
# per process. Tracked as a module-level slot so reset_metrics() / repeat
# BaldurMetrics() construction does not re-register the same collector
# (which prometheus_client would reject with ValueError).
_collector_registered = False

# Persistent metric instances — created once per process so labels survive
# DaemonWorkerMetricRecorder reconstruction across reset_metrics().
_iteration_histogram = None
_restarts_counter = None


def _ensure_persistent_metrics() -> None:
    global _iteration_histogram, _restarts_counter
    if not PROMETHEUS_AVAILABLE:
        return
    if _iteration_histogram is None:
        try:
            _iteration_histogram = Histogram(
                "baldur_daemon_worker_iteration_duration_seconds",
                "Sleep-excluded execution time per loop iteration",
                ["name"],
            )
        except ValueError:
            # Already registered in this process (test-mode reload).
            from prometheus_client import REGISTRY as _R

            for collector in list(_R._collector_to_names.keys()):
                if getattr(collector, "_name", None) == (
                    "baldur_daemon_worker_iteration_duration_seconds"
                ):
                    _iteration_histogram = cast("Histogram", collector)
                    break
    if _restarts_counter is None:
        try:
            _restarts_counter = Counter(
                "baldur_daemon_worker_restarts_total",
                "Total daemon worker thread restarts (lifetime monotonic)",
                ["name"],
            )
        except ValueError:
            from prometheus_client import REGISTRY as _R

            for collector in list(_R._collector_to_names.keys()):
                if getattr(collector, "_name", None) == (
                    "baldur_daemon_worker_restarts_total"
                ):
                    _restarts_counter = cast("Counter", collector)
                    break


def register_daemon_worker(name: str, handle: DaemonWorkerHandle) -> None:
    """Register ``handle`` under ``name`` for scrape-time metric collection.

    Idempotent: repeat registration of the same name silently replaces the
    slot, so a worker that respawns or restarts can re-register against the
    same label without raising. Safe to call when ``prometheus_client`` is
    unavailable — the registry is maintained even if no metrics emit.

    Side effect: injects ``_iteration_duration_observer`` onto the handle so
    ``handle.observe_iteration(d)`` flows through the per-name histogram
    label binding without coupling the handle module to ``prometheus_client``.
    """
    _ensure_persistent_metrics()
    with _registry_lock:
        _handle_registry[name] = handle
    if PROMETHEUS_AVAILABLE and _iteration_histogram is not None:
        handle._iteration_duration_observer = _iteration_histogram.labels(
            name=name
        ).observe


def unregister_daemon_worker(name: str) -> None:
    """Remove ``name`` from the metric registry, no-op if absent."""
    with _registry_lock:
        _handle_registry.pop(name, None)


def get_registered_daemon_workers() -> dict[str, DaemonWorkerHandle]:
    """Snapshot the handle registry for diagnostics or test assertions."""
    with _registry_lock:
        return dict(_handle_registry)


def record_daemon_worker_restart(name: str) -> None:
    """Increment the lifetime restart counter for ``name``.

    Called by the respawn coordinator (``DaemonWorkerProbe`` in
    ``meta/health_probe.py``) on every successful restart.
    """
    _ensure_persistent_metrics()
    if PROMETHEUS_AVAILABLE and _restarts_counter is not None:
        _restarts_counter.labels(name=name).inc()


class _DaemonWorkerCollector:
    """Custom prometheus collector — yields gauges per registered handle."""

    def collect(self):  # type: ignore[no-untyped-def]
        if not PROMETHEUS_AVAILABLE:
            return

        alive_gauge = GaugeMetricFamily(
            "baldur_daemon_worker_alive",
            "Whether the daemon worker thread is alive (1=alive, 0=dead)",
            labels=["name"],
        )
        heartbeat_age_gauge = GaugeMetricFamily(
            "baldur_daemon_worker_last_heartbeat_age_seconds",
            "Seconds since the worker last called handle.heartbeat()",
            labels=["name"],
        )
        processing_delay_gauge = GaugeMetricFamily(
            "baldur_daemon_worker_processing_delay_seconds",
            "Buffer enqueue→pop residency time (buffer-backed workers only)",
            labels=["name"],
        )

        with _registry_lock:
            snapshot = list(_handle_registry.items())

        now = time.monotonic()
        for name, handle in snapshot:
            try:
                alive = 1.0 if handle.thread.is_alive() else 0.0
            except Exception:
                alive = 0.0
            try:
                age = max(0.0, now - handle.last_heartbeat_at)
            except Exception:
                age = 0.0

            alive_gauge.add_metric([name], alive)
            heartbeat_age_gauge.add_metric([name], age)

            if handle.processing_delay_provider is not None:
                try:
                    delay = float(handle.processing_delay_provider())
                except Exception:
                    delay = 0.0
                processing_delay_gauge.add_metric([name], delay)

        yield alive_gauge
        yield heartbeat_age_gauge
        yield processing_delay_gauge


class DaemonWorkerMetricRecorder:
    """Daemon worker observability — registers a scrape-time collector.

    Mirrors the ``ExecutorMetricRecorder`` slot pattern used in
    ``BaldurMetrics``: instantiated once, exposed as
    ``metrics.daemon_workers``. Construction registers the custom
    ``_DaemonWorkerCollector`` with the process-wide prometheus
    ``REGISTRY`` exactly once (re-construction after ``reset_metrics()``
    is a no-op so prometheus_client does not raise on double-register).

    When ``prometheus_client`` is unavailable, instantiation succeeds but
    no metrics are exported.
    """

    def __init__(self) -> None:
        global _collector_registered
        if not PROMETHEUS_AVAILABLE:
            return
        _ensure_persistent_metrics()
        if _collector_registered:
            return
        try:
            REGISTRY.register(_DaemonWorkerCollector())
            _collector_registered = True
        except Exception as e:
            logger.debug("metrics.daemon_worker_collector_register_failed", error=e)
