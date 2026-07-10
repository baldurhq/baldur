"""
Cascade Load Shedding - drop events under high load.

Applies priority-based load shedding so a high-load situation in the audit
system does not cascade into a full system outage.

Features:
- Priority-based event dropping (LOW -> MEDIUM order)
- Automatic adjustment based on buffer utilization
- CRITICAL events are never dropped
- Metrics recording

Usage:
    from baldur.audit.cascade_load_shedding import CascadeLoadShedding

    shedding = CascadeLoadShedding()

    # Check before recording an event
    decision = shedding.should_accept(
        trigger_type="METRICS_UPDATED",
        buffer_size=8000,
        buffer_capacity=10000,
    )

    if decision["accepted"]:
        # record the event
        auditor.record(...)
    else:
        # drop or fall back locally
        shedding.record_dropped(trigger_type)
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any

import structlog

from baldur.audit.cascade_config import (
    AuditBackpressureConfig,
    get_audit_backpressure_config,
)
from baldur.audit.cascade_event import (
    CascadeEventPriority,
    get_priority_for_trigger,
)
from baldur.core.rate_limiting import SlidingWindowCounter
from baldur.utils.time import utc_now

logger = structlog.get_logger()

# Single-key window: this manager tracks one global event rate, not per-key.
_RATE_WINDOW_KEY = "cascade"

# Fallback rate window (seconds) when audit settings cannot be read.
_DEFAULT_RATE_WINDOW_SECONDS = 1.0


# =============================================================================
# Metrics (Prometheus-compatible)
# =============================================================================


@dataclass
class LoadSheddingMetrics:
    """Load shedding metrics."""

    accepted_count: int = 0
    """Number of accepted events."""

    dropped_count: int = 0
    """Number of dropped events."""

    fallback_count: int = 0
    """Number of events handled via fallback."""

    dropped_by_priority: dict[str, int] = field(
        default_factory=lambda: {
            "LOW": 0,
            "MEDIUM": 0,
            "HIGH": 0,
            "CRITICAL": 0,
        }
    )
    """Drop count per priority."""

    last_shedding_time: str | None = None
    """Timestamp of the last load-shedding event."""

    def to_dict(self) -> dict[str, Any]:
        """Convert to a dictionary."""
        return {
            "accepted_count": self.accepted_count,
            "dropped_count": self.dropped_count,
            "fallback_count": self.fallback_count,
            "dropped_by_priority": dict(self.dropped_by_priority),
            "last_shedding_time": self.last_shedding_time,
            "drop_rate": (
                self.dropped_count / max(1, self.accepted_count + self.dropped_count)
            ),
        }


# =============================================================================
# CascadeLoadShedding
# =============================================================================


class CascadeLoadShedding:
    """
    Cascade event load-shedding manager.

    Drops lower-priority events based on buffer utilization. CRITICAL events
    are never dropped.

    Shedding Policy:
        - Normal (< warning_threshold): accept all events
        - Warning (warning ~ critical): drop LOW priority
        - Critical (>= critical_threshold): drop LOW + MEDIUM
        - CRITICAL events: always accepted (via local fallback)
    """

    def __init__(self, config: AuditBackpressureConfig | None = None):
        """
        Args:
            config: Backpressure config (defaults are used when None).
        """
        self.config = config or get_audit_backpressure_config()
        self._metrics = LoadSheddingMetrics()
        self._lock = threading.RLock()

        # Rate limiting — a single-key sliding window with write-side retention
        # so memory stays bounded by the rate window.
        self._rate_window_seconds = self._get_rate_window_seconds()
        self._rate_window = SlidingWindowCounter(
            retention_seconds=self._rate_window_seconds
        )

    @staticmethod
    def _get_rate_window_seconds() -> float:
        """Read rate_window_seconds from settings."""
        try:
            from baldur.settings.audit import get_audit_settings

            return get_audit_settings().cascade_rate_window_seconds
        except Exception:
            return _DEFAULT_RATE_WINDOW_SECONDS

    def should_accept(
        self,
        trigger_type: str,
        buffer_size: int,
        buffer_capacity: int,
        priority: CascadeEventPriority | None = None,
    ) -> dict[str, Any]:
        """
        Decide whether to accept an event.

        Args:
            trigger_type: Trigger type
            buffer_size: Current buffer size
            buffer_capacity: Maximum buffer capacity
            priority: Priority (inferred from trigger_type when None)

        Returns:
            Decision result:
            - accepted: whether the event is accepted
            - priority: event priority
            - buffer_ratio: buffer utilization
            - reason: decision reason
            - use_fallback: whether local fallback is recommended
        """
        if not self.config.load_shedding_enabled:
            return {
                "accepted": True,
                "priority": CascadeEventPriority.MEDIUM.name,
                "buffer_ratio": 0.0,
                "reason": "load_shedding_disabled",
                "use_fallback": False,
            }

        # Determine priority
        event_priority = priority or get_priority_for_trigger(trigger_type)

        # Buffer utilization
        buffer_ratio = buffer_size / max(1, buffer_capacity)

        # Rate-limit check
        rate_exceeded = self._check_rate_limit()

        with self._lock:
            # CRITICAL is always accepted (fallback recommended)
            if event_priority == CascadeEventPriority.CRITICAL:
                self._metrics.accepted_count += 1
                return {
                    "accepted": True,
                    "priority": event_priority.name,
                    "buffer_ratio": buffer_ratio,
                    "reason": "critical_always_accepted",
                    "use_fallback": buffer_ratio
                    >= self.config.buffer_critical_threshold,
                }

            # Decision based on buffer state
            if buffer_ratio >= self.config.buffer_critical_threshold:
                # Critical state: drop MEDIUM and below
                if event_priority <= CascadeEventPriority.MEDIUM:
                    return self._drop_event(
                        event_priority, buffer_ratio, "buffer_critical"
                    )

            # Warning state: drop LOW-priority events
            elif (
                buffer_ratio >= self.config.buffer_warning_threshold
                and event_priority <= CascadeEventPriority.LOW
            ):
                return self._drop_event(event_priority, buffer_ratio, "buffer_warning")

            # Rate limit exceeded
            if rate_exceeded and event_priority <= CascadeEventPriority.LOW:
                return self._drop_event(event_priority, buffer_ratio, "rate_exceeded")

            # Accept
            self._metrics.accepted_count += 1
            self._record_event_time()

            return {
                "accepted": True,
                "priority": event_priority.name,
                "buffer_ratio": buffer_ratio,
                "reason": "accepted",
                "use_fallback": False,
                "load_shedding_triggered": buffer_ratio
                >= self.config.buffer_critical_threshold,
            }

    def _drop_event(
        self,
        priority: CascadeEventPriority,
        buffer_ratio: float,
        reason: str,
    ) -> dict[str, Any]:
        """Handle an event drop."""
        self._metrics.dropped_count += 1
        self._metrics.dropped_by_priority[priority.name] += 1
        self._metrics.last_shedding_time = utc_now().isoformat()

        logger.warning(
            "cascade_load_shedding.event_dropped",
            priority=priority.name,
            reason=reason,
            buffer_ratio=buffer_ratio,
        )

        return {
            "accepted": False,
            "priority": priority.name,
            "buffer_ratio": buffer_ratio,
            "reason": reason,
            "use_fallback": self.config.fallback_enabled,
        }

    def _check_rate_limit(self) -> bool:
        """
        Check whether the rate limit is exceeded.

        Returns:
            True if the per-second maximum event count is exceeded.
        """
        count = self._rate_window.count(_RATE_WINDOW_KEY, self._rate_window_seconds)
        return count >= self.config.max_events_per_second

    def _record_event_time(self) -> None:
        """Record an event timestamp (for rate limiting)."""
        self._rate_window.record(_RATE_WINDOW_KEY)

    def record_dropped(
        self,
        trigger_type: str,
        priority: CascadeEventPriority | None = None,
    ) -> None:
        """
        Record a dropped event (for metrics).

        Args:
            trigger_type: Trigger type
            priority: Priority
        """
        event_priority = priority or get_priority_for_trigger(trigger_type)

        with self._lock:
            self._metrics.dropped_count += 1
            self._metrics.dropped_by_priority[event_priority.name] += 1

    def record_fallback(self) -> None:
        """Record a fallback handling."""
        with self._lock:
            self._metrics.fallback_count += 1

    def get_metrics(self) -> dict[str, Any]:
        """Return the current metrics."""
        with self._lock:
            return self._metrics.to_dict()

    def get_status(
        self,
        buffer_size: int,
        buffer_capacity: int,
    ) -> dict[str, Any]:
        """
        Return the current load-shedding status.

        Args:
            buffer_size: Current buffer size
            buffer_capacity: Maximum buffer capacity

        Returns:
            Status dictionary
        """
        buffer_ratio = buffer_size / max(1, buffer_capacity)

        if buffer_ratio >= self.config.buffer_critical_threshold:
            status = "CRITICAL"
            shedding_level = "MEDIUM_AND_BELOW"
        elif buffer_ratio >= self.config.buffer_warning_threshold:
            status = "WARNING"
            shedding_level = "LOW_ONLY"
        else:
            status = "NORMAL"
            shedding_level = "NONE"

        return {
            "status": status,
            "buffer_ratio": buffer_ratio,
            "buffer_size": buffer_size,
            "buffer_capacity": buffer_capacity,
            "shedding_level": shedding_level,
            "config": {
                "warning_threshold": self.config.buffer_warning_threshold,
                "critical_threshold": self.config.buffer_critical_threshold,
                "max_events_per_second": self.config.max_events_per_second,
            },
            "metrics": self.get_metrics(),
        }

    def reset_metrics(self) -> None:
        """Reset metrics."""
        with self._lock:
            self._metrics = LoadSheddingMetrics()
            self._rate_window.reset_all()


# =============================================================================
# Singleton
# =============================================================================


_load_shedding: CascadeLoadShedding | None = None
_shedding_lock = threading.Lock()


def get_cascade_load_shedding() -> CascadeLoadShedding:
    """Return the CascadeLoadShedding singleton."""
    global _load_shedding

    if _load_shedding is not None:
        return _load_shedding

    with _shedding_lock:
        if _load_shedding is None:
            _load_shedding = CascadeLoadShedding()
        return _load_shedding


def reset_cascade_load_shedding() -> None:
    """Reset the CascadeLoadShedding singleton (for testing)."""
    global _load_shedding
    with _shedding_lock:
        _load_shedding = None
