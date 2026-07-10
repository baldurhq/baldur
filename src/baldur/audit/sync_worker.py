"""
Background Sync Worker - WAL → central store synchronization.

Core component of the Fail-Open + WAL-based zero-loss guarantee.

How it works:
1. Read unsynced entries from the WAL (synced=False)
2. Attempt to write them to the central store
3. On success, clean up the WAL entries (cleanup_processed)
4. On failure, retry (exponential backoff)

Usage:
    from baldur.audit.sync_worker import AuditSyncWorker, SyncWorkerConfig

    worker = AuditSyncWorker(
        wal=wal_instance,
        central_adapter=adapter,
    )
    worker.start()

    # On shutdown
    worker.stop()
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import structlog

from baldur.core.backoff import ExponentialBackoff
from baldur.interfaces.audit_adapter import AuditEntry

if TYPE_CHECKING:
    from baldur.audit.checkpoint import CheckpointStorageStrategy
    from baldur.settings.audit_sync import AuditSyncSettings

logger = structlog.get_logger()


@dataclass
class SyncWorkerConfig:
    """Sync Worker configuration."""

    # Synchronization interval (seconds)
    sync_interval_seconds: float = 1.0

    # Batch size
    batch_size: int = 100

    # Retry settings
    max_retries: int = 3
    retry_delay_seconds: float = 1.0
    retry_backoff_multiplier: float = 2.0
    max_retry_delay_seconds: float = 30.0

    # Threshold for cleaning up stale entries (seconds)
    cleanup_after_seconds: float = 3600.0  # 1 hour

    # Metrics reporting interval (seconds)
    metrics_interval_seconds: float = 60.0

    # Checkpoint save settings
    checkpoint_save_interval_batches: int = 10  # save every N batches
    checkpoint_save_interval_seconds: float = 30.0  # max save interval

    # Consecutive failing batches where the B-contiguous cursor cannot advance
    # (a permanently-failing head entry) before a CRITICAL cursor_stalled alert.
    cursor_stall_alert_cycles: int = 5

    @classmethod
    def from_settings(
        cls,
        settings: AuditSyncSettings | None = None,
        **overrides,
    ) -> SyncWorkerConfig:
        """
        Create a SyncWorkerConfig instance from settings.

        Args:
            settings: AuditSyncSettings instance (uses the singleton if omitted)
            **overrides: individual field overrides

        Returns:
            SyncWorkerConfig: instance derived from settings
        """
        from baldur.settings.audit_sync import get_audit_sync_settings

        s = settings or get_audit_sync_settings()
        return cls(
            sync_interval_seconds=overrides.get(
                "sync_interval_seconds", s.sync_interval_seconds
            ),
            batch_size=overrides.get("batch_size", s.batch_size),
            max_retries=overrides.get("max_retries", s.max_retries),
            retry_delay_seconds=overrides.get(
                "retry_delay_seconds", s.retry_delay_seconds
            ),
            retry_backoff_multiplier=overrides.get(
                "retry_backoff_multiplier", s.retry_backoff_multiplier
            ),
            max_retry_delay_seconds=overrides.get(
                "max_retry_delay_seconds", s.max_retry_delay_seconds
            ),
            cleanup_after_seconds=overrides.get(
                "cleanup_after_seconds", s.cleanup_after_seconds
            ),
            metrics_interval_seconds=overrides.get(
                "metrics_interval_seconds", s.metrics_interval_seconds
            ),
            checkpoint_save_interval_batches=overrides.get(
                "checkpoint_save_interval_batches",
                getattr(s, "checkpoint_save_interval_batches", 10),
            ),
            checkpoint_save_interval_seconds=overrides.get(
                "checkpoint_save_interval_seconds",
                getattr(s, "checkpoint_save_interval_seconds", 30.0),
            ),
            cursor_stall_alert_cycles=overrides.get(
                "cursor_stall_alert_cycles",
                getattr(s, "cursor_stall_alert_cycles", 5),
            ),
        )


@dataclass
class SyncStats:
    """Synchronization statistics."""

    total_synced: int = 0
    total_failed: int = 0
    total_retries: int = 0
    last_sync_time: float | None = None
    last_sync_count: int = 0
    last_error: str | None = None
    current_lag_entries: int = 0

    # Performance statistics
    avg_sync_duration_ms: float = 0.0
    _sync_durations: list[float] = field(default_factory=list)

    def record_sync_duration(self, duration_ms: float) -> None:
        """Record the time taken for a synchronization."""
        self._sync_durations.append(duration_ms)
        # Keep only the most recent 100
        if len(self._sync_durations) > 100:
            self._sync_durations = self._sync_durations[-100:]
        self.avg_sync_duration_ms = sum(self._sync_durations) / len(
            self._sync_durations
        )

    def to_dict(self) -> dict[str, Any]:
        """Convert to a dictionary."""
        return {
            "total_synced": self.total_synced,
            "total_failed": self.total_failed,
            "total_retries": self.total_retries,
            "last_sync_time": self.last_sync_time,
            "last_sync_count": self.last_sync_count,
            "last_error": self.last_error,
            "current_lag_entries": self.current_lag_entries,
            "avg_sync_duration_ms": round(self.avg_sync_duration_ms, 2),
        }


class AuditSyncWorker:
    """
    Background Sync Worker.

    Background worker that synchronizes audit events written to the WAL to the
    central store.

    Thread-safe, operated as a single instance.
    """

    _instance: AuditSyncWorker | None = None
    _instance_lock = threading.Lock()

    def __init__(
        self,
        wal: Any = None,
        central_adapter: Any = None,
        config: SyncWorkerConfig | None = None,
        on_sync_complete: Callable[[int, int], None] | None = None,
        on_sync_error: Callable[[Exception], None] | None = None,
    ):
        """
        Initialize Sync Worker.

        Args:
            wal: WriteAheadLog instance (obtained from audit_helpers if None)
            central_adapter: central store adapter (AuditLogAdapter)
            config: worker configuration
            on_sync_complete: sync-complete callback (synced_count, failed_count)
            on_sync_error: sync-error callback
        """
        self._wal = wal
        self._central_adapter = central_adapter
        self._config = config or SyncWorkerConfig.from_settings()
        self._on_sync_complete = on_sync_complete
        self._on_sync_error = on_sync_error
        self._checkpoint_strategy: CheckpointStorageStrategy | None = None

        self._stats = SyncStats()
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._running = False
        self._handle: Any | None = None  # DaemonWorkerHandle (impl 489 D9)

        # Last processed sequence (for WAL cleanup)
        self._last_processed_seq: int = 0

        # Edge-triggered guard for the no-central-adapter WARNING: warn once per
        # unwired episode, reset the moment an adapter reappears.
        self._no_adapter_warned: bool = False

        # Cursor-stall detection: the B-contiguous cursor holds at the first
        # per-entry failure (zero-loss), so a permanently-failing head entry
        # pins the cursor. Count consecutive stalled cycles; alert once per
        # episode (edge-triggered like _no_adapter_warned), reset on advance.
        self._stall_cycles: int = 0
        self._cursor_stall_alerted: bool = False

        # Checkpoint saving state
        self._batches_since_checkpoint: int = 0
        self._last_checkpoint_time: float = time.time()

        logger.info(
            "audit_sync_worker.initialized",
            sync_interval_seconds=self._config.sync_interval_seconds,
            batch_size=self._config.batch_size,
        )

    @classmethod
    def get_instance(
        cls,
        wal: Any = None,
        central_adapter: Any = None,
        config: SyncWorkerConfig | None = None,
    ) -> AuditSyncWorker:
        """Get or create singleton instance."""
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    cls._instance = cls(
                        wal=wal,
                        central_adapter=central_adapter,
                        config=config,
                    )
        return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        """Reset singleton (for testing)."""
        with cls._instance_lock:
            if cls._instance:
                cls._instance.stop()
            cls._instance = None

    def _get_wal(self) -> Any:
        """Get the WAL instance."""
        if self._wal is not None:
            return self._wal

        # Obtain from audit_helpers
        try:
            from baldur_pro.services.audit import _get_wal

            return _get_wal()
        except Exception as e:
            logger.warning(
                "audit_sync_worker.get_wal_failed",
                error=e,
            )
            return None

    def _get_adapter(self) -> Any:
        """Get the central store adapter."""
        if self._central_adapter is not None:
            return self._central_adapter

        # Obtain from ProviderRegistry
        try:
            from baldur.factory import ProviderRegistry

            return ProviderRegistry.get_audit_adapter()
        except Exception as e:
            logger.debug(
                "audit_sync_worker.adapter_available",
                error=e,
            )
            return None

    def start(self) -> bool:
        """
        Start the worker.

        Returns:
            True: started successfully
            False: already running
        """
        from baldur.meta.daemon_worker import DaemonWorkerHandle
        from baldur.metrics.recorders.daemon_worker import register_daemon_worker

        with self._lock:
            if self._running:
                return False

            self._stop_event.clear()
            self._running = True
            self._spawn_thread()
            assert self._thread is not None  # _spawn_thread() invariant
            self._handle = DaemonWorkerHandle(
                thread=self._thread,
                tick_interval_seconds=self._config.sync_interval_seconds,
                restart_callback=self._spawn_thread,
            )
            register_daemon_worker("AuditSyncWorker", self._handle)
            logger.info("sync_worker.started")
            return True

    def _spawn_thread(self) -> None:
        """Construct + start a fresh sync loop thread (impl 489 D9 respawn helper)."""
        self._thread = threading.Thread(
            target=self._run_loop_with_crash_capture,
            name="AuditSyncWorker",
            daemon=True,
        )
        self._thread.start()
        if self._handle is not None:
            self._handle.thread = self._thread

    def _run_loop_with_crash_capture(self) -> None:
        try:
            self._run_loop()
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException as e:
            if self._handle is not None:
                self._handle.record_crash(e)
            raise

    def stop(self, timeout: float = 1.0) -> None:
        """
        Stop the worker.

        Args:
            timeout: time to wait for shutdown (seconds)
        """
        from baldur.metrics.recorders.daemon_worker import unregister_daemon_worker

        with self._lock:
            if not self._running:
                return

            if self._handle is not None:
                self._handle.is_stopping = True

            self._stop_event.set()
            self._running = False

        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)

        unregister_daemon_worker("AuditSyncWorker")
        if self._thread is not None and self._thread.is_alive():
            logger.critical(
                "daemon_worker.stop_join_timeout",
                worker_name="AuditSyncWorker",
                join_timeout_seconds=timeout,
            )

        logger.info("sync_worker.stopped")

    def _run_loop(self) -> None:
        """Main synchronization loop."""
        last_metrics_time = time.time()

        while not self._stop_event.is_set():
            iter_start = time.monotonic()
            try:
                # Perform synchronization
                synced, failed = self._sync_batch()

                if synced > 0 or failed > 0:
                    logger.debug(
                        "audit_sync_worker.synced_failed",
                        synced=synced,
                        failed=failed,
                    )

                # Metrics reporting
                now = time.time()
                if now - last_metrics_time >= self._config.metrics_interval_seconds:
                    self._report_metrics()
                    last_metrics_time = now

            except Exception as e:
                logger.exception(
                    "audit_sync_worker.sync_loop_error",
                    error=e,
                )
                if self._on_sync_error:
                    try:
                        self._on_sync_error(e)
                    except Exception:
                        pass

            if self._handle is not None:
                self._handle.observe_iteration(time.monotonic() - iter_start)
                self._handle.heartbeat()

            # Wait until the next cycle
            self._stop_event.wait(timeout=self._config.sync_interval_seconds)

    def _process_batch_entries(
        self, adapter: Any, batch: list, synced_count: int, failed_count: int
    ) -> tuple[int, int]:
        """Sync each entry in the batch. Precondition: ``adapter`` is non-None.

        Advances the persisted cursor (``_last_processed_seq``) only over the
        **contiguous leading run** of successes. The batch is sequence-ascending
        (``recover_unprocessed`` returns sorted entries), so on the first
        per-entry failure the cursor stops advancing even though later entries
        are still attempted and delivered. This keeps the WAL file holding the
        failed entry from being unlinked by file-granular ``cleanup_processed``
        (``max_seq <= cursor``), guaranteeing zero loss on recovery replay.
        Later successes are re-read next cycle, where idempotency dedup +
        central ``ON CONFLICT`` make the re-delivery a safe no-op.
        """
        advance_ok = True
        for entry in batch:
            try:
                self._sync_entry_to_adapter(adapter, entry)
                synced_count += 1
                if advance_ok:
                    self._last_processed_seq = max(
                        self._last_processed_seq, entry.sequence
                    )
            except Exception as e:
                # Gap: do not advance the cursor past an undelivered entry.
                advance_ok = False
                failed_count += 1
                logger.warning(
                    "audit_sync_worker.sync_entry_failed",
                    entry_sequence=entry.sequence,
                    error=e,
                )
        return synced_count, failed_count

    def _post_sync_cleanup(self, synced_count: int, wal: Any) -> None:
        """Post-sync cleanup and checkpoint save."""
        if synced_count <= 0:
            return

        try:
            # mode="runtime": drain only this worker's own-PID files so a
            # peer worker's still-active WAL file is never deleted (#470 G3).
            wal.cleanup_processed(self._last_processed_seq, mode="runtime")
        except Exception as e:
            logger.warning(
                "audit_sync_worker.cleanup_wal_failed",
                error=e,
            )

        self._batches_since_checkpoint += 1
        should_save = (
            self._batches_since_checkpoint
            >= self._config.checkpoint_save_interval_batches
            or time.time() - self._last_checkpoint_time
            >= self._config.checkpoint_save_interval_seconds
        )
        if should_save:
            self._save_checkpoint()
            self._batches_since_checkpoint = 0
            self._last_checkpoint_time = time.time()

    def _update_sync_stats(
        self, synced_count: int, failed_count: int, duration_ms: float
    ) -> None:
        """Update synchronization statistics and invoke the callback."""
        with self._lock:
            self._stats.total_synced += synced_count
            self._stats.total_failed += failed_count
            self._stats.last_sync_time = time.time()
            self._stats.last_sync_count = synced_count
            self._stats.record_sync_duration(duration_ms)

        if self._on_sync_complete and (synced_count > 0 or failed_count > 0):
            try:
                self._on_sync_complete(synced_count, failed_count)
            except Exception:
                pass

    def _update_stall_state(
        self, batch: list, failed_count: int, cursor_before: int
    ) -> None:
        """Detect and surface a cursor stall (B-contiguous zero-loss trade-off).

        The contiguous cursor (``_process_batch_entries``) holds at the first
        per-entry failure so a never-delivered entry is never unlinked. A head
        entry that fails every cycle therefore pins the cursor: it is retained
        and re-read, but the cursor cannot advance past it. That stall is
        counted; once it persists for ``cursor_stall_alert_cycles`` consecutive
        cycles an edge-triggered CRITICAL ``cursor_stalled`` fires (once per
        episode) and the ``wal_sync_cursor_stalled`` gauge is set. The entry is
        **never auto-dropped** — discarding an undelivered audit record is a
        separate compliance decision.
        """
        if self._last_processed_seq > cursor_before:
            # Any forward progress clears the stall episode.
            self._stall_cycles = 0
            if self._cursor_stall_alerted:
                self._cursor_stall_alerted = False
                self._set_cursor_stalled_gauge(False)
            return

        if failed_count <= 0:
            # No failure and no advance — not a stall (e.g. all duplicates).
            return

        # Failure present and the cursor did not move: a stuck head entry. In a
        # stall cycle batch[0] is necessarily the failing head (a successful
        # head would have advanced the cursor), so it identifies the poison.
        self._stall_cycles += 1
        if (
            self._stall_cycles >= self._config.cursor_stall_alert_cycles
            and not self._cursor_stall_alerted
        ):
            self._cursor_stall_alerted = True
            stuck_sequence = batch[0].sequence if batch else self._last_processed_seq
            logger.critical(
                "audit_sync_worker.cursor_stalled",
                stuck_sequence=stuck_sequence,
                pending_entries=self._stats.current_lag_entries,
                stall_cycles=self._stall_cycles,
            )
            self._set_cursor_stalled_gauge(True)

    def _set_cursor_stalled_gauge(self, stalled: bool) -> None:
        """Publish cursor-stall state to the WAL drift gauge (best-effort)."""
        try:
            from baldur.metrics.drift_metrics import update_wal_cursor_stalled

            update_wal_cursor_stalled(stalled)
        except Exception:
            pass

    def _sync_batch(self) -> tuple[int, int]:
        """
        Perform batch synchronization.

        Returns:
            (synced_count, failed_count)
        """
        wal = self._get_wal()
        if wal is None:
            return 0, 0

        adapter = self._get_adapter()
        start_time = time.time()
        synced_count = 0
        failed_count = 0

        try:
            # mode="runtime": read only this worker's own-PID entries — no
            # peer over-replay; the single in-memory cursor thresholds only
            # this worker's own (independent) sequence space (#470 G4).
            entries = wal.recover_unprocessed(self._last_processed_seq, mode="runtime")
            if not entries:
                return 0, 0

            batch = entries[: self._config.batch_size]
            with self._lock:
                self._stats.current_lag_entries = len(entries)

            if adapter is None:
                # No central destination wired — surface the backlog via lag, but
                # do NOT advance the cursor or delete the WAL; entries wait for a
                # wired adapter. Edge-triggered WARNING (once per unwired episode)
                # so a growing WAL backlog is not mistaken for a stalled worker.
                if not self._no_adapter_warned:
                    logger.warning(
                        "audit_sync_worker.central_adapter_unwired",
                        pending_entries=len(entries),
                    )
                    self._no_adapter_warned = True
                return 0, 0

            self._no_adapter_warned = False
            cursor_before = self._last_processed_seq
            synced_count, failed_count = self._process_batch_entries(
                adapter, batch, synced_count, failed_count
            )
            self._post_sync_cleanup(synced_count, wal)
            self._update_stall_state(batch, failed_count, cursor_before)

            duration_ms = (time.time() - start_time) * 1000
            self._update_sync_stats(synced_count, failed_count, duration_ms)

            return synced_count, failed_count

        except Exception as e:
            with self._lock:
                self._stats.last_error = str(e)
            raise

    def absorb_orphans(self) -> int:
        """
        Drain orphan (non-own-PID) WAL entries to the central store once.

        Compensates for the runtime drain partitioning (``mode="runtime"``):
        no live worker drains a crashed peer's (dead-PID) WAL file, so this
        one-shot startup pass reads peer/dead-PID files via
        ``WriteAheadLog.recover_orphans()`` and syncs each entry through the
        idempotent ``_sync_entry_to_adapter`` path.

        Invariants:
        - Does **not** advance ``_last_processed_seq`` — orphan seqs live in
          foreign (per-worker-independent) sequence spaces; advancing would
          re-introduce cursor incoherence.
        - Does **not** ``cleanup_processed`` cross-PID — orphan files are
          reclaimed by the WAL's own retention.
        - Idempotent — re-absorption of an as-yet-unreclaimed orphan, or a
          still-live peer's not-yet-drained entry, is deduplicated within
          ``_sync_entry_to_adapter``.

        Returns:
            Number of orphan entries absorbed.
        """
        wal = self._get_wal()
        if wal is None or not hasattr(wal, "recover_orphans"):
            return 0

        try:
            entries = wal.recover_orphans()
        except Exception as e:
            logger.warning(
                "audit_sync_worker.orphan_recover_failed",
                error=e,
            )
            return 0

        if not entries:
            return 0

        adapter = self._get_adapter()
        if adapter is None:
            # No central destination wired — this one-shot startup pass is a
            # no-op: absorb nothing, advance no cursor, retain the orphan files
            # for a later wired worker. (No anti-spam guard needed — this runs
            # once at startup, not in the recurring _sync_batch loop.)
            return 0

        absorbed = 0
        for entry in entries:
            try:
                self._sync_entry_to_adapter(adapter, entry)
                # Note: no _last_processed_seq advance (foreign sequence space).
                absorbed += 1
            except Exception as e:
                logger.warning(
                    "audit_sync_worker.orphan_absorb_entry_failed",
                    entry_sequence=entry.sequence,
                    error=e,
                )

        if absorbed > 0:
            logger.info(
                "audit_sync_worker.orphans_absorbed",
                absorbed_count=absorbed,
            )
            try:
                from baldur.metrics.drift_metrics import record_wal_orphans_absorbed

                record_wal_orphans_absorbed(absorbed)
            except Exception:
                pass

        return absorbed

    def _sync_entry_to_adapter(self, adapter: Any, entry: Any) -> None:  # noqa: C901
        """
        Sync a single entry to the adapter (Idempotent Consumer pattern).

        Prevents duplicate processing and includes retry logic.
        """
        # Idempotent Consumer: prevent duplicate processing
        idempotency: Any = None
        key: Any = None
        try:
            from baldur.services.idempotency import (
                IdempotencyDomain,
                IdempotencyKey,
                IdempotencyService,
            )

            idempotency = IdempotencyService()
            key = IdempotencyKey.for_operation(
                entity_type="wal_entry",
                entity_id=entry.sequence,
                operation=f"sync:{entry.checksum[:8] if entry.checksum else 'unknown'}",
                domain=IdempotencyDomain.WAL_RECOVERY,
            )

            # Skip if already processed
            result = idempotency.check(key)
            if result.is_duplicate:
                logger.debug(
                    "audit_sync_worker.skipping_duplicate_entry",
                    entry_sequence=entry.sequence,
                )
                return

        except ImportError:
            # IdempotencyService unavailable in this environment
            pass
        except Exception as e:
            logger.debug(
                "audit_sync_worker.idempotency_check_failed",
                error=e,
            )

        # Pipeline B (continuous_audit) hands the adapter a real AuditEntry;
        # this WAL-drain path (Pipeline A) must do the same. Convert once,
        # before the retry loop, so both call sites routing through this method
        # (_process_batch_entries steady drain + absorb_orphans) are fixed by
        # one change. The conversion is total (never raises for a dict input),
        # so a malformed entry cannot become a poison entry that stalls the
        # contiguous cursor.
        audit_entry = AuditEntry.from_wal_dict(entry.data)

        backoff = ExponentialBackoff(
            base_delay=self._config.retry_delay_seconds,
            multiplier=self._config.retry_backoff_multiplier,
            max_delay=self._config.max_retry_delay_seconds,
            jitter=True,
        )
        last_error: Exception | None = None

        for attempt in range(self._config.max_retries + 1):
            try:
                # Deliver the AuditEntry to the adapter's log() contract. No
                # audit adapter implements write() (the ABC declares only
                # log()/query()), so a non-adapter object falls to the
                # structlog emit below.
                if hasattr(adapter, "log"):
                    adapter.log(audit_entry)
                else:
                    logger.info(
                        "audit_sync.event",
                        entry_data=entry.data,
                    )

                # Mark as processed (best-effort)
                if idempotency is not None and key is not None:
                    idempotency.mark_as_processed(key)

                return  # success

            except Exception as e:
                last_error = e
                if attempt < self._config.max_retries:
                    with self._lock:
                        self._stats.total_retries += 1
                    time.sleep(backoff.calculate(attempt + 1))

        # All retries exhausted
        if last_error:
            raise last_error

    def _report_metrics(self) -> None:
        """Report metrics."""
        try:
            from baldur.audit.resilience import AuditMetrics

            metrics = AuditMetrics.get_instance()

            with self._lock:
                stats = self._stats.to_dict()

            # Record custom metric
            metrics.record_write(
                "sync_worker", success=True, duration_ms=stats["avg_sync_duration_ms"]
            )

            logger.debug(
                "audit_sync_worker.metrics",
                stats=stats,
            )

        except Exception as e:
            logger.debug(
                "audit_sync_worker.report_metrics_failed",
                error=e,
            )

    def _get_checkpoint_strategy(self) -> CheckpointStorageStrategy | None:
        """Get the CheckpointStorageStrategy instance."""
        if self._checkpoint_strategy is not None:
            return self._checkpoint_strategy

        try:
            from baldur.audit.checkpoint import get_default_checkpoint_strategy

            self._checkpoint_strategy = get_default_checkpoint_strategy()
            return self._checkpoint_strategy
        except Exception as e:
            logger.debug(
                "audit_sync_worker.checkpoint_strategy_unavailable",
                error=e,
            )
            return None

    def set_checkpoint_strategy(self, strategy: CheckpointStorageStrategy) -> None:
        """Inject a CheckpointStorageStrategy (for testing/customization)."""
        self._checkpoint_strategy = strategy

    def _save_checkpoint(self) -> None:
        """Save the checkpoint immediately (using CheckpointStorageStrategy)."""
        strategy = self._get_checkpoint_strategy()
        if strategy is None:
            logger.warning(
                "audit_sync_worker.no_checkpoint_strategy_available",
                last_processed_seq=self._last_processed_seq,
            )
            return

        try:
            from baldur.audit.checkpoint import UnifiedCheckpointData

            checkpoint_data = UnifiedCheckpointData(
                wal_sequence=self._last_processed_seq,
            )
            strategy.save("sync_worker", checkpoint_data)
            strategy.commit("sync_worker")
            logger.debug(
                "audit_sync_worker.checkpoint_saved",
                last_processed_seq=self._last_processed_seq,
            )
        except Exception as e:
            logger.warning(
                "audit_sync_worker.checkpoint_save_failed",
                error=e,
            )

    def sync_now(self) -> tuple[int, int]:
        """
        Perform synchronization immediately (for testing/debugging).

        Returns:
            (synced_count, failed_count)
        """
        return self._sync_batch()

    def get_stats(self) -> dict[str, Any]:
        """Query synchronization statistics."""
        with self._lock:
            return self._stats.to_dict()

    def get_lag(self) -> int:
        """Current number of entries lagging behind synchronization."""
        wal = self._get_wal()
        if wal is None:
            return 0

        try:
            # mode="runtime": own-PID lag only — keeps this metric coherent
            # with the per-worker cursor (matches _sync_batch).
            entries = wal.recover_unprocessed(self._last_processed_seq, mode="runtime")
            return len(entries)
        except Exception:
            return 0

    @property
    def is_running(self) -> bool:
        """Whether the worker is running."""
        return self._running


# =============================================================================
# Convenience Functions
# =============================================================================


def start_sync_worker(
    wal: Any = None,
    central_adapter: Any = None,
    config: SyncWorkerConfig | None = None,
) -> AuditSyncWorker:
    """
    Helper function to start the Sync Worker.

    Gets the singleton instance and starts it.
    """
    worker = AuditSyncWorker.get_instance(
        wal=wal,
        central_adapter=central_adapter,
        config=config,
    )
    worker.start()
    return worker


def stop_sync_worker() -> None:
    """Helper function to stop the Sync Worker."""
    try:
        worker = AuditSyncWorker.get_instance()
        worker.stop()
    except Exception:
        pass


def get_sync_stats() -> dict[str, Any] | None:
    """Helper function to query Sync Worker statistics."""
    try:
        worker = AuditSyncWorker.get_instance()
        return worker.get_stats()
    except Exception:
        return None
