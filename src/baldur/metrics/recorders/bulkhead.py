"""
Bulkhead metric recorder — metric definitions and recording.

Owns all bulkhead-related Prometheus metrics. The series are the single source
of truth (name + label parity matters): the PRO ``BulkheadMetricsUpdater``
daemon and every reject path route their writes through this recorder rather
than re-defining the series, removing the two-definition label-drift footgun.

On an OSS-only checkout the series register at zero (exactly like DLQ/Throttle),
so the drift guard (G43) sees them without ``baldur_pro``. On a PRO install the
updater daemon populates the gauges and the reject sites increment the counter.

Metrics (5):
- baldur_bulkhead_active_count{bulkhead_name, bulkhead_type}: active requests
- baldur_bulkhead_max_concurrent{bulkhead_name}: maximum concurrent capacity
- baldur_bulkhead_rejected_total{bulkhead_name}: total rejected requests
- baldur_bulkhead_utilization_percent{bulkhead_name}: utilization (%)
- baldur_bulkhead_waiting_count{bulkhead_name}: waiting requests
"""

from __future__ import annotations

import structlog

from baldur.metrics.recorders.base import BaseMetricRecorder
from baldur.metrics.registry import (
    get_or_create_counter,
    get_or_create_gauge,
)

logger = structlog.get_logger()

__all__ = ["BulkheadMetricRecorder"]


class BulkheadMetricRecorder(BaseMetricRecorder):
    """Bulkhead metric definitions and recording."""

    def __init__(self) -> None:
        self._active_count = get_or_create_gauge(
            f"{self.PREFIX}_bulkhead_active_count",
            "Current number of active requests",
            ["bulkhead_name", "bulkhead_type"],
        )
        self._max_concurrent = get_or_create_gauge(
            f"{self.PREFIX}_bulkhead_max_concurrent",
            "Maximum concurrent capacity",
            ["bulkhead_name"],
        )
        self._rejected_total = get_or_create_counter(
            f"{self.PREFIX}_bulkhead_rejected_total",
            "Total rejected requests",
            ["bulkhead_name"],
        )
        self._utilization_percent = get_or_create_gauge(
            f"{self.PREFIX}_bulkhead_utilization_percent",
            "Bulkhead utilization (%)",
            ["bulkhead_name"],
        )
        self._waiting_count = get_or_create_gauge(
            f"{self.PREFIX}_bulkhead_waiting_count",
            "Number of waiting requests",
            ["bulkhead_name"],
        )

    def update_metrics(
        self,
        bulkhead_name: str,
        bulkhead_type: str,
        active_count: int,
        max_concurrent: int,
        waiting_count: int,
    ) -> None:
        """Update the bulkhead state gauges (active / max / waiting / utilization).

        Utilization is computed here (active / max * 100), so callers pass raw
        counts only. The rejection counter is event-driven via
        ``increment_rejected`` — it is not part of the poll snapshot.
        """
        try:
            self._active_count.labels(
                bulkhead_name=bulkhead_name,
                bulkhead_type=bulkhead_type,
            ).set(active_count)
            self._max_concurrent.labels(bulkhead_name=bulkhead_name).set(max_concurrent)
            self._waiting_count.labels(bulkhead_name=bulkhead_name).set(waiting_count)
            utilization = (
                (active_count / max_concurrent * 100) if max_concurrent > 0 else 0
            )
            self._utilization_percent.labels(bulkhead_name=bulkhead_name).set(
                utilization
            )
        except Exception as e:
            logger.debug("metrics.update_bulkhead_metrics_failed", error=e)

    def increment_rejected(self, bulkhead_name: str) -> None:
        """Increment the per-bulkhead rejection counter."""
        try:
            self._rejected_total.labels(bulkhead_name=bulkhead_name).inc()
        except Exception as e:
            logger.debug("metrics.increment_bulkhead_rejected_failed", error=e)
