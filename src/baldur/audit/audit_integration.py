"""
Audit Integration Module.

Integration wiring:
1. ContinuousAuditRecorder ↔ CircuitBreaker reinforced wiring
2. ContinuousAuditRecorder ↔ SyslogFallback automatic wiring
3. AsyncLogger ↔ ContinuousAudit adapter

Design:
    - Adapter pattern: wire things up without modifying existing code
    - Observer pattern: propagate events to both sides as they occur
    - Non-blocking: no performance impact on the main application

Usage:
    from baldur.audit.audit_integration import (
        IntegratedAuditRecorder,
        AsyncLoggerAdapter,
        configure_integration,
    )

    # Integrated setup
    recorder = IntegratedAuditRecorder(adapter)

    # Or attach AsyncLogger to an existing ResilientRecorder
    async_adapter = AsyncLoggerAdapter(flush_callback=send_to_command_center)
    recorder.attach_async_logger(async_adapter)
"""

from __future__ import annotations

import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING, Any

import structlog

from baldur.interfaces.audit_adapter import AuditEntry

if TYPE_CHECKING:
    from baldur.audit.resilient_recorder import ResilientContinuousAuditRecorder
    from baldur.settings.batch import BatchSettings

logger = structlog.get_logger()


# =============================================================================
# Event Severity - single source is utils/async_logger.py (Item 4 dedup)
# =============================================================================

from baldur.utils.async_logger import EventSeverity  # noqa: E402, F401
from baldur.utils.time import utc_now

# =============================================================================
# AsyncLogger Adapter
# =============================================================================


@dataclass
class AsyncLoggerConfig:
    """AsyncLogger adapter configuration."""

    batch_size: int = 5
    flush_interval_seconds: float = 2.0
    max_queue_size: int = 5000
    immediate_severities: set[EventSeverity] = field(
        default_factory=lambda: {EventSeverity.CRITICAL, EventSeverity.WARNING}
    )

    @classmethod
    def from_settings(
        cls,
        settings: BatchSettings | None = None,
        **overrides,
    ) -> AsyncLoggerConfig:
        """
        Build an AsyncLoggerConfig instance from settings.

        Args:
            settings: BatchSettings instance (uses the singleton when omitted)
            **overrides: individual field overrides

        Returns:
            AsyncLoggerConfig: settings-based instance
        """
        from baldur.settings.batch import get_batch_settings

        s = settings or get_batch_settings()
        return cls(
            batch_size=overrides.get("batch_size", s.async_logger_batch_size),
            flush_interval_seconds=overrides.get(
                "flush_interval_seconds", s.async_logger_flush_interval
            ),
            max_queue_size=overrides.get(
                "max_queue_size", s.async_logger_max_queue_size
            ),
            immediate_severities=overrides.get(
                "immediate_severities",
                {EventSeverity.CRITICAL, EventSeverity.WARNING},
            ),
        )


class AsyncLoggerAdapter:
    """
    AsyncHealingLogger-compatible adapter.

    Provides the same interface as the AsyncHealingLogger in
    load_tests/utils/baldur/async_logger.py, while remaining usable
    standalone inside the baldur package.

    Main features:
    1. Asynchronous event buffering (non-blocking)
    2. Immediate delivery of CRITICAL/WARNING events
    3. Batching for network efficiency
    4. Thread-safe operation
    """

    def __init__(
        self,
        flush_callback: Callable[[list[dict[str, Any]]], None] | None = None,
        config: AsyncLoggerConfig | None = None,
    ):
        """
        Initialize AsyncLoggerAdapter.

        Args:
            flush_callback: function that receives and sends batched events
            config: adapter configuration
        """
        self._flush_callback = flush_callback
        self._config = config or AsyncLoggerConfig()

        # Queue
        self._queue: queue.Queue = queue.Queue(maxsize=self._config.max_queue_size)

        # Thread control
        self._running = False
        self._worker_thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._handle: Any | None = None  # DaemonWorkerHandle (impl 489 D9)

        # Statistics
        self._stats = {
            "events_logged": 0,
            "events_flushed": 0,
            "immediate_flushes": 0,
            "batch_flushes": 0,
            "flush_errors": 0,
            "queue_overflows": 0,
        }

    def configure(
        self,
        flush_callback: Callable[[list[dict[str, Any]]], None],
        batch_size: int = 5,
        flush_interval: float = 2.0,
        max_queue_size: int = 5000,
    ) -> None:
        """
        Change the configuration at runtime.

        Args:
            flush_callback: batch delivery callback
            batch_size: batch size
            flush_interval: flush interval (seconds)
            max_queue_size: maximum queue size
        """
        with self._lock:
            self._flush_callback = flush_callback
            self._config.batch_size = batch_size
            self._config.flush_interval_seconds = flush_interval
            # Queue size cannot change at runtime (needs recreation)

    def start(self) -> None:
        """Start the background worker."""
        from baldur.meta.daemon_worker import DaemonWorkerHandle
        from baldur.metrics.recorders.daemon_worker import register_daemon_worker

        with self._lock:
            if self._running:
                return

            self._running = True
            self._spawn_thread()
            assert self._worker_thread is not None  # _spawn_thread() invariant
            self._handle = DaemonWorkerHandle(
                thread=self._worker_thread,
                tick_interval_seconds=self._config.flush_interval_seconds,
                restart_callback=self._spawn_thread,
            )
            register_daemon_worker("AsyncLoggerAdapter", self._handle)
            logger.info("async_logger_adapter.worker_started")

    def _spawn_thread(self) -> None:
        """Construct + start a fresh worker thread (impl 489 D9)."""
        self._worker_thread = threading.Thread(
            target=self._worker_loop_with_crash_capture,
            daemon=True,
            name="AsyncLoggerAdapter",
        )
        self._worker_thread.start()
        if self._handle is not None:
            self._handle.thread = self._worker_thread

    def _worker_loop_with_crash_capture(self) -> None:
        try:
            self._worker_loop()
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException as e:
            if self._handle is not None:
                self._handle.record_crash(e)
            raise

    def stop(self, timeout: float | None = None) -> None:
        """Stop the background worker."""
        from baldur.metrics.recorders.daemon_worker import unregister_daemon_worker

        if timeout is None:
            from baldur.settings.thread_management import (
                get_thread_management_settings,
            )

            timeout = get_thread_management_settings().join_timeout
        with self._lock:
            if not self._running:
                return
            if self._handle is not None:
                self._handle.is_stopping = True
            self._running = False

        if self._worker_thread:
            self._worker_thread.join(timeout=timeout)

        unregister_daemon_worker("AsyncLoggerAdapter")
        if self._worker_thread is not None and self._worker_thread.is_alive():
            logger.critical(
                "daemon_worker.stop_join_timeout",
                worker_name="AsyncLoggerAdapter",
                join_timeout_seconds=timeout,
            )
        logger.info("async_logger_adapter.worker_stopped")

    def log(
        self,
        event: dict[str, Any],
        severity: EventSeverity = EventSeverity.INFO,
    ) -> bool:
        """
        Log an event (non-blocking, ~0.01ms).

        Args:
            event: event dictionary
            severity: event severity

        Returns:
            True if queued/sent, False if dropped
        """
        enriched_event = {
            **event,
            "severity": severity.name,
            "timestamp": time.time(),
            "timestamp_iso": utc_now().isoformat(),
        }

        self._stats["events_logged"] += 1

        if severity in self._config.immediate_severities:
            # CRITICAL/WARNING: send immediately (on a separate thread)
            self._stats["immediate_flushes"] += 1
            threading.Thread(
                target=self._flush_immediate,
                args=([enriched_event],),
                daemon=True,
                name="ImmediateFlush",
            ).start()
            return True
        # Normal: wait for the batch
        try:
            self._queue.put_nowait(enriched_event)
            return True
        except queue.Full:
            self._stats["queue_overflows"] += 1
            logger.warning("async_logger_adapter.queue_full_event_dropped")
            return False

    def log_cb_event(
        self,
        service: str,
        state: str,
        reason: str = "",
        **kwargs,
    ) -> None:
        """Log a Circuit Breaker event."""
        severity = (
            EventSeverity.CRITICAL
            if state in ["OPEN", "BLOCKED"]
            else EventSeverity.INFO
        )
        self.log(
            {
                "type": "circuit_breaker",
                "service": service,
                "state": state,
                "reason": reason,
                **kwargs,
            },
            severity,
        )

    def log_recovery_event(
        self,
        service: str,
        recovery_time_ms: float,
        success: bool = True,
        **kwargs,
    ) -> None:
        """Log a recovery event."""
        self.log(
            {
                "type": "recovery",
                "service": service,
                "recovery_time_ms": recovery_time_ms,
                "success": success,
                **kwargs,
            },
            EventSeverity.INFO,
        )

    def log_emergency_event(
        self,
        level: str,
        action: str,
        reason: str = "",
        **kwargs,
    ) -> None:
        """Log an Emergency/Fallback event."""
        severity = (
            EventSeverity.CRITICAL if action == "trigger" else EventSeverity.WARNING
        )
        self.log(
            {
                "type": "emergency",
                "level": level,
                "action": action,
                "reason": reason,
                **kwargs,
            },
            severity,
        )

    def log_fallback_activated(
        self,
        fallback_type: str,
        reason: str,
        **kwargs,
    ) -> None:
        """Log a fallback activation event."""
        self.log(
            {
                "type": "fallback_activated",
                "fallback_type": fallback_type,
                "reason": reason,
                **kwargs,
            },
            EventSeverity.WARNING,
        )

    def log_audit_event(
        self,
        action: str,
        success: bool,
        audit_id: str = "",
        **kwargs,
    ) -> None:
        """Log an audit event (for ContinuousAudit integration)."""
        severity = EventSeverity.INFO if success else EventSeverity.WARNING
        self.log(
            {
                "type": "audit",
                "action": action,
                "success": success,
                "audit_id": audit_id,
                **kwargs,
            },
            severity,
        )

    def flush_now(self) -> int:
        """Flush immediately, by hand."""
        batch = []
        while not self._queue.empty():
            try:
                batch.append(self._queue.get_nowait())
            except queue.Empty:
                break

        if batch:
            self._flush_batch(batch)
        return len(batch)

    def get_stats(self) -> dict[str, Any]:
        """Return statistics."""
        return {
            **self._stats,
            "queue_size": self._queue.qsize(),
            "is_running": self._running,
        }

    def reset_stats(self) -> None:
        """Reset statistics."""
        self._stats = {
            "events_logged": 0,
            "events_flushed": 0,
            "immediate_flushes": 0,
            "batch_flushes": 0,
            "flush_errors": 0,
            "queue_overflows": 0,
        }

    def _worker_loop(self) -> None:
        """Batch-processing worker."""
        batch: list[dict[str, Any]] = []
        last_flush = time.time()

        while self._running:
            iter_start = time.monotonic()
            try:
                event = self._queue.get(timeout=1.0)
                batch.append(event)
            except queue.Empty:
                pass

            # Send once the batch size is reached or the interval has elapsed
            should_flush = len(batch) >= self._config.batch_size or (
                batch
                and time.time() - last_flush >= self._config.flush_interval_seconds
            )

            if should_flush:
                self._flush_batch(batch)
                batch = []
                last_flush = time.time()

            if self._handle is not None:
                self._handle.observe_iteration(time.monotonic() - iter_start)
                self._handle.heartbeat()

        # Handle events left over at shutdown
        if batch:
            self._flush_batch(batch)

        # Also handle events still sitting in the queue
        remaining = []
        while not self._queue.empty():
            try:
                remaining.append(self._queue.get_nowait())
            except queue.Empty:
                break
        if remaining:
            self._flush_batch(remaining)

    def _flush_batch(self, events: list[dict[str, Any]]) -> None:
        """Send a batch."""
        if not self._flush_callback or not events:
            return

        try:
            self._flush_callback(events)
            self._stats["events_flushed"] += len(events)
            self._stats["batch_flushes"] += 1
            logger.debug(
                "async_logger_adapter.flushed_events",
                events_count=len(events),
            )
        except Exception as e:
            self._stats["flush_errors"] += 1
            logger.exception(
                "async_logger_adapter.flush_failed",
                error=e,
            )

    def _flush_immediate(self, events: list[dict[str, Any]]) -> None:
        """Send immediately."""
        self._flush_batch(events)


# =============================================================================
# Audit Event Observer
# =============================================================================


class AuditObserverEventType(str, Enum):
    """Audit event type."""

    # Record events
    RECORD_SUCCESS = "record_success"
    RECORD_FAILED = "record_failed"

    # Circuit breaker events
    CIRCUIT_OPENED = "circuit_opened"
    CIRCUIT_CLOSED = "circuit_closed"
    CIRCUIT_HALF_OPEN = "circuit_half_open"

    # Fallback events
    FALLBACK_ACTIVATED = "fallback_activated"
    FALLBACK_FAILED = "fallback_failed"
    SYSLOG_ACTIVATED = "syslog_activated"

    # Recovery events
    PRIMARY_RECOVERED = "primary_recovered"

    # Health events
    BUFFER_OVERFLOW = "buffer_overflow"
    DEGRADED_MODE_ENTERED = "degraded_mode_entered"
    DEGRADED_MODE_EXITED = "degraded_mode_exited"


@dataclass
class AuditEventData:
    """Audit event data."""

    event_type: AuditObserverEventType
    timestamp: datetime = field(default_factory=lambda: utc_now())
    details: dict[str, Any] = field(default_factory=dict)


class AuditEventObserver:
    """
    Audit event observer interface.

    Propagates audit-system events outward via the Observer pattern.
    """

    def on_event(self, event: AuditEventData) -> None:
        """Called when an event is received."""
        raise NotImplementedError


class AsyncLoggerObserver(AuditEventObserver):
    """
    Wraps an AsyncLoggerAdapter as an Observer.

    Converts audit events into AsyncLogger events and sends them.
    """

    def __init__(self, async_logger: AsyncLoggerAdapter):
        self._async_logger = async_logger

    def on_event(self, event: AuditEventData) -> None:
        """Propagate an audit event to AsyncLogger."""
        event_type = event.event_type

        # Conversion per event type
        if event_type == AuditObserverEventType.CIRCUIT_OPENED:
            self._async_logger.log_cb_event(
                service=event.details.get("service", "audit_primary"),
                state="OPEN",
                reason=event.details.get("reason", ""),
            )
        elif event_type == AuditObserverEventType.CIRCUIT_CLOSED:
            self._async_logger.log_cb_event(
                service=event.details.get("service", "audit_primary"),
                state="CLOSED",
            )
        elif event_type == AuditObserverEventType.FALLBACK_ACTIVATED:
            self._async_logger.log_fallback_activated(
                fallback_type=event.details.get("fallback_type", "file"),
                reason=event.details.get("reason", "primary_failed"),
            )
        elif event_type == AuditObserverEventType.SYSLOG_ACTIVATED:
            self._async_logger.log_emergency_event(
                level="CRITICAL",
                action="trigger",
                reason="all_backends_failed",
            )
        elif event_type == AuditObserverEventType.PRIMARY_RECOVERED:
            self._async_logger.log_recovery_event(
                service=event.details.get("service", "audit_primary"),
                recovery_time_ms=event.details.get("recovery_time_ms", 0),
                success=True,
            )
        elif event_type == AuditObserverEventType.DEGRADED_MODE_ENTERED:
            self._async_logger.log_emergency_event(
                level="WARNING",
                action="trigger",
                reason="degraded_mode",
            )
        elif event_type == AuditObserverEventType.RECORD_SUCCESS:
            self._async_logger.log_audit_event(
                action=event.details.get("action", "unknown"),
                success=True,
                audit_id=event.details.get("audit_id", ""),
            )
        elif event_type == AuditObserverEventType.RECORD_FAILED:
            self._async_logger.log_audit_event(
                action=event.details.get("action", "unknown"),
                success=False,
                audit_id=event.details.get("audit_id", ""),
                error=event.details.get("error", ""),
            )


# =============================================================================
# Integrated Audit Recorder
# =============================================================================


class IntegratedAuditRecorder:
    """
    Integrated audit recorder.

    Adds the following on top of the existing
    ResilientContinuousAuditRecorder:
    1. Outward propagation of CircuitBreaker state-change events
    2. Reinforced automatic SyslogFallback wiring
    3. AsyncLogger integration support (Observer pattern)

    Usage:
        recorder = IntegratedAuditRecorder(adapter)
        async_adapter = AsyncLoggerAdapter(flush_callback=send_to_server)
        recorder.attach_async_logger(async_adapter)

        # record() now propagates to both sides automatically
        recorder.record(entry)
    """

    def __init__(
        self,
        resilient_recorder: ResilientContinuousAuditRecorder,
        enable_auto_async_logging: bool = True,
    ):
        """
        Initialize IntegratedAuditRecorder.

        Args:
            resilient_recorder: existing ResilientContinuousAuditRecorder instance
            enable_auto_async_logging: enable automatic AsyncLogger logging
        """
        self._recorder = resilient_recorder
        self._enable_auto_async_logging = enable_auto_async_logging

        # Observer list
        self._observers: list[AuditEventObserver] = []
        self._observers_lock = threading.Lock()

        # AsyncLogger adapter (optional)
        self._async_logger: AsyncLoggerAdapter | None = None

        # Circuit state tracking
        self._last_circuit_state = None

    def attach_observer(self, observer: AuditEventObserver) -> None:
        """Register an observer."""
        with self._observers_lock:
            self._observers.append(observer)
            logger.debug(
                "integrated_recorder.observer_attached",
                adapter_type=type(observer).__name__,
            )

    def detach_observer(self, observer: AuditEventObserver) -> None:
        """Unregister an observer."""
        with self._observers_lock:
            if observer in self._observers:
                self._observers.remove(observer)
                logger.debug(
                    "integrated_recorder.observer_detached",
                    adapter_type=type(observer).__name__,
                )

    def attach_async_logger(self, async_logger: AsyncLoggerAdapter) -> None:
        """
        Attach an AsyncLoggerAdapter.

        Once attached, every audit event is also propagated to AsyncLogger.
        """
        self._async_logger = async_logger

        # Register AsyncLogger as an Observer
        observer = AsyncLoggerObserver(async_logger)
        self.attach_observer(observer)

        # Start AsyncLogger (if not started yet)
        async_logger.start()

        logger.info("integrated_recorder.asyncloggeradapter_attached")

    def _notify_observers(self, event: AuditEventData) -> None:
        """Propagate an event to every observer."""
        with self._observers_lock:
            for observer in self._observers:
                try:
                    observer.on_event(event)
                except Exception as e:
                    logger.exception(
                        "integrated_recorder.observer_error",
                        error=e,
                    )

    def _check_circuit_state_change(self) -> None:
        """Detect and propagate Circuit Breaker state changes."""
        current_state = self._recorder._circuit_breaker.state

        if self._last_circuit_state != current_state:
            if current_state.value == "open":
                self._notify_observers(
                    AuditEventData(
                        event_type=AuditObserverEventType.CIRCUIT_OPENED,
                        details={"service": "audit_primary"},
                    )
                )
            elif current_state.value == "closed" and self._last_circuit_state:
                if self._last_circuit_state.value == "open":
                    self._notify_observers(
                        AuditEventData(
                            event_type=AuditObserverEventType.PRIMARY_RECOVERED,
                            details={"service": "audit_primary"},
                        )
                    )
                self._notify_observers(
                    AuditEventData(
                        event_type=AuditObserverEventType.CIRCUIT_CLOSED,
                        details={"service": "audit_primary"},
                    )
                )
            elif current_state.value == "half_open":
                self._notify_observers(
                    AuditEventData(
                        event_type=AuditObserverEventType.CIRCUIT_HALF_OPEN,
                        details={"service": "audit_primary"},
                    )
                )

            self._last_circuit_state = current_state

    def record_with_events(self, entry: AuditEntry) -> str:
        """
        Record + propagate events.

        Wraps the original record method so events are propagated too.
        """
        try:
            # Call the existing record path
            audit_id = self._recorder._record_with_integrity(entry)

            # Propagate the success event
            if self._enable_auto_async_logging:
                self._notify_observers(
                    AuditEventData(
                        event_type=AuditObserverEventType.RECORD_SUCCESS,
                        details={
                            "action": entry.action,
                            "audit_id": audit_id,
                        },
                    )
                )

            # Check for a circuit state change
            self._check_circuit_state_change()

            return audit_id

        except Exception as e:
            # Propagate the failure event
            if self._enable_auto_async_logging:
                self._notify_observers(
                    AuditEventData(
                        event_type=AuditObserverEventType.RECORD_FAILED,
                        details={
                            "action": entry.action,
                            "error": str(e),
                        },
                    )
                )

            # Check for a circuit state change
            self._check_circuit_state_change()

            raise

    def get_health_status(self) -> dict[str, Any]:
        """Return the integrated health status."""
        health = self._recorder.get_health_status()

        # Add AsyncLogger status
        if self._async_logger:
            health["async_logger"] = self._async_logger.get_stats()

        # Add observer count
        with self._observers_lock:
            health["observers_count"] = len(self._observers)

        return health

    # Proxy methods
    def start(self) -> None:
        """Start."""
        self._recorder.start()
        if self._async_logger:
            self._async_logger.start()

    def stop(self, timeout: float | None = None) -> None:
        """Stop."""
        self._recorder.stop(timeout)
        if self._async_logger:
            self._async_logger.stop(timeout)


# =============================================================================
# Convenience Functions
# =============================================================================


def configure_integration(
    resilient_recorder: ResilientContinuousAuditRecorder,
    flush_callback: Callable[[list[dict[str, Any]]], None] | None = None,
    async_logger_config: AsyncLoggerConfig | None = None,
) -> IntegratedAuditRecorder:
    """
    Integration setup helper function.

    Args:
        resilient_recorder: existing ResilientContinuousAuditRecorder
        flush_callback: AsyncLogger batch delivery callback
        async_logger_config: AsyncLogger configuration

    Returns:
        The configured IntegratedAuditRecorder
    """
    integrated = IntegratedAuditRecorder(resilient_recorder)

    if flush_callback:
        async_logger = AsyncLoggerAdapter(
            flush_callback=flush_callback,
            config=async_logger_config,
        )
        integrated.attach_async_logger(async_logger)

    return integrated


def create_command_center_callback(
    endpoint: str,
    timeout_seconds: float = 5.0,
) -> Callable[[list[dict[str, Any]]], None]:
    """
    Create a Command Center delivery callback.

    Args:
        endpoint: Command Center API endpoint
        timeout_seconds: request timeout

    Returns:
        Batch delivery callback function
    """
    import urllib.error
    import urllib.request

    from baldur.utils.http import safe_urlopen
    from baldur.utils.serialization import fast_dumps

    def send_to_command_center(events: list[dict[str, Any]]) -> None:
        """Send events to the Command Center."""
        try:
            data = fast_dumps(events)
            request = urllib.request.Request(
                endpoint,
                data=data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with safe_urlopen(request, timeout=timeout_seconds) as response:
                if response.status != 200:
                    logger.warning(
                        "command.center_returned",
                        response_status=response.status,
                    )
        except urllib.error.URLError as e:
            logger.exception(
                "integrated_recorder.command_center_send_failed",
                error=e,
            )
        except Exception as e:
            logger.exception(
                "command.center_callback_error",
                error=e,
            )

    return send_to_command_center


# =============================================================================
# Export for __init__.py
# =============================================================================

__all__ = [
    "EventSeverity",
    "AsyncLoggerConfig",
    "AsyncLoggerAdapter",
    "AuditObserverEventType",
    "AuditEventData",
    "AuditEventObserver",
    "AsyncLoggerObserver",
    "IntegratedAuditRecorder",
    "configure_integration",
    "create_command_center_callback",
]
