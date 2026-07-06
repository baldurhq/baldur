"""ExecutorMetricRecorder — scrape-time gauges for ThreadPoolExecutor singletons.

Exposes three Prometheus gauges scoped by ``name`` label so the two
process-shared ``ThreadPoolExecutor`` DCL singletons in baldur
(``TimeoutPolicy._executor``, ``BaldurEventBus._executor``) are observable
without per-action instrumentation:

- ``baldur_executor_queue_size{name}`` — pending tasks (``_work_queue.qsize()``)
- ``baldur_executor_active_threads{name}`` — live worker thread count (``len(_threads)``)
- ``baldur_executor_max_workers{name}`` — configured ceiling (``_max_workers``)

Implementation: scrape-time custom ``Collector`` reading from a module-level
``dict[str, ThreadPoolExecutor]`` registry — no background polling thread.
Private-attribute access (``_work_queue``, ``_threads``) follows the
established precedent at ``baldur/audit/resilient_recorder.py``.

Registration ergonomics: both DCL classmethods
(``TimeoutPolicy._get_executor`` / ``BaldurEventBus._get_executor``) call
``register_executor(name, executor)`` immediately after the
``ThreadPoolExecutor`` constructor inside the lock; the matching
``shutdown_*`` classmethods call ``unregister_executor(name)`` after
``shutdown(wait=True)``. Both helpers are idempotent — repeat registration
under the same name silently replaces the slot, and unregister of an
absent name is a no-op so the dual-invocation paths in
``reset_protect_caches()`` / ``reset_event_bus_settings()`` are harmless.

Per impl 487 D11/D12.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import structlog

logger = structlog.get_logger()

__all__ = [
    "ExecutorMetricRecorder",
    "register_executor",
    "unregister_executor",
    "get_registered_executors",
]

try:
    from prometheus_client import REGISTRY
    from prometheus_client.core import GaugeMetricFamily

    PROMETHEUS_AVAILABLE = True
except ImportError:
    PROMETHEUS_AVAILABLE = False
    REGISTRY = None  # type: ignore[assignment]
    GaugeMetricFamily = None  # type: ignore[assignment,misc]


# Module-level registry of named executors. Maps the ``thread_name_prefix``
# (used as the metric label) to the executor instance. Reads inside
# ``collect()`` snapshot the dict via ``list(...)`` so a concurrent
# register/unregister does not mutate the iterator mid-collection.
_executor_registry: dict[str, ThreadPoolExecutor] = {}
_registry_lock = threading.Lock()

# Collector singleton — registered with prometheus REGISTRY exactly once
# per process. Tracked as a module-level slot so reset_metrics() / repeat
# BaldurMetrics() construction does not re-register the same collector
# (which prometheus_client would reject with ValueError).
_collector_registered = False


def register_executor(name: str, executor: ThreadPoolExecutor) -> None:
    """Register ``executor`` under ``name`` for scrape-time metric collection.

    Idempotent: repeat registration of the same name silently replaces
    the slot, so a DCL classmethod that constructs a new executor after
    a prior shutdown can re-register against the same label without
    raising. Safe to call when ``prometheus_client`` is unavailable —
    the registry is maintained even if no collector emits from it.
    """
    with _registry_lock:
        _executor_registry[name] = executor


def unregister_executor(name: str) -> None:
    """Remove ``name`` from the metric registry, no-op if absent.

    The no-op-on-absent contract makes the dual-invocation reset paths
    (``reset_protect_caches()`` and ``reset_event_bus_settings()`` both
    drain the EventBus dispatch executor in 487 D3) safe — the second
    call sees an already-cleared slot and silently returns.
    """
    with _registry_lock:
        _executor_registry.pop(name, None)


def get_registered_executors() -> dict[str, ThreadPoolExecutor]:
    """Snapshot the executor registry for diagnostics or test assertions."""
    with _registry_lock:
        return dict(_executor_registry)


class _ExecutorCollector:
    """Custom prometheus collector — yields gauges per registered executor.

    Registered with the global ``REGISTRY`` once per process via
    ``ExecutorMetricRecorder.__init__``. ``collect()`` runs at scrape
    time, so values reflect the live state of each executor without any
    background polling thread.
    """

    def collect(self):  # type: ignore[no-untyped-def]
        if not PROMETHEUS_AVAILABLE:
            return

        queue_gauge = GaugeMetricFamily(
            "baldur_executor_queue_size",
            "Pending tasks in the executor's work queue",
            labels=["name"],
        )
        active_gauge = GaugeMetricFamily(
            "baldur_executor_active_threads",
            "Number of live worker threads in the executor pool",
            labels=["name"],
        )
        max_gauge = GaugeMetricFamily(
            "baldur_executor_max_workers",
            "Configured maximum worker count for the executor pool",
            labels=["name"],
        )

        with _registry_lock:
            snapshot = list(_executor_registry.items())

        for name, executor in snapshot:
            try:
                queue_size = executor._work_queue.qsize()
            except Exception:
                queue_size = 0
            try:
                active_threads = len(executor._threads)
            except Exception:
                active_threads = 0
            try:
                max_workers = int(executor._max_workers)
            except Exception:
                max_workers = 0

            queue_gauge.add_metric([name], queue_size)
            active_gauge.add_metric([name], active_threads)
            max_gauge.add_metric([name], max_workers)

        yield queue_gauge
        yield active_gauge
        yield max_gauge


class ExecutorMetricRecorder:
    """ThreadPoolExecutor pool observability — registers a scrape-time collector.

    Mirrors the slot pattern used by other recorders in
    ``BaldurMetrics``: instantiated once, exposed as ``metrics.executor``.
    Construction registers the custom ``_ExecutorCollector`` with the
    process-wide prometheus ``REGISTRY`` exactly once (re-construction
    after ``reset_metrics()`` is a no-op so prometheus_client does not
    raise on double-register).

    When ``prometheus_client`` is unavailable, instantiation succeeds but
    no metrics are exported — matches ``BaldurMetrics.__init__``'s early
    return for the same condition.
    """

    def __init__(self) -> None:
        global _collector_registered
        if not PROMETHEUS_AVAILABLE:
            return
        if _collector_registered:
            return
        try:
            REGISTRY.register(_ExecutorCollector())
            _collector_registered = True
        except Exception as e:
            logger.debug("metrics.executor_collector_register_failed", error=e)
