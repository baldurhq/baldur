"""
Shutdown metric recorder — metric definitions and recording.

Owns all Graceful Shutdown-related Prometheus metrics.
See DD-5 for SRE-Core scope rationale.
"""

from __future__ import annotations

import structlog

from baldur.metrics.recorders.base import BaseMetricRecorder
from baldur.metrics.registry import (
    get_or_create_counter,
    get_or_create_gauge,
    get_or_create_histogram,
)

logger = structlog.get_logger()

__all__ = [
    "ShutdownMetricRecorder",
    "set_shutdown_phase",
    "record_drain_duration",
    "record_drained",
    "record_aborted",
    "record_shutdown_initiated",
]

_PHASE_MAP = {"running": 0, "draining": 1, "terminating": 2, "terminated": 3}


class ShutdownMetricRecorder(BaseMetricRecorder):
    """Shutdown metric definitions and recording (4 methods).

    DD-6: String interface — phase mapped to int internally.
    """

    def __init__(self) -> None:
        self._phase = get_or_create_gauge(
            f"{self.PREFIX}_shutdown_phase",
            "Current phase (0-3)",
            [],
        )
        self._drain_duration = get_or_create_histogram(
            f"{self.PREFIX}_shutdown_drain_duration_seconds",
            "Time from DRAINING to completion",
            [],
            buckets=(1, 5, 10, 15, 30, 60, 120, 300),
        )
        self._drained_total = get_or_create_counter(
            f"{self.PREFIX}_shutdown_drained_requests_total",
            "Requests successfully drained",
            [],
        )
        self._aborted_total = get_or_create_counter(
            f"{self.PREFIX}_shutdown_aborted_requests_total",
            "Requests force-killed during TERMINATING",
            [],
        )
        self._initiations_total = get_or_create_counter(
            f"{self.PREFIX}_shutdown_initiations_total",
            "Times shutdown initiation has fired (signal or programmatic)",
            [],
        )

    def set_phase(self, phase: str) -> None:
        """Set shutdown phase gauge.

        Maps phase string to int: running=0, draining=1, terminating=2, terminated=3
        """
        try:
            # No str() coercion: ShutdownPhase is a (str, Enum), so members hash
            # and compare by value and hit the map directly — while str(member)
            # returns the member path ('ShutdownPhase.DRAINING'), which always
            # misses. Unhashable input raises TypeError into the fail-open
            # except below.
            value = _PHASE_MAP.get(phase)
            if value is None:
                logger.warning(
                    "metrics.set_shutdown_phase_failed",
                    reason="unmapped_value",
                    phase=repr(phase),
                )
                value = 0
            self._phase.set(value)
        except Exception as e:
            logger.warning("metrics.set_shutdown_phase_failed", error=e)

    def record_drain_duration(self, duration: float) -> None:
        """Record total drain duration in seconds."""
        try:
            self._drain_duration.observe(duration)
        except Exception as e:
            logger.warning("metrics.record_drain_duration_failed", error=e)

    def record_drained(self, count: int = 1) -> None:
        """Increment drained requests counter."""
        try:
            self._drained_total.inc(count)
        except Exception as e:
            logger.warning("metrics.record_drained_failed", error=e)

    def record_aborted(self, count: int = 1) -> None:
        """Increment aborted requests counter."""
        try:
            self._aborted_total.inc(count)
        except Exception as e:
            logger.warning("metrics.record_aborted_failed", error=e)

    def record_initiated(self) -> None:
        """Increment shutdown-initiation counter.

        Called from ``GracefulShutdownCoordinator.initiate_shutdown`` —
        often inside an OS signal-handler context where structlog's first
        emit may be dropped (signal interrupting a logging-internal lock).
        prometheus_client's ``inc()`` has a much shorter critical section
        than logging's handler chain, so this counter is the more reliable
        marker for "shutdown initiation fired" across operator dashboards.
        """
        try:
            self._initiations_total.inc()
        except Exception as e:
            logger.warning("metrics.record_shutdown_initiated_failed", error=e)


# --- Module-level convenience functions (DD-7) ---


def _lazy_recorder() -> ShutdownMetricRecorder | None:
    try:
        from baldur.metrics.prometheus import get_metrics

        return getattr(get_metrics(), "shutdown", None)
    except Exception:
        return None


def set_shutdown_phase(phase: str) -> None:
    rec = _lazy_recorder()
    if rec:
        rec.set_phase(phase)


def record_drain_duration(duration: float) -> None:
    rec = _lazy_recorder()
    if rec:
        rec.record_drain_duration(duration)


def record_drained(count: int = 1) -> None:
    rec = _lazy_recorder()
    if rec:
        rec.record_drained(count)


def record_aborted(count: int = 1) -> None:
    rec = _lazy_recorder()
    if rec:
        rec.record_aborted(count)


def record_shutdown_initiated() -> None:
    rec = _lazy_recorder()
    if rec:
        rec.record_initiated()
