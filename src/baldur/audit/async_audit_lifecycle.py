"""
Async Audit Lifecycle Manager - manages the lifecycle of the async audit pipeline.

Handles startup recovery and graceful shutdown so that no unprocessed events are
lost on process termination.

Features:
1. Recover unprocessed WAL entries on startup
2. Initialize AsyncHealingLogger and wire the audit callback
3. Flush all outstanding events on shutdown
4. SIGTERM/SIGINT signal handling

Usage (Django apps.py):
    from baldur.audit.async_audit_lifecycle import (
        startup_async_audit_system,
        register_shutdown_handlers,
    )

    class AuditConfig(AppConfig):
        def ready(self):
            startup_async_audit_system()
            register_shutdown_handlers()

Version: 1.0.0
"""

from __future__ import annotations

import os
import threading
import time
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    pass

logger = structlog.get_logger()

# 450 Phase 4: lifecycle / shutdown guards live on a runtime-scoped state
# object so resetting the active ``BaldurRuntime`` (or swapping it for a test
# fixture) drops the flags atomically.
_lifecycle_lock = threading.Lock()


class _AuditLifecycleState:
    """Mutable async-audit lifecycle flags owned by the active runtime."""

    __slots__ = ("audit_shutdown_done", "shutdown_registered", "startup_completed")

    def __init__(self) -> None:
        self.startup_completed: bool = False
        self.shutdown_registered: bool = False
        self.audit_shutdown_done: bool = False


def _lifecycle_state() -> _AuditLifecycleState:
    from baldur.runtime import get_runtime

    return get_runtime().get_singleton("audit_lifecycle_state", _AuditLifecycleState)


# ═══════════════════════════════════════════════════════════════════════════════
# Audit callback wiring (AsyncHealingLogger → AuditAdapter)
# ═══════════════════════════════════════════════════════════════════════════════


def create_audit_flush_callback():
    """
    Create the AsyncHealingLogger batch flush callback.

    Forwards batched events to the AuditAdapter, converting dicts to AuditEntry
    objects and calling log() or log_batch().
    """

    def flush_to_audit_adapter(events: list[dict[str, Any]]) -> None:
        """Forward a batch of events to the AuditAdapter."""
        if not events:
            return

        try:
            from baldur.adapters.audit.singleton import get_audit_adapter
            from baldur.interfaces.audit_adapter import AuditAction, AuditEntry

            adapter = get_audit_adapter()
            if adapter is None:
                logger.debug("async_audit_lifecycle.auditadapter_available")
                return

            entries = []
            for event_dict in events:
                try:
                    # action string → AuditAction enum
                    action_str = event_dict.get("action", "config_change")
                    try:
                        action = AuditAction(action_str)
                    except ValueError:
                        action = AuditAction.CONFIG_CHANGE

                    entry = AuditEntry(
                        action=action,
                        actor_id=event_dict.get("actor_id"),
                        actor_type=event_dict.get("actor_type", "system"),
                        target_type=event_dict.get("target_type"),
                        target_id=event_dict.get("target_id", ""),
                        domain=event_dict.get("domain"),
                        reason=event_dict.get("reason"),
                        details=event_dict.get("details", {}),
                        success=event_dict.get("success", True),
                        error_message=event_dict.get("error_message"),
                    )
                    entries.append(entry)
                except Exception as e:
                    logger.debug(
                        "async_audit_lifecycle.event_conversion_failed",
                        error=e,
                    )

            # Batch insert when supported
            if hasattr(adapter, "log_batch"):
                adapter.log_batch(entries)
            else:
                for entry in entries:
                    adapter.log(entry)

            logger.debug(
                "async_audit_lifecycle.flushed_audit_entries",
                entries_count=len(entries),
            )

        except Exception as e:
            logger.warning(
                "async_audit_lifecycle.flush_adapter_failed",
                error=e,
            )

    return flush_to_audit_adapter


# ═══════════════════════════════════════════════════════════════════════════════
# Startup recovery
# ═══════════════════════════════════════════════════════════════════════════════


def startup_async_audit_system() -> bool:
    """
    Start and recover the async audit system.

    Order:
    1. Load checkpoint (last processed sequence)
    2. Inspect WAL for unprocessed entries
    3. Initialize AsyncHealingLogger and wire the callback
    4. Start the SyncWorker

    Returns:
        True: started successfully
        False: failed to start, or already started
    """
    state = _lifecycle_state()

    with _lifecycle_lock:
        if state.startup_completed:
            logger.debug("async_audit_lifecycle.already_started")
            return False

        logger.info("async_audit_lifecycle.starting_async_audit_system")

        try:
            # 1. Load checkpoint
            last_seq = _load_checkpoint()
            logger.info(
                "async_audit_lifecycle.last_processed_sequence",
                last_seq=last_seq,
            )

            # 2. Inspect WAL for unprocessed entries
            unprocessed_count = _check_unprocessed_wal_entries(last_seq)
            if unprocessed_count > 0:
                logger.info(
                    "async_audit_lifecycle.found_unprocessed_wal_entries",
                    unprocessed_count=unprocessed_count,
                )

            # 3. Initialize and start AsyncHealingLogger
            _initialize_async_logger()

            # 4. Start the SyncWorker
            _start_sync_worker()

            state.startup_completed = True
            logger.info("async_audit_lifecycle.async_audit_system_started")
            return True

        except Exception as e:
            logger.exception(
                "async_audit_lifecycle.startup_failed",
                error=e,
            )
            return False


def _load_checkpoint() -> int:
    """Load the last processed sequence from the checkpoint store."""
    try:
        from baldur.audit.checkpoint import (
            get_default_checkpoint_strategy,
        )

        strategy = get_default_checkpoint_strategy()
        return strategy.get_wal_sequence("default")
    except Exception as e:
        logger.debug(
            "async_audit_lifecycle.checkpoint_load_failed",
            error=e,
        )
        return 0


def _check_unprocessed_wal_entries(last_seq: int) -> int:
    """
    Count unprocessed WAL entries (lazy recovery support).

    Prefers count_unprocessed() so we can determine the number of unprocessed
    entries without reading the full WAL file.
    """
    try:
        wal = _get_wal_instance()
        if wal is None:
            return 0

        # Prefer count_unprocessed() (lazy: no file reads)
        if hasattr(wal, "count_unprocessed"):
            return wal.count_unprocessed(last_processed_seq=last_seq)

        # Fallback: full read (legacy behavior)
        if hasattr(wal, "recover_unprocessed"):
            entries = wal.recover_unprocessed(last_processed_seq=last_seq)
            return len(entries) if entries else 0

        return 0
    except Exception as e:
        logger.debug(
            "async_audit_lifecycle.wal_check_failed",
            error=e,
        )
        return 0


def _initialize_async_logger() -> None:
    """Initialize AsyncHealingLogger and wire the flush callback."""
    try:
        from baldur.utils.async_logger import AsyncHealingLogger

        # Wire the audit flush callback
        flush_callback = create_audit_flush_callback()
        AsyncHealingLogger.configure(flush_callback=flush_callback)

        # Start the background worker
        AsyncHealingLogger.start()

        logger.info("async_audit_lifecycle.asynchealinglogger_initialized")
    except Exception as e:
        logger.warning(
            "async_audit_lifecycle.asynchealinglogger_init_failed",
            error=e,
        )


def _start_sync_worker() -> None:
    """Start the AuditSyncWorker."""
    try:
        from baldur.audit.sync_worker import AuditSyncWorker

        sync_worker = AuditSyncWorker.get_instance()
        # Absorb a crashed peer's orphan (non-own-PID) WAL entries once before
        # the steady runtime-partitioned drain begins (#470 D2).
        sync_worker.absorb_orphans()
        sync_worker.start()

        logger.info("async_audit_lifecycle.auditsyncworker_started")
    except Exception as e:
        logger.debug(
            "async_audit_lifecycle.syncworker_start_failed",
            error=e,
        )


# ═══════════════════════════════════════════════════════════════════════════════
# Graceful Shutdown
# ═══════════════════════════════════════════════════════════════════════════════


_audit_shutdown_lock = threading.Lock()


def graceful_shutdown_audit_system() -> (
    None
):  # verified-by: test_log_critical_event_immediate_flush
    """
    Graceful shutdown of the audit system.

    Order:
    1. Flush AsyncHealingLogger (memory → WAL/adapter)
    2. Wait for AuditSyncWorker termination (WAL → central storage)
    3. Final WAL flush (disk sync)
    4. Save checkpoint
    5. Flush and close DiskPersistentBuffer (drain-positioned teardown)

    Guarantees zero data loss.

    Thread-safe once-guard prevents double execution when both Gunicorn
    worker_exit hook and ShutdownCoordinator trigger shutdown concurrently.

    Skipped in test mode (BALDUR_TEST_MODE=true).
    """
    # Avoid touching real resources in test mode
    if os.getenv("BALDUR_TEST_MODE", "").lower() == "true":
        return

    state = _lifecycle_state()
    with _audit_shutdown_lock:
        if state.audit_shutdown_done:
            logger.debug("graceful_shutdown.already_completed")
            return
        state.audit_shutdown_done = True

    logger.info("graceful_shutdown.starting_audit_system_shutdown")

    # 1. Flush and stop AsyncHealingLogger
    _shutdown_async_logger()

    # 2. Stop AuditSyncWorker
    _shutdown_sync_worker()

    # 3. Flush and close WAL
    _shutdown_wal()

    # 4. Save final checkpoint
    _save_final_checkpoint()

    # 5. Flush and close the disk-persistent buffer — literal-final:
    #    the PRO WAL-failure fallback writes INTO this buffer, so it
    #    must outlive the WAL step; checkpoint never touches it.
    _shutdown_disk_buffer()

    logger.info("graceful_shutdown.audit_system_shutdown_complete")


def _reset_audit_shutdown_state() -> None:
    """Test-only: reset once-guard for test isolation."""
    _lifecycle_state().audit_shutdown_done = False


def _shutdown_async_logger() -> None:
    """Flush and stop AsyncHealingLogger."""
    try:
        from baldur.utils.async_logger import AsyncHealingLogger

        # Flush remaining events
        AsyncHealingLogger.flush()

        # Stop the worker (5 second timeout)
        AsyncHealingLogger.stop(timeout=5.0)

        logger.info("graceful_shutdown.asynchealinglogger_stopped")
    except Exception as e:
        from baldur.audit.resilience.metrics import get_audit_metrics

        get_audit_metrics().record_failure("async_logger", "shutdown_flush")
        logger.exception(
            "graceful_shutdown.asynchealinglogger_error",
            error=e,
        )


def _shutdown_sync_worker() -> None:
    """Stop AuditSyncWorker."""
    try:
        from baldur.audit.sync_worker import AuditSyncWorker

        sync_worker = AuditSyncWorker.get_instance()

        # Wait for sync to drain (30 second timeout)
        sync_worker.stop(timeout=30.0)

        logger.info("graceful_shutdown.auditsyncworker_stopped")
    except Exception as e:
        from baldur.audit.resilience.metrics import get_audit_metrics

        get_audit_metrics().record_failure("sync_worker", "shutdown_drain")
        logger.exception(
            "graceful_shutdown.syncworker_error",
            error=e,
        )


def _shutdown_wal() -> None:
    """Flush and close WAL."""
    try:
        wal = _get_wal_instance()
        if wal is None:
            return

        # Call flush() if available
        if hasattr(wal, "flush"):
            wal.flush()

        # Call close() if available
        if hasattr(wal, "close"):
            wal.close()

        logger.info("graceful_shutdown.wal_closed")
    except Exception as e:
        from baldur.audit.resilience.metrics import get_audit_metrics

        get_audit_metrics().record_failure("wal", "shutdown_close")
        logger.exception(
            "graceful_shutdown.wal_error",
            error=e,
        )


def _save_final_checkpoint() -> None:
    """Save the final checkpoint."""
    try:
        from baldur.audit.checkpoint import (
            UnifiedCheckpointData,
            get_default_checkpoint_strategy,
        )

        last_seq = _get_last_processed_sequence()

        if last_seq > 0:
            strategy = get_default_checkpoint_strategy()
            strategy.save("default", UnifiedCheckpointData(wal_sequence=last_seq))
            logger.info(
                "graceful_shutdown.checkpoint_saved",
                last_seq=last_seq,
            )
    except Exception as e:
        from baldur.audit.resilience.metrics import get_audit_metrics

        get_audit_metrics().record_failure("checkpoint", "shutdown_save")
        logger.exception(
            "graceful_shutdown.checkpoint_error",
            error=e,
        )


def _shutdown_disk_buffer() -> None:
    """Flush and close the DiskPersistentBuffer (drain-positioned teardown)."""
    started = time.monotonic()
    error: Exception | None = None
    try:
        # Module-qualified call: the signal-path module owns the
        # teardown implementation (flush + close + instance null).
        from baldur.audit.persistence import disk_buffer_shutdown

        success = disk_buffer_shutdown._shutdown_disk_buffer()
    except Exception as e:
        success = False
        error = e

    duration_ms = (time.monotonic() - started) * 1000

    if success:
        logger.info(
            "graceful_shutdown.disk_buffer_closed",
            duration_ms=duration_ms,
        )
        return

    from baldur.audit.resilience.metrics import get_audit_metrics

    get_audit_metrics().record_failure("disk_buffer", "shutdown_close")
    if error is not None:
        logger.error(
            "graceful_shutdown.disk_buffer_error",
            duration_ms=duration_ms,
            error=error,
        )
    else:
        logger.error(
            "graceful_shutdown.disk_buffer_error",
            duration_ms=duration_ms,
        )


def _get_last_processed_sequence() -> int:
    """Get the last processed sequence number."""
    try:
        from baldur.audit.sync_worker import AuditSyncWorker

        sync_worker = AuditSyncWorker.get_instance()
        return getattr(sync_worker, "_last_processed_seq", 0)
    except Exception:
        return 0


def _get_wal_instance():
    """Get the WAL instance."""
    try:
        from baldur_pro.services.audit import _get_wal

        return _get_wal()
    except Exception:
        return None


# ═══════════════════════════════════════════════════════════════════════════════
# Signal handler registration
# ═══════════════════════════════════════════════════════════════════════════════


def _is_test_mode() -> bool:
    """Return True when running in test mode."""
    return os.getenv("BALDUR_TEST_MODE", "").lower() == "true"


# 416 Part 5: register_shutdown_handlers() and its signal helpers
# (_register_signal_handler, _handle_sigterm, _handle_sigint) were
# deleted. They were superseded by AuditShutdownHandler +
# GracefulShutdownCoordinator (registered via apps.py:323) and the
# Gunicorn worker_exit_cleanup hook (server.py:165).


# ═══════════════════════════════════════════════════════════════════════════════
# Status query
# ═══════════════════════════════════════════════════════════════════════════════


def get_lifecycle_status() -> dict[str, Any]:
    """Return the current lifecycle status."""
    state = _lifecycle_state()
    return {
        "startup_completed": state.startup_completed,
        "shutdown_registered": state.shutdown_registered,
    }


def reset_lifecycle_state() -> None:
    """Reset lifecycle state (for tests)."""
    state = _lifecycle_state()
    with _lifecycle_lock:
        state.startup_completed = False
        state.shutdown_registered = False


# ═══════════════════════════════════════════════════════════════════════════════
# Monitoring metrics (6.3 monitoring)
# ═══════════════════════════════════════════════════════════════════════════════


def get_async_audit_metrics() -> dict[str, Any]:
    """
    Return async audit pipeline metrics.

    Returns AsyncHealingLogger statistics together with the current queue size,
    suitable for collection by Prometheus or other monitoring systems.

    Returns:
        dict: metric information
            - events_logged: total events logged
            - events_flushed: total events flushed
            - immediate_flushes: number of immediate flushes (CRITICAL events)
            - batch_flushes: number of batch flushes
            - flush_errors: number of flush errors
            - queue_size: current number of events queued
            - worker_running: whether the worker thread is running
    """
    try:
        from baldur.utils.async_logger import AsyncHealingLogger

        # Pull base statistics
        stats = AsyncHealingLogger.get_stats()

        # Append queue size
        queue_size = 0
        try:
            if AsyncHealingLogger._queue is not None:
                queue_size = AsyncHealingLogger._queue.qsize()
        except Exception:
            pass

        # Append worker state
        worker_running = AsyncHealingLogger._running

        state = _lifecycle_state()
        return {
            **stats,
            "queue_size": queue_size,
            "worker_running": worker_running,
            "lifecycle_startup_completed": state.startup_completed,
            "lifecycle_shutdown_registered": state.shutdown_registered,
        }

    except Exception as e:
        logger.warning(
            "async_audit_lifecycle.get_metrics_failed",
            error=e,
        )
        state = _lifecycle_state()
        return {
            "error": str(e),
            "lifecycle_startup_completed": state.startup_completed,
            "lifecycle_shutdown_registered": state.shutdown_registered,
        }


def export_metrics_to_prometheus() -> str:
    """
    Render metrics in Prometheus exposition format.

    Suitable for use as the response body of a /metrics endpoint.

    Returns:
        str: Prometheus text exposition format
    """
    metrics = get_async_audit_metrics()

    lines = [
        "# HELP async_audit_events_logged Total events logged to async logger",
        "# TYPE async_audit_events_logged counter",
        f"async_audit_events_logged {metrics.get('events_logged', 0)}",
        "",
        "# HELP async_audit_events_flushed Total events flushed to backend",
        "# TYPE async_audit_events_flushed counter",
        f"async_audit_events_flushed {metrics.get('events_flushed', 0)}",
        "",
        "# HELP async_audit_immediate_flushes Total immediate flushes (CRITICAL events)",
        "# TYPE async_audit_immediate_flushes counter",
        f"async_audit_immediate_flushes {metrics.get('immediate_flushes', 0)}",
        "",
        "# HELP async_audit_batch_flushes Total batch flushes",
        "# TYPE async_audit_batch_flushes counter",
        f"async_audit_batch_flushes {metrics.get('batch_flushes', 0)}",
        "",
        "# HELP async_audit_flush_errors Total flush errors",
        "# TYPE async_audit_flush_errors counter",
        f"async_audit_flush_errors {metrics.get('flush_errors', 0)}",
        "",
        "# HELP async_audit_queue_size Current queue size",
        "# TYPE async_audit_queue_size gauge",
        f"async_audit_queue_size {metrics.get('queue_size', 0)}",
        "",
        "# HELP async_audit_worker_running Worker thread running status",
        "# TYPE async_audit_worker_running gauge",
        f"async_audit_worker_running {1 if metrics.get('worker_running', False) else 0}",
    ]

    return "\n".join(lines)
