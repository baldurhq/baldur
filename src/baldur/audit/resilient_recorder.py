"""
Resilient Continuous Audit Recorder.

Adds fault-tolerance capabilities on top of ContinuousAuditRecorder:
- RingBuffer: non-intrusive shadow logging
- CircuitBreaker: reuses resilience.py
- Self-Audit: records its own state
- SyslogFallback: last resort

Design:
    Application → record() → RingBuffer → Background Worker → Storage
        (Non-blocking)    (Shadow)      (Async Flush)

    On failure:
    Primary Store → Fallback → Syslog → stderr

Usage:
    from baldur.audit.resilient_recorder import ResilientContinuousAuditRecorder

    recorder = ResilientContinuousAuditRecorder(
        audit_adapter=adapter,
        enable_background_flush=True,
    )

    # Non-blocking record
    recorder.record_auto_tuning(...)
"""

import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog

from baldur.interfaces.audit_adapter import AuditEntry, AuditLogAdapter
from baldur.settings import (
    ResilientRecorderSettings,
    get_resilient_recorder_settings,
)

from .checksum import compute_crc32
from .config import AuditConfig
from .continuous_audit import ContinuousAuditRecorder
from .resilience import (
    AuditCircuitBreakerConfig,
    AuditMetrics,
    CircuitBreakerRegistry,
    CircuitState,
    DegradedModeManager,
    SyslogFallback,
)
from .ring_buffer import BackpressureStrategy, RingBuffer, RingBufferStats
from .self_audit import SelfAuditEvent, self_audit

logger = structlog.get_logger()


@dataclass
class ResilientRecorderConfig:
    """Resilient Recorder configuration."""

    # Buffer
    buffer_capacity: int = 10000
    backpressure_strategy: BackpressureStrategy = BackpressureStrategy.DROP_OLDEST

    # Background Worker
    enable_background_flush: bool = True
    flush_interval_seconds: float = 1.0
    flush_batch_size: int = 100

    # Circuit Breaker
    circuit_failure_threshold: int = 3
    circuit_success_threshold: int = 2
    circuit_timeout_seconds: float = 30.0
    circuit_call_timeout_seconds: float = 5.0

    # Fallback
    fallback_file_path: str | None = None
    enable_syslog_fallback: bool = True

    @classmethod
    def from_settings(
        cls, settings: ResilientRecorderSettings | None = None
    ) -> "ResilientRecorderConfig":
        """
        Build a Config from ResilientRecorderSettings.

        Args:
            settings: Pydantic Settings instance (defaults are used when None)

        Returns:
            ResilientRecorderConfig instance
        """
        s = settings or get_resilient_recorder_settings()

        # backpressure_strategy string -> enum conversion.
        # BLOCK is accepted by the settings validator but not implemented in the
        # BackpressureStrategy enum — falls back to DROP_OLDEST.
        strategy_map: dict[str, BackpressureStrategy] = {
            "DROP_OLDEST": BackpressureStrategy.DROP_OLDEST,
            "DROP_NEWEST": BackpressureStrategy.DROP_NEWEST,
            "BLOCK": BackpressureStrategy.DROP_OLDEST,
        }
        strategy = strategy_map.get(
            s.backpressure_strategy, BackpressureStrategy.DROP_OLDEST
        )

        return cls(
            buffer_capacity=s.buffer_capacity,
            backpressure_strategy=strategy,
            enable_background_flush=s.enable_background_flush,
            flush_interval_seconds=s.flush_interval_seconds,
            flush_batch_size=s.flush_batch_size,
            circuit_failure_threshold=s.circuit_failure_threshold,
            circuit_success_threshold=s.circuit_success_threshold,
            circuit_timeout_seconds=s.circuit_timeout_seconds,
            circuit_call_timeout_seconds=s.circuit_call_timeout_seconds,
            fallback_file_path=s.fallback_file_path,
            enable_syslog_fallback=s.enable_syslog_fallback,
        )


class ResilientContinuousAuditRecorder(ContinuousAuditRecorder):
    """
    Fault-tolerant continuous audit recorder.

    Extends ContinuousAuditRecorder with:
    1. RingBuffer: non-intrusive shadow logging (non-blocking)
    2. CircuitBreaker wiring: reuses resilience.py
    3. Self-Audit: records its own state
    4. Syslog wiring: last resort
    5. Background Flush: asynchronous batch processing

    Fallback Chain:
        Primary (injected adapter: File/S3/Loki/custom)
            ↓ fails
        Fallback (Local File)
            ↓ fails
        Syslog (OS-level)
            ↓ fails
        stderr (last resort)

    Non-intrusive principle:
        - Never touches the customer's DB directly
        - Default: FileAuditLogAdapter (local JSONL)
        - Customers may swap the adapter to use S3/Loki/DB instead
    """

    def __init__(
        self,
        audit_adapter: AuditLogAdapter,
        config: AuditConfig | None = None,
        resilient_config: ResilientRecorderConfig | None = None,
        alert_callback: Callable[[str, dict[str, Any]], None] | None = None,
        state_file: Path | None = None,
    ):
        """
        Initialize ResilientContinuousAuditRecorder.

        Args:
            audit_adapter: audit log storage adapter (Primary)
            config: audit configuration
            resilient_config: resilient-feature configuration
            alert_callback: alert callback
            state_file: hash-chain state file path
        """
        super().__init__(
            audit_adapter=audit_adapter,
            config=config,
            alert_callback=alert_callback,
            state_file=state_file,
        )

        self._resilient_config = (
            resilient_config or ResilientRecorderConfig.from_settings()
        )

        # ─────────────────────────────────────────────────────────
        # Wire up existing resilience.py components
        # ─────────────────────────────────────────────────────────
        self._cb_registry = CircuitBreakerRegistry.get_instance()
        self._circuit_breaker = self._cb_registry.get_or_create(
            "audit_primary",
            AuditCircuitBreakerConfig(
                failure_threshold=self._resilient_config.circuit_failure_threshold,
                success_threshold=self._resilient_config.circuit_success_threshold,
                timeout_seconds=self._resilient_config.circuit_timeout_seconds,
                call_timeout_seconds=self._resilient_config.circuit_call_timeout_seconds,
            ),
        )
        self._syslog_fallback = SyslogFallback.get_instance()
        self._metrics = AuditMetrics.get_instance()
        self._degraded_manager = DegradedModeManager.get_instance()

        # ─────────────────────────────────────────────────────────
        # New components
        # ─────────────────────────────────────────────────────────
        self._buffer: RingBuffer[dict[str, Any]] = RingBuffer(
            capacity=self._resilient_config.buffer_capacity,
            strategy=self._resilient_config.backpressure_strategy,
        )

        # Executor for Primary Store writes (timeout protection,
        # at most one zombie thread)
        self._write_executor: ThreadPoolExecutor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="audit_write",
        )

        # Background flush worker
        self._flush_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._started = False
        self._handle: Any | None = None  # DaemonWorkerHandle (impl 489 D9)

        # Fallback file adapter (lazy init)
        self._fallback_adapter: AuditLogAdapter | None = None
        if self._resilient_config.fallback_file_path:
            self._init_fallback_adapter()

        # Self-audit logging
        self_audit().log(
            SelfAuditEvent.INITIALIZED, "ResilientContinuousAuditRecorder initialized"
        )

        # Start background flush
        if self._resilient_config.enable_background_flush:
            self.start()

    def _init_fallback_adapter(self) -> None:
        """Fallback file adapter init.

        416 D20: uses plain ``FileAuditLogAdapter`` (NOT
        ``HashChainFileAuditLogAdapter``). Tamper-evidence is preserved
        via ``entry.details["integrity"]`` which is populated by
        ``_record_with_integrity()`` before buffering and survives the
        round-trip through ``to_dict()`` / ``from_dict()``. The
        hash-chain version would (a) double-hash the integrity field
        and (b) collide with the primary chain's state file.
        """
        try:
            from baldur.adapters.audit.file_adapter import FileAuditLogAdapter

            assert self._resilient_config.fallback_file_path is not None  # caller guard
            fallback_path = Path(self._resilient_config.fallback_file_path)
            fallback_path.parent.mkdir(parents=True, exist_ok=True)
            self._fallback_adapter = FileAuditLogAdapter(str(fallback_path))
        except Exception as e:
            logger.warning(
                "resilient_recorder.fallback_adapter_init_failed",
                error=e,
            )

    # ─────────────────────────────────────────────────────────────
    # Lifecycle
    # ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Start the background flush worker."""
        from baldur.meta.daemon_worker import DaemonWorkerHandle
        from baldur.metrics.recorders.daemon_worker import register_daemon_worker

        if self._started:
            return

        self._started = True
        self._stop_event.clear()

        self._spawn_thread()
        assert self._flush_thread is not None  # _spawn_thread() invariant
        self._handle = DaemonWorkerHandle(
            thread=self._flush_thread,
            tick_interval_seconds=self._resilient_config.flush_interval_seconds,
            restart_callback=self._spawn_thread,
        )
        register_daemon_worker("AuditFlushWorker", self._handle)

        self_audit().log(SelfAuditEvent.STARTUP, "Background flush worker started")
        logger.info("resilient_recorder.background_flush_worker_started")

    def _spawn_thread(self) -> None:
        """Construct + start a fresh flush thread (impl 489 D9)."""
        self._flush_thread = threading.Thread(
            target=self._flush_loop_with_crash_capture,
            daemon=True,
            name="AuditFlushWorker",
        )
        self._flush_thread.start()
        if self._handle is not None:
            self._handle.thread = self._flush_thread

    def _flush_loop_with_crash_capture(self) -> None:
        try:
            self._flush_loop()
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException as e:
            if self._handle is not None:
                self._handle.record_crash(e)
            raise

    def stop(self, timeout: float | None = None) -> None:
        """Stop the background flush worker."""
        from baldur.metrics.recorders.daemon_worker import unregister_daemon_worker

        if timeout is None:
            from baldur.settings.thread_management import (
                get_thread_management_settings,
            )

            timeout = get_thread_management_settings().join_timeout
        if not self._started:
            return

        if self._handle is not None:
            self._handle.is_stopping = True

        self._stop_event.set()

        if self._flush_thread:
            self._flush_thread.join(timeout=timeout)

        unregister_daemon_worker("AuditFlushWorker")
        if self._flush_thread is not None and self._flush_thread.is_alive():
            logger.critical(
                "daemon_worker.stop_join_timeout",
                worker_name="AuditFlushWorker",
                join_timeout_seconds=timeout,
            )

        # Drain remaining buffer (needs the executor)
        self._flush_remaining()

        # Executor cleanup: called after _flush_remaining() completes.
        # wait=False — a hung zombie thread must not block application shutdown
        self._write_executor.shutdown(wait=False)

        self._started = False
        self_audit().log(SelfAuditEvent.SHUTDOWN, "Background flush worker stopped")
        logger.info("resilient_recorder.background_flush_worker_stopped")

    # ─────────────────────────────────────────────────────────────
    # Override: record_with_integrity
    # ─────────────────────────────────────────────────────────────

    def _record_with_integrity(self, entry: AuditEntry) -> str:
        """
        Record together with the hash chain (non-blocking).

        Appends to the RingBuffer first; the background worker does the
        actual store.
        """
        with self._lock:
            # Convert the entry to a dictionary
            entry_dict = entry.to_dict()

            # Add hash-chain integrity information
            entry_dict = self._hash_manager.add_integrity(entry_dict)

            # Include the integrity information in details
            entry.details["integrity"] = entry_dict.get("integrity", {})

            # Add checksum
            entry_dict["checksum"] = compute_crc32(entry_dict)

            # Generate the ID
            integrity = entry_dict.get("integrity", {})
            audit_id = f"audit-{entry.timestamp.strftime('%Y%m%d%H%M%S')}-{integrity.get('sequence', 0):06d}"
            entry_dict["audit_id"] = audit_id

            # Append to the RingBuffer (non-blocking)
            if not self._buffer.put(entry_dict):
                self_audit().log(
                    SelfAuditEvent.BUFFER_OVERFLOW,
                    "Buffer full, entry dropped",
                    details={"audit_id": audit_id},
                )
                self._metrics.record_failure("RingBuffer", "overflow")

            logger.debug(
                "resilient_recorder.queued",
                entry_action=entry.action,
                audit_id=audit_id,
            )

            return audit_id

    # ─────────────────────────────────────────────────────────────
    # Background Flush
    # ─────────────────────────────────────────────────────────────

    def _flush_loop(self) -> None:
        """Background flush loop."""
        import time as _time

        while not self._stop_event.is_set():
            iter_start = _time.monotonic()
            try:
                self._flush_batch()
            except Exception as e:
                self_audit().log(
                    SelfAuditEvent.BATCH_FLUSH_FAILED,
                    f"Flush loop error: {e}",
                )
                logger.exception(
                    "resilient_recorder.flush_loop_error",
                    error=e,
                )

            if self._handle is not None:
                self._handle.observe_iteration(_time.monotonic() - iter_start)
                self._handle.heartbeat()

            # Wait for next interval or stop event
            self._stop_event.wait(timeout=self._resilient_config.flush_interval_seconds)

    def _flush_batch(self) -> int:
        """
        Flush a batch.

        Returns:
            Number of entries processed
        """
        batch = self._buffer.get_batch(self._resilient_config.flush_batch_size)

        if not batch:
            return 0

        processed = 0
        for entry_dict in batch:
            success = self._write_with_fallback(entry_dict)
            if success:
                processed += 1

        if processed > 0:
            logger.debug(
                "resilient_recorder.flushed_entries",
                processed=processed,
                batch_count=len(batch),
            )

        return processed

    def _flush_remaining(self) -> None:
        """Flush everything left in the buffer."""
        total = 0
        while not self._buffer.is_empty:
            count = self._flush_batch()
            total += count
            if count == 0:
                break

        if total > 0:
            logger.info(
                "resilient_recorder.final_flush_entries",
                flushed_total=total,
            )

    def _write_with_fallback(self, entry_dict: dict[str, Any]) -> bool:
        """
        Write through the fallback chain.

        Primary → Fallback → Syslog → stderr
        """
        start_time = time.time()

        # 1. Primary Store (with Circuit Breaker)
        if self._circuit_breaker.can_execute():
            try:
                self._write_to_primary_with_timeout(
                    entry_dict,
                    timeout=self._circuit_breaker.config.call_timeout_seconds,
                )
                self._circuit_breaker.record_success()
                self._metrics.record_write(
                    "Primary",
                    success=True,
                    duration_ms=(time.time() - start_time) * 1000,
                )
                return True
            except Exception as e:
                self._circuit_breaker.record_failure()
                self._metrics.record_failure("Primary", type(e).__name__)
                self_audit().log(
                    SelfAuditEvent.PRIMARY_STORE_FAILED,
                    f"Primary store failed: {e}",
                    details={"error": str(e)},
                )

                # Circuit state-change notification
                if self._circuit_breaker.state == CircuitState.OPEN:
                    self_audit().log(
                        SelfAuditEvent.CIRCUIT_OPENED,
                        "Circuit breaker opened for Primary",
                    )

        # 2. Fallback File
        if self._fallback_adapter:
            try:
                self._write_to_fallback(entry_dict)
                self._metrics.record_write("Fallback", success=True)
                self_audit().log(
                    SelfAuditEvent.FALLBACK_ACTIVATED,
                    "Fallback file used",
                )
                return True
            except Exception as e:
                self._metrics.record_failure("Fallback", type(e).__name__)
                self_audit().log(
                    SelfAuditEvent.FALLBACK_FAILED,
                    f"Fallback failed: {e}",
                )

        # 3. Syslog
        if self._resilient_config.enable_syslog_fallback:
            try:
                self._write_to_syslog(entry_dict)
                self._metrics.record_write("Syslog", success=True)
                self_audit().log(
                    SelfAuditEvent.SYSLOG_ACTIVATED,
                    "Syslog fallback used",
                )
                return True
            except Exception as e:
                self._metrics.record_failure("Syslog", type(e).__name__)
                self_audit().log(
                    SelfAuditEvent.SYSLOG_FAILED,
                    f"Syslog failed: {e}",
                )

        # 4. stderr (last resort)
        self._write_to_stderr(entry_dict)
        return True

    def _write_to_primary_with_timeout(
        self,
        entry_dict: dict[str, Any],
        timeout: float,
    ) -> None:
        """Write to the Primary Store under a timeout."""
        future = self._write_executor.submit(self._write_to_primary, entry_dict)
        try:
            future.result(timeout=timeout)
        except FuturesTimeoutError as err:
            # Already-running task: cancel is a no-op (Python threads cannot
            # be force-killed).
            # Task still queued: remove it from the queue to prevent a late
            # execution / duplicate record.
            future.cancel()
            raise TimeoutError(
                f"Primary store write timed out after {timeout}s"
            ) from err

    def _write_to_primary(self, entry_dict: dict[str, Any]) -> None:
        """Write to the Primary Store."""
        entry = AuditEntry.from_dict(entry_dict)
        self.audit_adapter.log(entry)

    def _write_to_fallback(self, entry_dict: dict[str, Any]) -> None:
        """Write to the Fallback File."""
        if self._fallback_adapter:
            entry = AuditEntry.from_dict(entry_dict)
            self._fallback_adapter.log(entry)

    def _write_to_syslog(self, entry_dict: dict[str, Any]) -> None:
        """Write to Syslog."""
        action = entry_dict.get("action", "unknown")
        audit_id = entry_dict.get("audit_id", "unknown")
        self._syslog_fallback.log_critical(
            event_type="audit_entry",
            message=f"{action}: {audit_id}",
            details={"checksum": entry_dict.get("checksum")},
        )

    def _write_to_stderr(self, entry_dict: dict[str, Any]) -> None:
        """Write to stderr (last resort, skipped in test environments)."""
        import os
        import sys

        from baldur.utils.serialization import fast_dumps_str

        if os.environ.get("BALDUR_TEST_MODE"):
            return

        try:
            line = fast_dumps_str(entry_dict, default=str)
            print(f"[AUDIT-FALLBACK] {line}", file=sys.stderr, flush=True)
        except Exception:
            pass

    # ─────────────────────────────────────────────────────────────
    # Status & Metrics
    # ─────────────────────────────────────────────────────────────

    def get_buffer_stats(self) -> RingBufferStats:
        """Return buffer statistics."""
        return self._buffer.get_stats()

    def get_health_status(self) -> dict[str, Any]:
        """Return the health status."""
        buffer_stats = self._buffer.get_stats()

        return {
            "healthy": self._is_healthy(),
            "started": self._started,
            "circuit_breaker": {
                "state": self._circuit_breaker.state.value,
                "stats": self._circuit_breaker.get_stats(),
            },
            "buffer": {
                "size": buffer_stats.size,
                "capacity": buffer_stats.capacity,
                "drop_rate": buffer_stats.drop_rate,
                "total_dropped": buffer_stats.total_dropped,
            },
            "write_executor": {
                "active_threads": len(self._write_executor._threads),
                "pending_tasks": self._write_executor._work_queue.qsize(),
            },
            "degraded_mode": self._degraded_manager.is_degraded,
            "self_audit": {
                "is_healthy": self_audit().is_healthy(),
                "failure_rate": self_audit().get_failure_rate(),
            },
        }

    def _is_healthy(self) -> bool:
        """Check the health status."""
        # Unhealthy once the buffer is 80% or more full
        buffer_stats = self._buffer.get_stats()
        if buffer_stats.size > buffer_stats.capacity * 0.8:
            return False

        # Unhealthy while the circuit breaker is open
        if self._circuit_breaker.state == CircuitState.OPEN:
            return False

        # Unhealthy when the self-audit failure rate is high
        return self_audit().is_healthy()

    def force_flush(self) -> int:
        """Flush manually."""
        return self._flush_batch()

    def __enter__(self):
        """Context manager entry."""
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit."""
        self.stop()
        return False
