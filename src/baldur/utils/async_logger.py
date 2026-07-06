# packages/baldur-python/src/baldur/utils/async_logger.py
"""
Asynchronous healing event logger (Platinum SLA optimization)

Asynchronous event buffering for Zero-Latency Logging
~100ms reduction on the recovery path

Key features:
- Priority Queue-based event processing (CRITICAL first)
- ThreadPoolExecutor-based CRITICAL event processing (prevents thread explosion)
- WAL-First logging (prevents data loss)
- Batch flush retries (exponential backoff)
- Queue size limit and backpressure strategy
- Error-threshold-based automatic alerting
"""

from __future__ import annotations

import queue
import threading
import time
from collections import deque
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from enum import Enum, IntEnum
from typing import TYPE_CHECKING, Any

import structlog

from baldur.settings.batch import get_batch_settings

if TYPE_CHECKING:
    from baldur.audit.wal import WriteAheadLog

__all__ = [
    "AsyncHealingLogger",
    "EventSeverity",
    "LogFlushPriority",
    "PrioritizedEvent",
    "WALPolicy",
    "QueueOverflowPolicy",
    "BatchRetryPolicy",
    "FlushErrorAlertConfig",
]

logger = structlog.get_logger()


# =============================================================================
# Enums and Data Classes
# =============================================================================


class EventSeverity(IntEnum):
    """Event severity (Batch Flush Policy)"""

    DEBUG = 0
    INFO = 1
    WARNING = 2
    CRITICAL = 3  # CB Open, failure detected → send immediately


class LogFlushPriority:
    """Log flush priority constants (lower = higher priority, for PriorityQueue)."""

    CRITICAL = 0
    WARNING = 1
    INFO = 2
    DEBUG = 3


# Severity → Priority mapping
SEVERITY_PRIORITY_MAP: dict[EventSeverity, int] = {
    EventSeverity.CRITICAL: LogFlushPriority.CRITICAL,
    EventSeverity.WARNING: LogFlushPriority.WARNING,
    EventSeverity.INFO: LogFlushPriority.INFO,
    EventSeverity.DEBUG: LogFlushPriority.DEBUG,
}


@dataclass(order=True)
class PrioritizedEvent:
    """Priority-based event wrapper (for PriorityQueue)."""

    priority: int  # lower = higher priority
    timestamp: float = field(compare=False)
    event: dict[str, Any] = field(compare=False)


class WALPolicy(str, Enum):
    """WAL write policy."""

    ALL = "all"  # write all events to WAL
    CRITICAL_ONLY = "critical"  # write only CRITICAL to WAL (recommended)
    NONE = "none"  # no WAL (legacy behavior)


class QueueOverflowPolicy(str, Enum):
    """Queue overflow policy."""

    DROP_NEWEST = "drop_newest"  # drop the new event (default, simple)
    DROP_OLDEST = "drop_oldest"  # drop the oldest event (RingBuffer style)
    BLOCK = "block"  # block (violates Non-blocking)


@dataclass
class BatchRetryPolicy:
    """Batch flush retry policy (exponential backoff)."""

    max_retries: int = 3
    initial_delay_seconds: float = 1.0
    backoff_multiplier: float = 2.0
    max_delay_seconds: float = 30.0
    dlq_on_final_failure: bool = True  # move to DLQ on final failure


@dataclass
class FlushErrorAlertConfig:
    """Flush error alert configuration."""

    threshold_count: int = 10  # threshold (N times)
    window_seconds: float = 60.0  # time window (seconds)
    cooldown_seconds: float = 300.0  # alert cooldown (5 minutes)
    severity: str = "CRITICAL"  # alert level


# =============================================================================
# AsyncHealingLogger
# =============================================================================


class AsyncHealingLogger:
    """
    Asynchronous healing event logger

    Key characteristics:
    - Priority Queue: process CRITICAL events first
    - ThreadPoolExecutor: thread pool for CRITICAL events (prevents thread explosion)
    - WAL-First: write to WAL before the memory queue (prevents data loss)
    - Batch retry: applies exponential backoff
    - Queue size limit: protects memory via a backpressure strategy
    - Error alert: automatic alert when the threshold is exceeded

    Usage:
        def send_to_command_center(events):
            requests.post('http://command-center/events', json=events)

        AsyncHealingLogger.configure(flush_callback=send_to_command_center)
        AsyncHealingLogger.start()

        # Normal event (batch processing)
        AsyncHealingLogger.log({'type': 'retry', 'service': 'payment'})

        # CRITICAL event (priority processing)
        AsyncHealingLogger.log({'type': 'cb_open', 'service': 'payment'}, EventSeverity.CRITICAL)
    """

    # Default queue (replaced by the Priority Queue)
    _queue: queue.Queue | None = None
    _priority_queue: queue.PriorityQueue | None = None

    _running: bool = False
    _worker_thread: threading.Thread | None = None
    _flush_callback: Callable[[list[dict]], None] | None = None
    _lock = threading.RLock()
    _settings_cache: Any | None = None
    _handle: Any | None = None  # DaemonWorkerHandle (impl 489 D9)

    # WAL-related
    _wal: WriteAheadLog | None = None
    _wal_policy: WALPolicy = WALPolicy.CRITICAL_ONLY

    # CRITICAL event thread pool
    _critical_executor: ThreadPoolExecutor | None = None
    CRITICAL_EXECUTOR_MAX_WORKERS: int = 5

    # Queue configuration
    _overflow_policy: QueueOverflowPolicy = QueueOverflowPolicy.DROP_NEWEST
    _max_queue_size: int = 5000

    # Performance 2: atomic counter (used instead of qsize())
    _queue_count: int = 0
    _queue_count_lock = threading.Lock()

    # flush() ↔ _worker() cooperation: full flush including the worker's local batch
    _flush_requested = threading.Event()
    _flush_done = threading.Event()

    # Retry-related
    _retry_policy: BatchRetryPolicy = BatchRetryPolicy()
    _pending_retries: list[tuple[list[dict], int, float]] = []

    # Error-alert-related
    _alert_config: FlushErrorAlertConfig = FlushErrorAlertConfig()
    _error_timestamps: deque = deque(maxlen=100)
    _last_alert_time: float = 0

    # Settings come from BatchSettings
    IMMEDIATE_SEVERITIES = {EventSeverity.CRITICAL}

    # Statistics
    _stats = {
        "events_logged": 0,
        "events_flushed": 0,
        "immediate_flushes": 0,
        "batch_flushes": 0,
        "flush_errors": 0,
        "wal_writes": 0,
        "queue_overflows": 0,
        "pending_retries": 0,
        "dlq_moved": 0,
        "total_retries": 0,
        "alerts_sent": 0,
    }

    @classmethod
    def _get_settings(cls):
        """Get BatchSettings (cached for performance)."""
        if cls._settings_cache is None:
            cls._settings_cache = get_batch_settings()
        return cls._settings_cache

    @classmethod
    def _get_batch_size(cls) -> int:
        """Get batch size from settings."""
        return cls._get_settings().logger_batch_size

    @classmethod
    def _get_flush_interval(cls) -> float:
        """Get flush interval from settings."""
        return cls._get_settings().flush_interval

    # -------------------------------------------------------------------------
    # Configuration
    # -------------------------------------------------------------------------

    @classmethod
    def configure(cls, flush_callback: Callable[[list[dict]], None]) -> None:
        """
        Set the batch send callback

        Args:
            flush_callback: function that receives batched events and sends them to the command center
        """
        with cls._lock:
            cls._flush_callback = flush_callback

    @classmethod
    def configure_wal(
        cls,
        wal: WriteAheadLog,
        policy: WALPolicy = WALPolicy.CRITICAL_ONLY,
    ) -> None:
        """
        Set the WAL instance and policy.

        Args:
            wal: WriteAheadLog instance
            policy: WAL write policy
        """
        with cls._lock:
            cls._wal = wal
            cls._wal_policy = policy
        logger.info(
            "async_healing_logger.wal_configured",
            policy=policy.value,
        )

    @classmethod
    def configure_queue(
        cls,
        max_size: int = 5000,
        overflow_policy: QueueOverflowPolicy = QueueOverflowPolicy.DROP_NEWEST,
    ) -> None:
        """
        Set the queue.

        Args:
            max_size: maximum queue size
            overflow_policy: overflow policy
        """
        with cls._lock:
            cls._max_queue_size = max_size
            cls._overflow_policy = overflow_policy
        logger.debug(
            "async_healing_logger.queue_configured",
            max_size=max_size,
            overflow_policy=overflow_policy.value,
        )

    @classmethod
    def configure_retry(cls, policy: BatchRetryPolicy) -> None:
        """
        Set the retry policy.

        Args:
            policy: batch retry policy
        """
        with cls._lock:
            cls._retry_policy = policy
        logger.debug(
            "async_healing_logger.retry_policy_configured",
            policy=policy.max_retries,
        )

    @classmethod
    def configure_alert(cls, config: FlushErrorAlertConfig) -> None:
        """
        Set the error alert.

        Args:
            config: flush error alert configuration
        """
        with cls._lock:
            cls._alert_config = config
        logger.debug(
            "async_healing_logger.alert_configured",
            alert_threshold_count=config.threshold_count,
        )

    # -------------------------------------------------------------------------
    # Lifecycle
    # -------------------------------------------------------------------------

    @classmethod
    def start(cls) -> None:
        """Start the background worker"""
        with cls._lock:
            if cls._running:
                return
            cls._running = True

            # Load max_queue_size from settings
            try:
                settings = cls._get_settings()
                cls._max_queue_size = getattr(
                    settings, "async_logger_max_queue_size", 5000
                )
            except Exception:
                pass

            # Initialize the Priority Queue (no size limit, managed separately)
            cls._priority_queue = queue.PriorityQueue()
            cls._queue = queue.Queue(
                maxsize=cls._max_queue_size
            )  # kept for compatibility

            # Create the dedicated thread pool for CRITICAL events
            cls._critical_executor = ThreadPoolExecutor(
                max_workers=cls.CRITICAL_EXECUTOR_MAX_WORKERS,
                thread_name_prefix="CriticalAuditFlush",
            )

            cls._pending_retries = []

            from baldur.meta.daemon_worker import DaemonWorkerHandle
            from baldur.metrics.recorders.daemon_worker import (
                register_daemon_worker,
            )

            cls._spawn_worker_thread()
            assert cls._worker_thread is not None  # populated by _spawn_worker_thread
            cls._handle = DaemonWorkerHandle(
                thread=cls._worker_thread,
                tick_interval_seconds=cls._get_flush_interval(),
                restart_callback=cls._spawn_worker_thread,
            )
            register_daemon_worker("AsyncHealingLogger", cls._handle)
            logger.debug("async_healing_logger.background_worker_started")

    @classmethod
    def _spawn_worker_thread(cls) -> None:
        """Construct + start a fresh worker thread (impl 489 D9)."""
        cls._worker_thread = threading.Thread(
            target=cls._worker_with_crash_capture,
            daemon=True,
            name="AsyncHealingLogger",
        )
        cls._worker_thread.start()
        if cls._handle is not None:
            cls._handle.thread = cls._worker_thread

    @classmethod
    def _worker_with_crash_capture(cls) -> None:
        try:
            cls._worker()
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException as e:
            if cls._handle is not None:
                cls._handle.record_crash(e)
            raise

    @classmethod
    def stop(cls, timeout: float | None = None) -> None:
        """Stop the background worker"""
        from baldur.metrics.recorders.daemon_worker import unregister_daemon_worker

        if timeout is None:
            from baldur.settings.thread_management import (
                get_thread_management_settings,
            )

            timeout = get_thread_management_settings().join_timeout
        with cls._lock:
            if not cls._running:
                return
            if cls._handle is not None:
                cls._handle.is_stopping = True
            cls._running = False

        if cls._worker_thread:
            cls._worker_thread.join(timeout=timeout)
            unregister_daemon_worker("AsyncHealingLogger")
            if cls._worker_thread.is_alive():
                logger.critical(
                    "daemon_worker.stop_join_timeout",
                    worker_name="AsyncHealingLogger",
                    join_timeout_seconds=timeout,
                )

        # Shut down the thread pool
        if cls._critical_executor:
            cls._critical_executor.shutdown(wait=True, cancel_futures=False)
            cls._critical_executor = None

        logger.debug("async_healing_logger.background_worker_stopped")

    # -------------------------------------------------------------------------
    # WAL Support
    # -------------------------------------------------------------------------

    @classmethod
    def _should_write_to_wal(cls, severity: EventSeverity) -> bool:
        """Decide whether to write to WAL."""
        if cls._wal_policy == WALPolicy.ALL:
            return True
        if cls._wal_policy == WALPolicy.CRITICAL_ONLY:
            return severity in cls.IMMEDIATE_SEVERITIES
        return False

    # -------------------------------------------------------------------------
    # Logging
    # -------------------------------------------------------------------------

    @classmethod
    def log(
        cls, event: dict[str, Any], severity: EventSeverity = EventSeverity.INFO
    ) -> None:
        """
        Log an event (non-blocking, ~0.01ms)

        Processing order:
        1. WAL-First: write to WAL first (per configuration)
        2. Add to the Priority Queue (CRITICAL first)

        Args:
            event: healing event dictionary
            severity: event severity
        """
        enriched_event = {
            **event,
            "severity": severity.name,
            "timestamp": time.time(),
        }

        # WAL-First: write to WAL before the memory queue
        wal_seq = -1
        if cls._wal and cls._should_write_to_wal(severity):
            try:
                wal_seq = cls._wal.write(enriched_event)
                with cls._lock:
                    cls._stats["wal_writes"] += 1
            except Exception as e:
                logger.warning(
                    "async_healing_logger.wal_write_failed",
                    error=e,
                )

        enriched_event["_wal_seq"] = wal_seq

        with cls._lock:
            cls._stats["events_logged"] += 1

        # Determine the priority
        priority = SEVERITY_PRIORITY_MAP.get(severity, LogFlushPriority.INFO)
        prioritized = PrioritizedEvent(
            priority=priority,
            timestamp=time.time(),
            event=enriched_event,
        )

        if severity in cls.IMMEDIATE_SEVERITIES:
            # CRITICAL: use the thread pool (prevents thread explosion)
            if cls._critical_executor:
                cls._critical_executor.submit(cls._flush_immediate, [enriched_event])
            else:
                # Fallback: create a thread directly when the thread pool is uninitialized
                threading.Thread(
                    target=cls._flush_immediate, args=([enriched_event],), daemon=True
                ).start()
        else:
            # Normal: add to the Priority Queue (apply backpressure)
            cls._enqueue_with_backpressure(prioritized)

    @classmethod
    def _enqueue_with_backpressure(cls, prioritized: PrioritizedEvent) -> None:
        """Add to the queue applying the backpressure strategy (Performance 2: uses the atomic counter)."""
        if cls._priority_queue is None:
            return

        try:
            # Performance 2: check size via the atomic counter instead of qsize() (minimizes Lock scope)
            with cls._queue_count_lock:
                current_count = cls._queue_count
                is_full = current_count >= cls._max_queue_size

                if is_full:
                    cls._stats["queue_overflows"] += 1

                    if cls._overflow_policy == QueueOverflowPolicy.DROP_NEWEST:
                        logger.warning(
                            "async_healing_logger.queue_full_dropping_newest"
                        )
                        return
                    if cls._overflow_policy == QueueOverflowPolicy.DROP_OLDEST:
                        # Performance 2: perform get + put atomically
                        try:
                            cls._priority_queue.get_nowait()
                            # counter stays the same (get then put)
                        except queue.Empty:
                            pass
                        logger.warning(
                            "async_healing_logger.queue_full_dropping_oldest"
                        )
                        # DROP_OLDEST puts below
                    # BLOCK uses put() (not recommended as it violates Non-blocking)

                # Add to the queue and increment the counter
                cls._priority_queue.put_nowait(prioritized)
                if not is_full:
                    cls._queue_count += 1

        except queue.Full:
            with cls._lock:
                cls._stats["queue_overflows"] += 1
            logger.warning("async_healing_logger.queue_full_event_dropped")

    @classmethod
    def flush(cls) -> None:
        """Manual flush (send all pending events immediately).

        If the worker thread is running, request a flush to the worker and wait for completion.
        Performs a full flush including events already dequeued into the worker's local batch.
        """
        if cls._running and cls._worker_thread is not None:
            # Request a flush to the worker → the worker flushes its local batch + queue remainder
            cls._flush_done.clear()
            cls._flush_requested.set()
            cls._flush_done.wait(timeout=5.0)
            return

        # Drain directly when the worker is not running (legacy logic)
        events = []
        extracted_count = 0

        if cls._priority_queue:
            while not cls._priority_queue.empty():
                try:
                    prioritized = cls._priority_queue.get_nowait()
                    events.append(prioritized.event)
                    extracted_count += 1
                except queue.Empty:
                    break

        if cls._queue:
            while not cls._queue.empty():
                try:
                    events.append(cls._queue.get_nowait())
                    extracted_count += 1
                except queue.Empty:
                    break

        if extracted_count > 0:
            with cls._queue_count_lock:
                cls._queue_count = max(0, cls._queue_count - extracted_count)

        if events:
            cls._flush_batch(events)

    @classmethod
    def get_stats(cls) -> dict[str, int]:
        """Look up logger statistics"""
        with cls._lock:
            stats = cls._stats.copy()
            # Performance 2: use the atomic counter
            with cls._queue_count_lock:
                stats["current_queue_size"] = cls._queue_count
            return stats

    @classmethod
    def reset_stats(cls) -> None:
        """Reset statistics"""
        with cls._lock:
            cls._stats = {
                "events_logged": 0,
                "events_flushed": 0,
                "immediate_flushes": 0,
                "batch_flushes": 0,
                "flush_errors": 0,
                "wal_writes": 0,
                "queue_overflows": 0,
                "pending_retries": 0,
                "dlq_moved": 0,
                "total_retries": 0,
                "alerts_sent": 0,
            }

    # -------------------------------------------------------------------------
    # Worker
    # -------------------------------------------------------------------------

    @classmethod
    def _worker(cls) -> None:  # noqa: C901, PLR0912
        """Priority Queue-based batch processing worker."""
        batch: list[dict] = []
        critical_batch: list[dict] = []
        last_flush = time.time()

        while cls._running:
            iter_start = time.monotonic()
            # 1. Process pending retries
            cls._process_pending_retries()

            # 2. Extract an event from the Priority Queue
            try:
                if cls._priority_queue:
                    prioritized = cls._priority_queue.get(timeout=0.5)

                    # Performance 2: decrement the counter
                    with cls._queue_count_lock:
                        cls._queue_count = max(0, cls._queue_count - 1)

                    if prioritized.priority == LogFlushPriority.CRITICAL:
                        # CRITICAL is processed immediately in a separate batch
                        critical_batch.append(prioritized.event)
                    else:
                        batch.append(prioritized.event)
            except queue.Empty:
                pass

            # 3. Flush the CRITICAL batch immediately
            if critical_batch:
                cls._flush_batch(critical_batch)
                critical_batch = []

            # 4. Handle a flush() request: flush both the queue remainder and the local batch
            if cls._flush_requested.is_set():
                # Drain remaining queue events into the local batch as well
                if cls._priority_queue:
                    while not cls._priority_queue.empty():
                        try:
                            p = cls._priority_queue.get_nowait()
                            with cls._queue_count_lock:
                                cls._queue_count = max(0, cls._queue_count - 1)
                            batch.append(p.event)
                        except queue.Empty:
                            break
                if batch:
                    cls._flush_batch(batch)
                    batch = []
                    last_flush = time.time()
                cls._flush_requested.clear()
                cls._flush_done.set()
                continue

            # 5. Conditionally flush the normal batch
            if cls._should_flush(batch, last_flush):
                cls._flush_batch(batch)
                batch = []
                last_flush = time.time()

            if cls._handle is not None:
                cls._handle.observe_iteration(time.monotonic() - iter_start)
                cls._handle.heartbeat()

        # On shutdown, process remaining events
        if critical_batch:
            cls._flush_batch(critical_batch)
        if batch:
            cls._flush_batch(batch)

        # Process remaining retries as well
        for events, attempt, _ in cls._pending_retries:
            cls._flush_with_retry(events, attempt)

    @classmethod
    def _should_flush(cls, batch: list[dict], last_flush: float) -> bool:
        """Check the batch flush condition."""
        if not batch:
            return False

        batch_size = cls._get_batch_size()
        flush_interval = cls._get_flush_interval()

        return len(batch) >= batch_size or (time.time() - last_flush >= flush_interval)

    # -------------------------------------------------------------------------
    # Flush with Retry
    # -------------------------------------------------------------------------

    @classmethod
    def _flush_batch(cls, events: list[dict]) -> None:
        """Batch flush (with retry support)."""
        cls._flush_with_retry(events, attempt=0)

    @classmethod
    def _flush_with_retry(cls, events: list[dict], attempt: int) -> None:
        """Batch flush with exponential backoff."""
        if not cls._flush_callback or not events:
            return

        try:
            cls._flush_callback(events)
            with cls._lock:
                cls._stats["events_flushed"] += len(events)
                cls._stats["batch_flushes"] += 1
            logger.debug(
                "async_healing_logger.flushed_events",
                events_count=len(events),
            )

        except Exception as e:
            with cls._lock:
                cls._stats["flush_errors"] += 1
                cls._error_timestamps.append(time.time())

            # Alert check
            cls._check_and_send_alert()

            if attempt < cls._retry_policy.max_retries:
                # Schedule a retry
                delay = min(
                    cls._retry_policy.initial_delay_seconds
                    * (cls._retry_policy.backoff_multiplier**attempt),
                    cls._retry_policy.max_delay_seconds,
                )
                next_retry = time.time() + delay

                with cls._lock:
                    cls._pending_retries.append((events, attempt + 1, next_retry))
                    cls._stats["pending_retries"] = len(cls._pending_retries)
                    cls._stats["total_retries"] += 1

                logger.warning(
                    "async_healing_logger.flush_failed_retry_after",
                    retry_attempt_index=attempt + 1,
                    cls=cls._retry_policy.max_retries,
                    delay=delay,
                    error=e,
                )
            else:
                # Final failure
                logger.exception(
                    "async_healing_logger.flush_failed_after_retries",
                    attempt=attempt,
                    error=e,
                )

                if cls._retry_policy.dlq_on_final_failure:
                    cls._move_to_dlq(events, str(e))
                else:
                    # If there is a WAL sequence, the SyncWorker reprocesses it
                    logger.warning("async_healing_logger.events_lost_no_dlq")

    @classmethod
    def _process_pending_retries(cls) -> None:
        """Process pending retries (called periodically by the worker)."""
        now = time.time()
        remaining = []

        with cls._lock:
            retries = cls._pending_retries[:]
            cls._pending_retries = []

        for events, attempt, next_retry in retries:
            if now >= next_retry:
                cls._flush_with_retry(events, attempt)
            else:
                remaining.append((events, attempt, next_retry))

        with cls._lock:
            cls._pending_retries.extend(remaining)
            cls._stats["pending_retries"] = len(cls._pending_retries)

    @classmethod
    def _move_to_dlq(cls, events: list[dict], error_message: str) -> None:
        """Move finally-failed events to the DLQ."""
        try:
            # DLQStoreOperations was decomposed into StoreOperationsMixin
            # (part of the unified DLQService). Resolve via ProviderRegistry
            # so OSS deployments without baldur_pro fall open gracefully.
            from baldur.factory.registry import ProviderRegistry

            dlq = ProviderRegistry.dlq_service.safe_get()
            if dlq is None:
                logger.warning("async_logger.dlq_unavailable_skipping_persist")
                return
            for event in events:
                dlq.store(
                    source="AsyncHealingLogger",
                    payload=event,
                    error_message=error_message,
                    max_retries=0,
                )

            with cls._lock:
                cls._stats["dlq_moved"] += len(events)

            logger.info(
                "async_healing_logger.moved_events_dlq",
                events_count=len(events),
            )

        except ImportError:
            logger.warning("async_healing_logger.dlq_available_events_lost")
        except Exception as e:
            logger.exception(
                "async_healing_logger.dlq_store_failed",
                error=e,
            )

    # -------------------------------------------------------------------------
    # Alert
    # -------------------------------------------------------------------------

    @classmethod
    def _check_and_send_alert(cls) -> None:
        """Check the error threshold and send an alert."""
        now = time.time()

        # Cooldown check
        if now - cls._last_alert_time < cls._alert_config.cooldown_seconds:
            return

        # Count errors within the time window
        window_start = now - cls._alert_config.window_seconds
        recent_errors = sum(1 for ts in cls._error_timestamps if ts >= window_start)

        if recent_errors >= cls._alert_config.threshold_count:
            cls._send_flush_error_alert(recent_errors)
            cls._last_alert_time = now

    @classmethod
    def _send_flush_error_alert(cls, error_count: int) -> None:
        """Send an alert via UnifiedNotificationManager."""
        try:
            from baldur.models.notification import (
                NotificationPayload,
                NotificationPriority,
            )
            from baldur_pro.services.unified_notification import (
                UnifiedNotificationManager,
            )

            severity_str = str(cls._alert_config.severity).upper()
            try:
                priority = NotificationPriority[severity_str]
            except KeyError:
                priority = NotificationPriority.HIGH

            manager = UnifiedNotificationManager()
            manager.notify(
                NotificationPayload(
                    title="[AsyncAuditLogger] Flush error threshold exceeded",
                    message=(
                        f"{cls._alert_config.window_seconds}s window: {error_count} flush failures. "
                        f"Risk of data loss - immediate attention required"
                    ),
                    priority=priority,
                    source="AsyncHealingLogger",
                    metadata={
                        "error_count": error_count,
                        "threshold": cls._alert_config.threshold_count,
                        "window_seconds": cls._alert_config.window_seconds,
                        "queue_size": cls._priority_queue.qsize()
                        if cls._priority_queue
                        else 0,
                    },
                )
            )

            with cls._lock:
                cls._stats["alerts_sent"] += 1

            logger.info(
                "async_healing_logger.flush_error_alert_sent",
                error_count=error_count,
            )

        except ImportError:
            logger.warning("async_healing_logger.unifiednotificationmanager_available")
        except Exception as e:
            logger.exception(
                "async_healing_logger.send_alert_failed",
                error=e,
            )

    # -------------------------------------------------------------------------
    # Immediate Flush
    # -------------------------------------------------------------------------

    @classmethod
    def _flush_immediate(cls, events: list[dict]) -> None:
        """Send immediately (for CRITICAL events)"""
        if not cls._flush_callback or not events:
            return

        try:
            cls._flush_callback(events)
            with cls._lock:
                cls._stats["events_flushed"] += len(events)
                cls._stats["immediate_flushes"] += 1
            logger.debug(
                "async_healing_logger.flushed_events_immediate",
                events_count=len(events),
            )
        except Exception as e:
            with cls._lock:
                cls._stats["flush_errors"] += 1
                cls._error_timestamps.append(time.time())
            logger.warning(
                "async_healing_logger.immediate_flush_failed",
                error=e,
            )
            cls._check_and_send_alert()

    # -------------------------------------------------------------------------
    # Reset
    # -------------------------------------------------------------------------

    @classmethod
    def reset(cls) -> None:
        """Reset state (for tests)"""
        cls.stop()
        with cls._lock:
            cls._queue = None
            cls._priority_queue = None
            cls._running = False
            cls._worker_thread = None
            cls._flush_callback = None
            cls._wal = None
            cls._wal_policy = WALPolicy.CRITICAL_ONLY
            cls._critical_executor = None
            cls._pending_retries = []
            cls._error_timestamps = deque(maxlen=100)
            cls._last_alert_time = 0
            cls._flush_requested.clear()
            cls._flush_done.clear()
            cls._stats = {
                "events_logged": 0,
                "events_flushed": 0,
                "immediate_flushes": 0,
                "batch_flushes": 0,
                "flush_errors": 0,
                "wal_writes": 0,
                "queue_overflows": 0,
                "pending_retries": 0,
                "dlq_moved": 0,
                "total_retries": 0,
                "alerts_sent": 0,
            }
