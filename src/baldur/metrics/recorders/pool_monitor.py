"""
Pool Monitor metric recorder — covers both ConnectionPoolMonitor
and PoolWatchdog in a single recorder.

Metrics (6):
- baldur_pool_health_status: Pool health status gauge (0-4)
- baldur_pool_utilization_percent: Pool utilization percentage gauge
- baldur_pool_leak_detected_total: Leak detection counter
- baldur_pool_close_leaked_total: Leaked connections closed counter
- baldur_pool_expand_total: Pool expansion counter
- baldur_pool_circuit_break_total: Pool circuit break counter
"""

from __future__ import annotations

import structlog

from baldur.metrics.recorders.base import BaseMetricRecorder
from baldur.metrics.registry import (
    get_or_create_counter,
    get_or_create_gauge,
)

logger = structlog.get_logger()

__all__ = ["PoolMetricRecorder"]


class PoolMetricRecorder(BaseMetricRecorder):
    """Pool Monitor + Watchdog metric definitions and recording (6 metrics)."""

    def __init__(self) -> None:
        self._health_status = get_or_create_gauge(
            f"{self.PREFIX}_pool_health_status",
            "Pool health status (0=healthy, 1=warning, 2=critical, 3=exhausted, 4=leak_suspected)",
            ["pool_name"],
        )
        self._utilization = get_or_create_gauge(
            f"{self.PREFIX}_pool_utilization_percent",
            "Pool utilization percentage",
            ["pool_name"],
        )
        self._leak_detected_total = get_or_create_counter(
            f"{self.PREFIX}_pool_leak_detected_total",
            "Number of leak detections",
            ["pool_name"],
        )
        self._close_leaked_total = get_or_create_counter(
            f"{self.PREFIX}_pool_close_leaked_total",
            "Number of leaked connections closed by watchdog",
            ["pool_name"],
        )
        self._expand_total = get_or_create_counter(
            f"{self.PREFIX}_pool_expand_total",
            "Number of pool expansions by watchdog",
            ["pool_name"],
        )
        self._circuit_break_total = get_or_create_counter(
            f"{self.PREFIX}_pool_circuit_break_total",
            "Number of pool circuit breaks triggered",
            ["pool_name"],
        )

    def set_health_status(self, pool_name: str, status_value: int) -> None:
        """Set pool health status gauge."""
        try:
            self._health_status.labels(pool_name=pool_name).set(
                self._clamp_non_negative(status_value, "pool_health_status")
            )
        except Exception:
            logger.debug("pool_monitor.metric_record_failed", metric="health_status")

    def set_utilization(self, pool_name: str, percent: float) -> None:
        """Set pool utilization percentage gauge."""
        try:
            self._utilization.labels(pool_name=pool_name).set(
                self._clamp_percentage(percent, "pool_utilization")
            )
        except Exception:
            logger.debug("pool_monitor.metric_record_failed", metric="utilization")

    def record_leak_detected(self, pool_name: str) -> None:
        """Record a leak detection event."""
        try:
            self._leak_detected_total.labels(pool_name=pool_name).inc()
        except Exception:
            logger.debug("pool_monitor.metric_record_failed", metric="leak_detected")

    def record_close_leaked(self, pool_name: str) -> None:
        """Record leaked connections closed by watchdog."""
        try:
            self._close_leaked_total.labels(pool_name=pool_name).inc()
        except Exception:
            logger.debug("pool_monitor.metric_record_failed", metric="close_leaked")

    def record_expand(self, pool_name: str) -> None:
        """Record pool expansion by watchdog."""
        try:
            self._expand_total.labels(pool_name=pool_name).inc()
        except Exception:
            logger.debug("pool_monitor.metric_record_failed", metric="expand")

    def record_circuit_break(self, pool_name: str) -> None:
        """Record pool circuit break."""
        try:
            self._circuit_break_total.labels(pool_name=pool_name).inc()
        except Exception:
            logger.debug("pool_monitor.metric_record_failed", metric="circuit_break")
