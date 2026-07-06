"""
Async Audit Writer (Non-blocking writes).

Provides asynchronous audit writing with background thread.
"""

import queue
import threading
import time
from collections.abc import Callable
from typing import Any

import structlog

from baldur.meta.daemon_worker import DaemonWorkerHandle
from baldur.metrics.recorders.daemon_worker import (
    register_daemon_worker,
    unregister_daemon_worker,
)

logger = structlog.get_logger()

_WORKER_NAME = "AsyncAuditWriter"


class AsyncAuditWriter:
    """
    Async audit writer with background thread.

    Problem:
        Synchronous file/Redis writes block request handling.
        Write latency directly impacts API response time.

    Solution:
        Queue entries for background thread processing.
        Request handler returns immediately after queueing.

    Pattern source:
        audit/audit_watchdog.py#L150-270 (daemon thread pattern)

    Usage:
        writer = AsyncAuditWriter(sync_writer)
        writer.start()
        writer.write_async(entry)  # Non-blocking
    """

    def __init__(
        self,
        sync_writer: Callable[[dict[str, Any]], bool],
        max_queue_size: int = 10000,
        batch_size: int = 50,
        flush_interval_seconds: float = 0.1,
    ):
        """
        Initialize async audit writer.

        Args:
            sync_writer: Synchronous write function to wrap
            max_queue_size: Maximum queue size before blocking
            batch_size: Number of entries to write per batch
            flush_interval_seconds: Max time between writes
        """
        self._sync_writer = sync_writer
        self._queue: queue.Queue = queue.Queue(maxsize=max_queue_size)
        self._batch_size = batch_size
        self._flush_interval = flush_interval_seconds
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._is_running = False
        self._entries_queued = 0
        self._entries_written = 0
        self._entries_dropped = 0
        self._handle: DaemonWorkerHandle | None = None

    def start(self) -> None:
        """Start background writer thread."""
        if self._is_running:
            return

        self._is_running = True
        self._stop_event.clear()

        self._spawn_thread()
        assert self._thread is not None  # _spawn_thread() invariant
        self._handle = DaemonWorkerHandle(
            thread=self._thread,
            tick_interval_seconds=self._flush_interval,
            restart_callback=self._spawn_thread,
        )
        register_daemon_worker(_WORKER_NAME, self._handle)
        logger.info("async_writer.started")

    def _spawn_thread(self) -> None:
        """Construct + start a fresh writer thread (impl 489 D9 respawn helper)."""
        self._thread = threading.Thread(
            target=self._writer_loop_with_crash_capture,
            daemon=True,
            name=_WORKER_NAME,
        )
        self._thread.start()
        if self._handle is not None:
            self._handle.thread = self._thread

    def _writer_loop_with_crash_capture(self) -> None:
        try:
            self._writer_loop()
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException as e:
            if self._handle is not None:
                self._handle.record_crash(e)
            raise

    def stop(self, timeout: float | None = None) -> None:
        """Stop background writer thread."""
        if timeout is None:
            from baldur.settings.thread_management import (
                get_thread_management_settings,
            )

            timeout = get_thread_management_settings().join_timeout
        if not self._is_running:
            return

        if self._handle is not None:
            self._handle.is_stopping = True

        self._stop_event.set()

        if self._thread:
            self._thread.join(timeout=timeout)

        self._is_running = False
        unregister_daemon_worker(_WORKER_NAME)
        if self._thread is not None and self._thread.is_alive():
            logger.critical(
                "daemon_worker.stop_join_timeout",
                worker_name=_WORKER_NAME,
                join_timeout_seconds=timeout,
            )
        logger.info(
            "async_audit_writer.stopped_queued_written",
            entries_queued=self._entries_queued,
            entries_written=self._entries_written,
        )

    def write_async(
        self,
        entry: dict[str, Any],
        block: bool = False,
    ) -> bool:
        """
        Queue entry for async write.

        Args:
            entry: Entry to write
            block: Block if queue is full (default: drop)

        Returns:
            True if queued successfully
        """
        try:
            self._queue.put(entry, block=block, timeout=0.01)
            self._entries_queued += 1
            return True
        except queue.Full:
            self._entries_dropped += 1
            logger.warning(
                "async_audit_writer.queue_full_entry_dropped",
                entries_dropped=self._entries_dropped,
            )
            return False

    def _writer_loop(self) -> None:
        """Background writer loop."""
        batch: list[dict[str, Any]] = []
        last_flush = time.monotonic()

        while not self._stop_event.is_set():
            iter_start = time.monotonic()
            try:
                # Collect batch
                try:
                    entry = self._queue.get(timeout=self._flush_interval)
                    batch.append(entry)
                except queue.Empty:
                    pass

                # Check flush conditions
                should_flush = len(batch) >= self._batch_size or (
                    batch and time.monotonic() - last_flush >= self._flush_interval
                )

                if should_flush and batch:
                    self._flush_batch(batch)
                    batch = []
                    last_flush = time.monotonic()

            except Exception as e:
                logger.exception(
                    "async_audit_writer.writer_loop_error",
                    error=e,
                )

            if self._handle is not None:
                self._handle.observe_iteration(time.monotonic() - iter_start)
                self._handle.heartbeat()

        # Final flush on stop
        if batch:
            self._flush_batch(batch)

    def _flush_batch(self, batch: list[dict[str, Any]]) -> None:
        """Flush a batch of entries."""
        for entry in batch:
            try:
                if self._sync_writer(entry):
                    self._entries_written += 1
            except Exception as e:
                logger.exception(
                    "async_audit_writer.write_failed",
                    error=e,
                )

    def get_stats(self) -> dict[str, Any]:
        """Get writer statistics."""
        return {
            "queued": self._entries_queued,
            "written": self._entries_written,
            "dropped": self._entries_dropped,
            "queue_size": self._queue.qsize(),
            "is_running": self._is_running,
        }
