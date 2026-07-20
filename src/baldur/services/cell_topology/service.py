"""
Cell Topology Service — Orchestration Layer.

Orchestrator that unifies CellRegistry, CellEvacuationPolicy, EventBus and
anti-entropy behind a single entry point.

Follows the CircuitMeshService pattern.

Startup sequence (doc 388):
    CellTopologyService.start()
    ├── get_cell_registry() + get_cell_evacuation_policy()
    ├── register_cell_handlers(registry)        ← EventBus subscription
    ├── registry._load_all_states_from_redis()  ← L2 → L1 restore
    ├── _start_anti_entropy_loop()              ← daemon thread, all workers
    ├── _start_health_scheduling()              ← try/except isolated
    └── self._active = True
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from baldur.meta.daemon_worker import DaemonWorkerHandle
    from baldur.services.cell_topology.policy import CellEvacuationPolicy
    from baldur.services.cell_topology.registry import CellRegistry

logger = structlog.get_logger()

__all__ = [
    "CellTopologyService",
    "get_cell_topology_service",
    "reset_cell_topology_service",
]


class CellTopologyService:
    """
    Cell Topology orchestrator.

    Manages CellRegistry, EventBus handlers, the anti-entropy daemon and health
    scheduling behind the single start()/stop() entry point.
    """

    def __init__(self) -> None:
        from baldur.settings.cell_topology import get_cell_topology_settings

        self._settings = get_cell_topology_settings()
        self._active = False
        self._registry: CellRegistry | None = None
        self._policy: CellEvacuationPolicy | None = None
        self._stop_event = threading.Event()
        self._anti_entropy_thread: threading.Thread | None = None
        self._handle: DaemonWorkerHandle | None = None  # impl 489 D9

    def start(self) -> None:
        """
        Start the Cell Topology service.

        No hydration guard needed — LWW comparison is order-independent
        and idempotent (Q17).
        """
        if not self._settings.enabled:
            logger.info("cell_topology_service.disabled")
            return

        if self._active:
            logger.debug("cell_topology_service.already_active")
            return

        from baldur.services.cell_topology.policy import (
            get_cell_evacuation_policy,
        )
        from baldur.services.cell_topology.registry import (
            get_cell_registry,
            register_cell_handlers,
        )

        self._registry = get_cell_registry()
        self._policy = get_cell_evacuation_policy()
        assert self._registry is not None  # just assigned

        # EventBus subscription (Q5 — subscriber side of 382)
        register_cell_handlers(self._registry)

        # L2 → L1 hydration (initial sync)
        hydrated = self._registry._load_all_states_from_redis()
        logger.info(
            "cell_topology_service.hydrated",
            synced_cells=hydrated,
        )

        # Anti-entropy daemon thread (Q8 — all workers)
        self._start_anti_entropy_loop()

        # Health scheduling — fault-isolated (Q4)
        self._start_health_scheduling()

        self._active = True
        logger.info("cell_topology_service.started")

    def stop(self) -> None:
        """
        Stop the Cell Topology service.

        EventBus unregister + anti-entropy thread shutdown.
        L2(Redis) is preserved for other workers (Q10).
        """
        if not self._active:
            return

        if self._registry is not None:
            from baldur.services.cell_topology.registry import (
                unregister_cell_handlers,
            )

            unregister_cell_handlers(self._registry)

        # Stop anti-entropy daemon thread
        from baldur.metrics.recorders.daemon_worker import unregister_daemon_worker
        from baldur.settings.thread_management import (
            get_thread_management_settings,
        )

        if self._handle is not None:
            self._handle.is_stopping = True
        self._stop_event.set()
        timeout = get_thread_management_settings().join_timeout
        if self._anti_entropy_thread is not None:
            self._anti_entropy_thread.join(timeout=timeout)
            unregister_daemon_worker("cell-topology-anti-entropy")
            if self._anti_entropy_thread.is_alive():
                logger.critical(
                    "daemon_worker.stop_join_timeout",
                    worker_name="cell-topology-anti-entropy",
                    join_timeout_seconds=timeout,
                )
            self._anti_entropy_thread = None

        self._active = False
        logger.info("cell_topology_service.stopped")

    @property
    def active(self) -> bool:
        """Whether the service is currently running."""
        return self._active

    # ── Anti-entropy daemon (Q8) ──────────────────────────────

    def _start_anti_entropy_loop(self) -> None:
        """Start anti-entropy reconciliation daemon thread."""
        from baldur.meta.daemon_worker import DaemonWorkerHandle
        from baldur.metrics.recorders.daemon_worker import register_daemon_worker

        self._stop_event.clear()
        self._spawn_anti_entropy_thread()
        assert self._anti_entropy_thread is not None  # spawn always sets non-None
        self._handle = DaemonWorkerHandle(
            thread=self._anti_entropy_thread,
            tick_interval_seconds=self._settings.reconciliation_interval_seconds,
            restart_callback=self._spawn_anti_entropy_thread,
        )
        register_daemon_worker("cell-topology-anti-entropy", self._handle)
        logger.info(
            "cell_topology_service.anti_entropy_started",
            interval_seconds=self._settings.reconciliation_interval_seconds,
        )

    def _spawn_anti_entropy_thread(self) -> None:
        """Construct + start a fresh anti-entropy thread (impl 489 D9)."""
        self._anti_entropy_thread = threading.Thread(
            target=self._anti_entropy_loop_with_crash_capture,
            name="cell-topology-anti-entropy",
            daemon=True,
        )
        self._anti_entropy_thread.start()
        if self._handle is not None:
            self._handle.thread = self._anti_entropy_thread

    def _anti_entropy_loop_with_crash_capture(self) -> None:
        try:
            self._anti_entropy_loop()
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException as e:
            if self._handle is not None:
                self._handle.record_crash(e)
            raise

    def _anti_entropy_loop(self) -> None:
        """Anti-entropy reconciliation loop."""
        import time as _time

        while not self._stop_event.is_set():
            iter_start = _time.monotonic()
            try:
                if self._registry is not None:
                    self._registry._load_all_states_from_redis()
            except Exception:
                logger.warning(
                    "cell_topology_service.anti_entropy_failed", exc_info=True
                )
            if self._handle is not None:
                self._handle.observe_iteration(_time.monotonic() - iter_start)
                self._handle.heartbeat()
            self._stop_event.wait(self._settings.reconciliation_interval_seconds)

    # ── Health scheduling delegation (Q4) ─────────────────────

    def _start_health_scheduling(self) -> None:
        """Delegate to setup_cell_health_scheduler() with fault isolation.

        Health scheduler failure logs a warning but does not prevent
        state sync from operating.
        """
        try:
            from baldur.services.cell_topology.health import (
                setup_cell_health_scheduler,
            )

            setup_cell_health_scheduler()
        except Exception:
            logger.warning(
                "cell_topology_service.health_scheduling_failed",
                exc_info=True,
            )


# =============================================================================
# Singleton (Q13 — module-level get_*/reset_* pattern)
# =============================================================================

_service: CellTopologyService | None = None
_service_lock = threading.Lock()


def get_cell_topology_service() -> CellTopologyService:
    """CellTopologyService singleton."""
    global _service
    if _service is None:
        with _service_lock:
            if _service is None:
                _service = CellTopologyService()
    return _service


def reset_cell_topology_service() -> None:
    """Reset singleton (for testing)."""
    global _service
    with _service_lock:
        if _service is not None and _service.active:
            _service.stop()
        _service = None
