"""
Health Check metric recorder — metric definitions and recording.

Owns all Health Check-related Prometheus metrics.
Alias label cardinality bounded by Django static database configurations
(settings.DATABASES keys). See DD-5 for SRE-Core scope rationale.
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
    "HealthCheckMetricRecorder",
    "record_health_check",
    "set_health_status",
    "set_database_connected",
    "set_pool_status",
]

_STATUS_MAP = {"healthy": 0, "degraded": 1, "unhealthy": 2, "error": 3}
_POOL_STATUS_MAP = {"healthy": 0, "degraded": 1, "error": 2}


class HealthCheckMetricRecorder(BaseMetricRecorder):
    """Health Check metric definitions and recording (4 methods).

    DD-6: String interface — status/result mapped to int internally.
    """

    def __init__(self) -> None:
        self._duration = get_or_create_histogram(
            f"{self.PREFIX}_health_check_duration_seconds",
            "Check execution time per type and alias",
            ["check_type", "alias"],
            buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1, 2, 5),
        )
        self._results_total = get_or_create_counter(
            f"{self.PREFIX}_health_check_results_total",
            "Check outcomes (healthy/degraded/unhealthy/error/timeout)",
            ["check_type", "result", "is_synthetic"],
        )
        self._status = get_or_create_gauge(
            f"{self.PREFIX}_health_check_status",
            "Current aggregate status (0=healthy, 1=degraded, 2=unhealthy)",
            ["check_type"],
        )
        self._database_connected = get_or_create_gauge(
            f"{self.PREFIX}_health_check_database_connected",
            "Per-database connection state (1=connected, 0=not)",
            ["alias"],
        )
        self._pool_status = get_or_create_gauge(
            f"{self.PREFIX}_health_check_pool_status",
            "Per-alias pool state (0=healthy, 1=degraded, 2=error)",
            ["alias"],
        )

    def record_check(
        self, check_type: str, result: str, duration: float, alias: str = ""
    ) -> None:
        """Record a health check execution.

        check_type: database|pool|overall|readiness
        result: healthy|degraded|unhealthy|error|timeout
        """
        try:
            is_synthetic = self._get_synthetic_label()
            self._duration.labels(check_type=check_type, alias=alias).observe(duration)
            self._results_total.labels(
                check_type=check_type, result=result, is_synthetic=is_synthetic
            ).inc()
        except Exception as e:
            logger.warning("metrics.record_health_check_failed", error=e)

    def set_status(self, check_type: str, status: str) -> None:
        """Set current health status gauge.

        Maps string to int: healthy=0, degraded=1, unhealthy=2
        """
        try:
            value = _STATUS_MAP.get(status, 0)
            self._status.labels(check_type=check_type).set(value)
        except Exception as e:
            logger.warning("metrics.set_health_status_failed", error=e)

    def set_database_connected(self, alias: str, connected: bool) -> None:
        """Set per-database connection state gauge."""
        try:
            self._database_connected.labels(alias=alias).set(1 if connected else 0)
        except Exception as e:
            logger.warning("metrics.set_database_connected_failed", error=e)

    def set_pool_status(self, alias: str, status: str) -> None:
        """Set per-alias pool state gauge.

        Maps string to int: healthy=0, degraded=1, error=2
        """
        try:
            value = _POOL_STATUS_MAP.get(status, 0)
            self._pool_status.labels(alias=alias).set(value)
        except Exception as e:
            logger.warning("metrics.set_pool_status_failed", error=e)


# --- Module-level convenience functions (DD-7) ---


def _lazy_recorder() -> HealthCheckMetricRecorder | None:
    try:
        from baldur.metrics.prometheus import get_metrics

        return getattr(get_metrics(), "health_check", None)
    except Exception:
        return None


def record_health_check(
    check_type: str, result: str, duration: float, alias: str = ""
) -> None:
    rec = _lazy_recorder()
    if rec:
        rec.record_check(check_type, result, duration, alias)


def set_health_status(check_type: str, status: str) -> None:
    rec = _lazy_recorder()
    if rec:
        rec.set_status(check_type, status)


def set_database_connected(alias: str, connected: bool) -> None:
    rec = _lazy_recorder()
    if rec:
        rec.set_database_connected(alias, connected)


def set_pool_status(alias: str, status: str) -> None:
    rec = _lazy_recorder()
    if rec:
        rec.set_pool_status(alias, status)
