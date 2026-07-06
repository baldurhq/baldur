"""
Pending Sequence Watchdog (Self-Cleanup).

Provides background monitoring and cleanup of stale pending sequences.
"""

import threading
import time
from typing import Any

import structlog

logger = structlog.get_logger()


class PendingSequenceWatchdog:
    """
    Background watchdog for cleaning stale pending sequences.

    Problem:
        PENDING entries may be orphaned if process crashes after reserve
        but before commit/abort. Global TTL (60s) is too long for responsiveness.

    Solution:
        Local watchdog thread monitors own reservations.
        On write failure, immediately cleans up (no TTL wait).

    Pattern source:
        audit/audit_watchdog.py#L150-270 (daemon thread pattern)
        api/django/rate_limit.py#L176-178 (cleanup interval pattern)

    Usage:
        watchdog = PendingSequenceWatchdog(redis_client)
        watchdog.start()

        seq = watchdog.register_pending(5)
        try:
            do_write()
            watchdog.mark_committed(seq)
        except:
            watchdog.mark_failed(seq)  # Immediate cleanup
    """

    def __init__(
        self,
        redis_client: Any,
        key_prefix: str = "baldur:",
        check_interval_seconds: float = 5.0,
        stale_threshold_seconds: float = 30.0,
    ):
        """
        Initialize pending sequence watchdog.

        Args:
            redis_client: Redis client
            key_prefix: Key prefix for Redis keys
            check_interval_seconds: How often to check for stale entries
            stale_threshold_seconds: Age after which entry is considered stale
        """
        self._redis = redis_client
        self._key_prefix = key_prefix
        self._check_interval = check_interval_seconds
        self._stale_threshold = stale_threshold_seconds

        # Track local pending sequences
        self._local_pending: dict[int, float] = {}  # seq -> monotonic_time
        self._lock = threading.RLock()

        # Background thread
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._is_running = False
        self._handle: Any | None = None  # DaemonWorkerHandle (impl 489 D9)

        # Stats
        self._cleaned_count = 0

    def start(self) -> None:
        """Start watchdog thread."""
        from baldur.meta.daemon_worker import DaemonWorkerHandle
        from baldur.metrics.recorders.daemon_worker import register_daemon_worker

        with self._lock:
            if self._is_running:
                return

            self._is_running = True
            self._stop_event.clear()

            self._spawn_thread()
            assert self._thread is not None  # _spawn_thread() invariant
            self._handle = DaemonWorkerHandle(
                thread=self._thread,
                tick_interval_seconds=self._check_interval,
                restart_callback=self._spawn_thread,
            )
            register_daemon_worker("PendingSequenceWatchdog", self._handle)
            logger.info("pending_watchdog.started")

    def _spawn_thread(self) -> None:
        """Construct + start a fresh cleanup thread (impl 489 D9)."""
        self._thread = threading.Thread(
            target=self._cleanup_loop_with_crash_capture,
            daemon=True,
            name="PendingSequenceWatchdog",
        )
        self._thread.start()
        if self._handle is not None:
            self._handle.thread = self._thread

    def _cleanup_loop_with_crash_capture(self) -> None:
        try:
            self._cleanup_loop()
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException as e:
            if self._handle is not None:
                self._handle.record_crash(e)
            raise

    def stop(self, timeout: float = 1.0) -> None:
        """Stop watchdog thread."""
        from baldur.metrics.recorders.daemon_worker import unregister_daemon_worker

        with self._lock:
            if not self._is_running:
                return

            if self._handle is not None:
                self._handle.is_stopping = True

            self._stop_event.set()

            if self._thread:
                self._thread.join(timeout=timeout)

            self._is_running = False
            unregister_daemon_worker("PendingSequenceWatchdog")
            if self._thread is not None and self._thread.is_alive():
                logger.critical(
                    "daemon_worker.stop_join_timeout",
                    worker_name="PendingSequenceWatchdog",
                    join_timeout_seconds=timeout,
                )
            logger.info(
                "pending_watchdog.stopped_cleaned",
                cleaned_count=self._cleaned_count,
            )

    def register_pending(self, sequence: int) -> None:
        """Register a pending sequence for tracking."""
        with self._lock:
            self._local_pending[sequence] = time.monotonic()

    def mark_committed(self, sequence: int) -> None:
        """Mark sequence as committed (remove from tracking)."""
        with self._lock:
            self._local_pending.pop(sequence, None)

    def mark_failed(self, sequence: int) -> None:
        """
        Mark sequence as failed (immediate cleanup).

        Unlike waiting for TTL, this cleans up immediately.
        """
        with self._lock:
            self._local_pending.pop(sequence, None)

        # Immediate Redis cleanup
        try:
            pending_key = f"{self._key_prefix}audit:hash_chain:pending:{sequence}"
            self._redis.delete(pending_key)
            self._cleaned_count += 1
            logger.debug(
                "pending_watchdog.immediately_cleaned_seq",
                sequence=sequence,
            )
        except Exception as e:
            logger.warning(
                "pending_watchdog.cleanup_failed_seq",
                sequence=sequence,
                error=e,
            )

    def _cleanup_loop(self) -> None:
        """Background cleanup loop."""
        while not self._stop_event.is_set():
            iter_start = time.monotonic()
            try:
                self._cleanup_stale_local()
            except Exception as e:
                logger.exception(
                    "pending_watchdog.cleanup_error",
                    error=e,
                )

            if self._handle is not None:
                self._handle.observe_iteration(time.monotonic() - iter_start)
                self._handle.heartbeat()

            self._stop_event.wait(timeout=self._check_interval)

    def _cleanup_stale_local(self) -> None:
        """Clean up locally tracked stale entries."""
        now = time.monotonic()
        stale_sequences = []

        with self._lock:
            for seq, start_time in list(self._local_pending.items()):
                if now - start_time > self._stale_threshold:
                    stale_sequences.append(seq)
                    del self._local_pending[seq]

        for seq in stale_sequences:
            try:
                pending_key = f"{self._key_prefix}audit:hash_chain:pending:{seq}"
                deleted = self._redis.delete(pending_key)
                if deleted:
                    self._cleaned_count += 1
                    logger.info(
                        "pending_watchdog.cleaned_stale_seq",
                        seq=seq,
                    )
            except Exception as e:
                logger.warning(
                    "pending_watchdog.stale_cleanup_failed",
                    seq=seq,
                    error=e,
                )

    def get_stats(self) -> dict[str, Any]:
        """Get watchdog statistics."""
        with self._lock:
            return {
                "local_pending_count": len(self._local_pending),
                "cleaned_count": self._cleaned_count,
                "is_running": self._is_running,
            }
