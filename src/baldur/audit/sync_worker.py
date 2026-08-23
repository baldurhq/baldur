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

import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import structlog

from baldur.core.backoff import ExponentialBackoff
from baldur.core.exceptions import ConfigurationError
from baldur.core.process_utils import fork_repaired
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

        # One-shot orphan absorb, performed by the drain loop rather than by
        # the caller that starts it. Set only by a pass that achieved
        # something, so an outage-booted worker keeps retrying.
        self._orphans_absorbed: bool = False

        # Fork ownership: an instance constructed here is born owned, so the
        # repair no-ops until this object is inherited through fork().
        self._origin_pid: int = os.getpid()
        self._repair_gate = threading.Lock()

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

    def _repair_if_forked(self) -> None:
        """Re-own fork-inherited worker state so this process drains its own.

        Instance-preserving: the registry handle and the respawn callback keep
        referencing this object, so every existing reference stays valid. What
        is renewed is the state a ``fork()`` makes unusable — a lock whose
        recorded owner is a thread that no longer exists, the stop ``Event``
        (replaced, never ``clear()``-ed, since its own internal lock can be
        inherited held), and the reference to a thread that is not running
        here.

        ``_last_processed_seq`` is kept **exactly** as inherited. The child's
        WAL carries the inherited sequence forward, so its first new entry is
        strictly above this cursor and gets drained; resetting the cursor is
        the class of mutation that silently swallows entries.

        Everything that merely *reports* on the parent's run is dropped:
        statistics, the stall counters, the edge-triggered warning latches,
        the checkpoint batch counter, and the one-shot absorb flag — the child
        has its own peers to absorb.

        ``_running`` is left alone (owned by start/stop; the guards compose
        thread aliveness instead), and the handle is kept for identity with
        only its inherited observations reset.
        """
        if os.getpid() == self._origin_pid:
            return

        with self._repair_gate:
            inherited_pid = self._origin_pid
            if os.getpid() == inherited_pid:
                return  # another thread finished the repair first

            # Renew the lock BEFORE anything acquires it.
            self._lock = threading.RLock()
            self._stop_event = threading.Event()
            self._thread = None

            if self._handle is not None:
                self._handle.reset_after_fork()

            self._stats = SyncStats()
            self._stall_cycles = 0
            self._cursor_stall_alerted = False
            self._no_adapter_warned = False
            self._batches_since_checkpoint = 0
            self._last_checkpoint_time = time.time()
            self._orphans_absorbed = False

            self._origin_pid = os.getpid()
            carried_cursor = self._last_processed_seq

        logger.info(
            "sync_worker.fork_state_repaired",
            inherited_pid=inherited_pid,
            carried_cursor=carried_cursor,
        )

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

    def _resolve_central_destination(self) -> Any:
        """The adapter to deliver to, or ``None`` when there is no real one.

        ``_get_adapter()`` cannot answer this: the provider registry falls
        back to the no-op audit adapter, so a booted process always gets an
        object back. Treating that object as a destination makes "delivered"
        satisfiable by a method whose body is ``pass`` — a crashed peer's
        whole backlog would be reported absorbed having reached nowhere.

        Detection is by type rather than by the registry's configured default
        name, which is wrong whenever the adapter was resolved by an explicit
        name. An injected adapter (tests, custom wiring) passes unchanged.
        """
        adapter = self._get_adapter()
        if adapter is None:
            return None

        try:
            from baldur.adapters.audit.null_adapter import NullAuditLogAdapter
        except ImportError:
            return adapter

        if isinstance(adapter, NullAuditLogAdapter):
            return None
        return adapter

    @fork_repaired
    def start(self) -> bool:
        """
        Start the worker.

        Idempotence composes thread aliveness rather than reading ``_running``
        alone: a fork child inherits the flag set together with a thread
        object that no longer runs anywhere.

        Returns:
            True: started successfully
            False: already running
        """
        from baldur.meta.daemon_worker import DaemonWorkerHandle
        from baldur.metrics.recorders.daemon_worker import register_daemon_worker

        with self._lock:
            if self._running and self._thread is not None and self._thread.is_alive():
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

    @fork_repaired
    def _spawn_thread(self) -> None:
        """Construct + start a fresh sync loop thread (impl 489 D9 respawn helper).

        The single atomic spawn point, shared by ``start()`` and by the
        meta-watchdog's respawn callback. The callback contract forbids
        consulting ``_running``, so the fork repair and the spawn idempotence
        both live here: a respawn over inherited state would drain against a
        stale cursor, and a respawn racing ``start()`` would leave two loops
        sharing one cursor.

        Guarded on thread aliveness — never on ``_running``.
        """
        with self._lock:
            existing = self._thread
            if existing is not None and existing.is_alive():
                logger.debug("sync_worker.spawn_skipped_thread_alive")
                return

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

    @fork_repaired
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
        """Main synchronization loop.

        The one-shot orphan absorb runs here — as the loop's first action, on
        the loop's own thread — rather than on whatever thread starts the
        worker. It still precedes the first steady drain, so the ordering that
        made it a start-time step is preserved, but a slow absorb now delays
        the next drain instead of blocking process readiness: with the central
        destination unreachable a synchronous absorb of K backlog entries
        costs K retry budgets, and gunicorn's worker-boot timeout kills a
        worker that spends that long inside its post-fork hook — a respawn
        crash-loop for as long as the destination is down. The watcher for a
        slow absorb is now the WAL lag gauge.
        """
        last_metrics_time = time.time()

        while not self._stop_event.is_set():
            iter_start = time.monotonic()
            try:
                if not self._orphans_absorbed:
                    self._absorb_orphans_once()

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

    def _count_pending(self, wal: Any) -> int:
        """Backlog depth above the cursor, for the lag gauge.

        Prefers ``count_unprocessed()``: it answers from the WAL's in-memory
        sequence with no file reads, the substitution
        ``async_audit_lifecycle._check_unprocessed_wal_entries`` already
        documents as preferred. A WAL-like object that cannot answer falls
        back to counting a full read, and an unreadable WAL reports 0.

        Fail direction: a sequence span over-reports when entries were
        physically reclaimed, i.e. it fails toward flagging a problem — the
        safe direction for a health verdict.
        """
        try:
            if hasattr(wal, "count_unprocessed"):
                return wal.count_unprocessed(self._last_processed_seq)
            entries = wal.recover_unprocessed(self._last_processed_seq, mode="runtime")
            return len(entries)
        except Exception as e:
            logger.debug(
                "audit_sync_worker.pending_count_failed",
                error=e,
            )
            return 0

    def _sync_batch(self) -> tuple[int, int]:
        """
        Perform batch synchronization.

        Returns:
            (synced_count, failed_count)
        """
        wal = self._get_wal()
        if wal is None:
            return 0, 0

        # The lag is the real backlog, not what one cycle read. The read is
        # capped at ``batch_size`` (100 by default), far below the audit health
        # probe's DEGRADED threshold, so deriving the lag from it would make
        # that verdict unreachable. It is written here — above the empty-read
        # early return — because a cycle that reads nothing is the steady state
        # of a wired, idle process, and that cycle must still report the
        # current backlog rather than leave the previous one standing.
        pending_entries = self._count_pending(wal)
        with self._lock:
            self._stats.current_lag_entries = pending_entries

        # Null-aware: the registry falls back to the no-op adapter, so
        # ``_get_adapter()`` would hand back an object whose ``log()`` body
        # is ``pass`` and every entry would be counted as delivered, the
        # cursor advanced, and the WAL file unlinked.
        adapter = self._resolve_central_destination()
        start_time = time.time()
        synced_count = 0
        failed_count = 0

        try:
            # mode="runtime": read only this worker's own-PID entries — no
            # peer over-replay; the single in-memory cursor thresholds only
            # this worker's own (independent) sequence space (#470 G4).
            # ``limit`` is the drain's own budget, so the read can never grow
            # past what this cycle is able to deliver.
            entries = wal.recover_unprocessed(
                self._last_processed_seq,
                mode="runtime",
                limit=self._config.batch_size,
            )
            if not entries:
                return 0, 0

            # Redundant against a real WAL (the read is already capped) and
            # kept as the invariant for a host-injected WAL-like object that
            # ignores ``limit``: the batch never exceeds the budget.
            batch = entries[: self._config.batch_size]

            if adapter is None:
                # No real central destination — surface the backlog via lag, but
                # do NOT advance the cursor or delete the WAL; entries wait for a
                # wired adapter. Edge-triggered WARNING (once per unwired episode)
                # so a growing WAL backlog is not mistaken for a stalled worker.
                if not self._no_adapter_warned:
                    logger.warning(
                        "audit_sync_worker.central_adapter_unwired",
                        pending_entries=pending_entries,
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

    def _absorb_orphans_once(self) -> None:
        """Run the one-shot absorb, consuming it only if the pass achieved something.

        "A destination object exists" is not "the destination is reachable":
        with a real adapter whose backing store is down, every delivery
        exhausts its retries and the pass absorbs nothing. Consuming the
        one-shot on such a pass strands a dead peer's backlog for the whole
        life of this process even though the store returns a minute later. So
        a pass that attempted at least one entry and delivered none is treated
        exactly like having no destination: nothing consumed, retried next
        cycle. A pass that found no orphans at all, or delivered some and
        failed others, does consume it.

        A pass that could not run at all reports ``None`` and consumes
        nothing — no WAL, no real destination, or a recovery read that raised.
        Reading an empty result out of a failure as "there were no orphans" is
        the same mistake as reading a no-op adapter's silence as delivery.
        """
        result = self._absorb_orphans_pass()
        if result is None:
            return

        absorbed, attempted = result
        if attempted > 0 and absorbed == 0:
            return

        self._orphans_absorbed = True

    def _absorb_orphans_pass(self) -> tuple[int, int] | None:
        """One absorb pass. ``(absorbed, attempted)``, or ``None`` if it could not run.

        ``None`` means the pass never got as far as an orphan set it can
        believe: there is no WAL to read, no real destination to deliver to,
        or the recovery read raised. Only a tuple is evidence *about orphans*,
        and only a tuple may consume the caller's one-shot — an empty result
        produced by a failure is a different fact from an empty orphan set,
        and conflating them strands a dead peer's backlog for the whole life
        of this process.

        The destination is resolved before any file is read: with nothing real
        to deliver to, the pass costs one registry lookup instead of a full
        read of every orphan file.

        A pass aborts as soon as the first entry exhausts its retry budget
        with nothing yet delivered. An unreachable store is a property of the
        destination, not of the entry, so there is nothing to learn from
        attempting the other N-1 — and this pass now runs inside the drain
        loop, where the difference is one retry budget per cycle versus one
        per entry.
        """
        wal = self._get_wal()
        if wal is None or not hasattr(wal, "recover_orphans"):
            # Absent, not empty: a WAL that is disabled, still failing its
            # init, or PRO-only on an OSS install may appear later, and
            # ``_sync_batch`` re-resolves it every cycle regardless.
            return None

        destination = self._resolve_central_destination()
        if destination is None:
            return None

        try:
            entries = wal.recover_orphans()
        except Exception as e:
            logger.warning(
                "audit_sync_worker.orphan_recover_failed",
                error=e,
            )
            return None

        if not entries:
            return (0, 0)

        absorbed = 0
        attempted = 0
        for entry in entries:
            attempted += 1
            try:
                self._sync_entry_to_adapter(destination, entry)
                # Note: no _last_processed_seq advance (foreign sequence space).
                absorbed += 1
            except Exception as e:
                logger.warning(
                    "audit_sync_worker.orphan_absorb_entry_failed",
                    entry_sequence=entry.sequence,
                    error=e,
                )
                if absorbed == 0:
                    break

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

        return (absorbed, attempted)

    @fork_repaired
    def absorb_orphans(self) -> int:
        """
        Drain orphan (dead-PID) WAL entries to the central store.

        Compensates for the runtime drain partitioning (``mode="runtime"``):
        no live worker drains a crashed peer's WAL file, so this pass reads
        dead-PID files via ``WriteAheadLog.recover_orphans()`` and syncs each
        entry through the idempotent ``_sync_entry_to_adapter`` path. Files
        whose embedded PID is still running are excluded by the reader — their
        owner is delivering them itself.

        Scheduled as the drain loop's first action rather than performed by
        the caller that starts the worker; this method stays public for
        explicit one-off use.

        Invariants:
        - Does **not** advance ``_last_processed_seq`` — orphan seqs live in
          foreign (per-worker-independent) sequence spaces; advancing would
          re-introduce cursor incoherence.
        - Deletes nothing. Orphan files are reclaimed by the WAL's own
          retention, so a re-absorption is possible and is deduplicated within
          ``_sync_entry_to_adapter``.

        Returns:
            Number of orphan entries absorbed.
        """
        result = self._absorb_orphans_pass()
        return 0 if result is None else result[0]

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
        except ConfigurationError as e:
            # Operator misconfiguration (an unwritable checkpoint directory
            # the operator chose). Checkpointing degrades exactly as before,
            # but the named cause is visible instead of debug-only.
            logger.warning(
                "audit_sync_worker.checkpoint_strategy_unavailable",
                error=e,
            )
            return None
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

    @fork_repaired
    def sync_now(self) -> tuple[int, int]:
        """
        Perform synchronization immediately (for testing/debugging).

        Returns:
            (synced_count, failed_count)
        """
        return self._sync_batch()

    @fork_repaired
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
        """Whether a sync thread is actually running in this process.

        Composed from thread aliveness rather than the control flag alone: a
        fork child inherits ``_running=True`` with a thread that does not
        exist here, and the audit health probe reads this property — a flag
        read would report a healthy pipeline in a worker that has no sync
        thread at all. Deliberately does not repair (a status read must not
        mutate); the liveness composition is what makes it honest.
        """
        return self._running and self._thread is not None and self._thread.is_alive()


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

    Gets the singleton instance and starts the drain. This is the single start
    path — the audit lifecycle delegates here rather than repeating the
    sequence.

    Absorbing a crashed peer's orphan WAL entries is still part of starting,
    but it is now scheduled as the drain thread's first action rather than
    performed on the caller's thread: it precedes the first steady drain
    exactly as before, while a slow or blocked absorb no longer holds up
    process readiness. The one visible consequence is for a short-lived
    process that calls this and exits immediately — its absorb may not
    complete, and the entries are then delivered by the next long-lived
    process rather than lost.
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
