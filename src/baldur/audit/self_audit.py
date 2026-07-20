"""
Self-Audit Logger.

Records the state of the audit system itself.

Principles:
1. Audit system failures must be recorded too
2. Minimal dependencies: standard stderr/syslog only
3. No circular dependency: independent of other audit modules

Usage:
    from baldur.audit.self_audit import self_audit, SelfAuditEvent

    # System startup
    self_audit().log(SelfAuditEvent.STARTUP, "Audit system started")

    # WAL write failure
    self_audit().log(
        SelfAuditEvent.WAL_WRITE_FAILED,
        "WAL write failed",
        details={"error": str(e)},
    )
"""

from __future__ import annotations

import os
import sys
import threading
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

import structlog

from baldur.utils.time import utc_now

logger = structlog.get_logger()


class SelfAuditEvent(str, Enum):
    """Event types of the audit system itself."""

    # Lifecycle
    STARTUP = "startup"
    SHUTDOWN = "shutdown"
    INITIALIZED = "initialized"

    # Write Path Failures
    WAL_WRITE_FAILED = "wal_write_failed"
    PRIMARY_STORE_FAILED = "primary_store_failed"
    BATCH_FLUSH_FAILED = "batch_flush_failed"

    # Fallback Events
    FALLBACK_ACTIVATED = "fallback_activated"
    FALLBACK_FAILED = "fallback_failed"
    SYSLOG_ACTIVATED = "syslog_activated"
    SYSLOG_FAILED = "syslog_failed"

    # Circuit Breaker Events
    CIRCUIT_OPENED = "circuit_opened"
    CIRCUIT_CLOSED = "circuit_closed"
    CIRCUIT_HALF_OPEN = "circuit_half_open"

    # Buffer Events
    BUFFER_OVERFLOW = "buffer_overflow"
    BUFFER_HIGH_WATERMARK = "buffer_high_watermark"

    # Integrity Events
    CHECKSUM_MISMATCH = "checksum_mismatch"
    HASH_CHAIN_BROKEN = "hash_chain_broken"
    WAL_CORRUPTED = "wal_corrupted"

    # Monitoring Events
    HEARTBEAT_MISSED = "heartbeat_missed"
    WATCHDOG_TIMEOUT = "watchdog_timeout"

    # Recovery Events
    RECOVERY_STARTED = "recovery_started"
    RECOVERY_COMPLETED = "recovery_completed"
    RECOVERY_FAILED = "recovery_failed"


@dataclass
class SelfAuditStats:
    """Self-Audit statistics."""

    total_events: int = 0
    failure_events: int = 0
    events_by_type: dict[str, int] = field(default_factory=dict)
    last_event_time: datetime | None = None
    uptime_seconds: float = 0.0


class SelfAuditLogger:
    """
    Records the state of the audit system itself.

    Characteristics:
    - No circular dependency (self-contained)
    - Minimal dependencies (standard library only)
    - Designed to always succeed (failures ignored)
    - Singleton pattern
    """

    _instance: SelfAuditLogger | None = None
    _lock = threading.Lock()

    # Events considered failures
    FAILURE_EVENTS = frozenset(
        [
            SelfAuditEvent.WAL_WRITE_FAILED,
            SelfAuditEvent.PRIMARY_STORE_FAILED,
            SelfAuditEvent.BATCH_FLUSH_FAILED,
            SelfAuditEvent.FALLBACK_FAILED,
            SelfAuditEvent.SYSLOG_FAILED,
            SelfAuditEvent.CHECKSUM_MISMATCH,
            SelfAuditEvent.HASH_CHAIN_BROKEN,
            SelfAuditEvent.WAL_CORRUPTED,
            SelfAuditEvent.HEARTBEAT_MISSED,
            SelfAuditEvent.WATCHDOG_TIMEOUT,
            SelfAuditEvent.RECOVERY_FAILED,
        ]
    )

    def __init__(self):
        """Initialize SelfAuditLogger."""
        self._logger = structlog.get_logger().bind(component="self_audit")
        self._start_time = utc_now()
        self._stats = SelfAuditStats()
        self._recent_events: list[dict[str, Any]] = []
        self._max_recent_events = self._get_max_recent_events()
        self._stats_lock = threading.Lock()

    @staticmethod
    def _get_max_recent_events() -> int:
        """Read max_recent_events from settings."""
        try:
            from baldur.settings.audit import get_audit_settings

            return get_audit_settings().self_audit_max_recent_events
        except Exception:
            return 100  # default

    @staticmethod
    def _get_default_limit() -> int:
        """Read default_limit from settings."""
        try:
            from baldur.settings.audit import get_audit_settings

            return get_audit_settings().self_audit_default_limit
        except Exception:
            return 20  # default

    @staticmethod
    def _get_max_failure_rate() -> float:
        """Read max_failure_rate from settings."""
        try:
            from baldur.settings.audit import get_audit_settings

            return get_audit_settings().self_audit_max_failure_rate
        except Exception:
            return 0.1  # default

    @classmethod
    def get_instance(cls) -> SelfAuditLogger:
        """Get singleton instance."""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        """Reset singleton (for testing)."""
        with cls._lock:
            cls._instance = None

    def log(
        self,
        event_type: SelfAuditEvent,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        """
        Record a Self-Audit event. Must always succeed.

        Args:
            event_type: Event type
            message: Message
            details: Additional details
        """
        try:
            now = utc_now()

            # Update statistics
            with self._stats_lock:
                self._stats.total_events += 1
                self._stats.last_event_time = now

                event_name = event_type.value
                if event_name not in self._stats.events_by_type:
                    self._stats.events_by_type[event_name] = 0
                self._stats.events_by_type[event_name] += 1

                if event_type in self.FAILURE_EVENTS:
                    self._stats.failure_events += 1

                # Store recent event
                event_record = {
                    "timestamp": now.isoformat(),
                    "event": event_name,
                    "message": message,
                    "details": details or {},
                }
                self._recent_events.append(event_record)
                if len(self._recent_events) > self._max_recent_events:
                    self._recent_events = self._recent_events[
                        -self._max_recent_events :
                    ]

            # Decide log level
            if event_type in self.FAILURE_EVENTS:
                log_method = self._logger.error
            elif event_type in (
                SelfAuditEvent.FALLBACK_ACTIVATED,
                SelfAuditEvent.SYSLOG_ACTIVATED,
                SelfAuditEvent.CIRCUIT_OPENED,
            ):
                log_method = self._logger.warning
            else:
                log_method = self._logger.info

            # Logging
            log_kwargs: dict[str, Any] = {
                "event_type": event_type.value,
                "message": message,
            }
            if details:
                log_kwargs["details"] = details
            log_method("self_audit.event", **log_kwargs)

            # stderr output (failure events only, skipped in test environments)
            if event_type in self.FAILURE_EVENTS and not os.environ.get(
                "BALDUR_TEST_MODE"
            ):
                print(
                    f"[SELF-AUDIT] {now.isoformat()} {event_type.value}: {message}",
                    file=sys.stderr,
                    flush=True,
                )

        except Exception:
            # Self-audit failures are swallowed silently (prevents infinite loops)
            pass

    def get_stats(self) -> SelfAuditStats:
        """
        Read statistics.

        Returns:
            SelfAuditStats object
        """
        with self._stats_lock:
            uptime = (utc_now() - self._start_time).total_seconds()
            return SelfAuditStats(
                total_events=self._stats.total_events,
                failure_events=self._stats.failure_events,
                events_by_type=dict(self._stats.events_by_type),
                last_event_time=self._stats.last_event_time,
                uptime_seconds=uptime,
            )

    def get_recent_events(self, limit: int | None = None) -> list[dict[str, Any]]:
        """
        Read recent events.

        Args:
            limit: Maximum count (None uses the default from settings)

        Returns:
            List of recent events
        """
        if limit is None:
            limit = self._get_default_limit()
        with self._stats_lock:
            return list(self._recent_events[-limit:])

    def get_failure_rate(self) -> float:
        """
        Compute the failure rate.

        Returns:
            Ratio of failure events (0.0 ~ 1.0)
        """
        with self._stats_lock:
            if self._stats.total_events == 0:
                return 0.0
            return self._stats.failure_events / self._stats.total_events

    def is_healthy(self, max_failure_rate: float | None = None) -> bool:
        """
        Health check.

        Args:
            max_failure_rate: Maximum allowed failure rate (None uses the
                default from settings)

        Returns:
            True if healthy
        """
        if max_failure_rate is None:
            max_failure_rate = self._get_max_failure_rate()
        return self.get_failure_rate() <= max_failure_rate


def self_audit() -> SelfAuditLogger:
    """
    Return the Self-Audit Logger instance.

    Usage:
        self_audit().log(SelfAuditEvent.STARTUP, "Started")
    """
    return SelfAuditLogger.get_instance()
