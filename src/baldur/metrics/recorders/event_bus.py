"""
EventBus metric recorder — metric definitions and recording.

Owns EventBus-related Prometheus metrics for emit operations.
Tracks emit skips (EventBus unavailable) and emit failures (exceptions).
"""

from __future__ import annotations

import structlog

from baldur.metrics.recorders.base import BaseMetricRecorder
from baldur.metrics.registry import get_or_create_counter

logger = structlog.get_logger()

__all__ = [
    "EventBusMetricRecorder",
    "record_emit_skipped",
    "record_emit_failed",
]


class EventBusMetricRecorder(BaseMetricRecorder):
    """EventBus metric definitions and recording.

    Tracks:
    - Emit skips: EventBus unavailable, event silently dropped
    - Emit failures: EventBus.emit() raised an exception
    """

    def __init__(self) -> None:
        self._emit_skipped_total = get_or_create_counter(
            f"{self.PREFIX}_eventbus_emit_skipped_total",
            "Event emissions skipped due to EventBus unavailability",
            ["source"],
        )
        self._emit_failed_total = get_or_create_counter(
            f"{self.PREFIX}_eventbus_emit_failed_total",
            "Event emissions failed with exception",
            ["source", "event_type"],
        )

    def record_emit_skipped(self, source: str) -> None:
        """Record an emit skip (EventBus unavailable)."""
        try:
            self._emit_skipped_total.labels(source=source).inc()
        except Exception as e:
            logger.warning("metrics.record_emit_skipped_failed", error=e)

    def record_emit_failed(self, source: str, event_type: str) -> None:
        """Record an emit failure (exception raised)."""
        try:
            self._emit_failed_total.labels(source=source, event_type=event_type).inc()
        except Exception as e:
            logger.warning("metrics.record_emit_failed_failed", error=e)


# --- Module-level convenience functions ---


def _lazy_recorder() -> EventBusMetricRecorder | None:
    try:
        from baldur.metrics.prometheus import get_metrics

        return getattr(get_metrics(), "event_bus", None)
    except Exception:
        return None


def record_emit_skipped(source: str) -> None:
    """Record an emit skip (EventBus unavailable)."""
    rec = _lazy_recorder()
    if rec:
        rec.record_emit_skipped(source)


def record_emit_failed(source: str, event_type: str) -> None:
    """Record an emit failure (exception raised)."""
    rec = _lazy_recorder()
    if rec:
        rec.record_emit_failed(source, event_type)
