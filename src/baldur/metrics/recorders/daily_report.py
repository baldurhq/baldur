"""
Daily Report metric recorder — report generation and delivery metrics.

Metrics (5):
- baldur_daily_report_generated_total: Report generation counter
- baldur_daily_report_delivery_total: Report delivery counter by channel/status
- baldur_daily_report_skipped_total: Report skip counter by reason
- baldur_daily_report_entries_dropped_total: Per-entry write-side drops by reason
- baldur_daily_report_last_generated_timestamp_seconds: Unix-timestamp gauge
  set on each successful generation. Use ``time() - <gauge>`` in PromQL to
  alert on stale reports (e.g., >1.5d means yesterday's run was missed).

Cache backend failures (read-side and write-side adapter errors) are tracked
by `cache_operation_errors_total{backend, operation}` in metrics/drift_metrics.py
— recorded inside the cache adapter swallow branches, not by domain code.
"""

from __future__ import annotations

import time
from typing import Literal

import structlog

from baldur.metrics.recorders.base import BaseMetricRecorder
from baldur.metrics.registry import get_or_create_counter, get_or_create_gauge

logger = structlog.get_logger()

DropReason = Literal["trimmed"]

__all__ = ["DailyReportMetricRecorder", "DropReason"]


class DailyReportMetricRecorder(BaseMetricRecorder):
    """Daily Report metric definitions and recording (5 metrics)."""

    def __init__(self) -> None:
        self._generated_total = get_or_create_counter(
            f"{self.PREFIX}_daily_report_generated_total",
            "Total daily reports generated",
            ["is_synthetic"],
        )
        self._delivery_total = get_or_create_counter(
            f"{self.PREFIX}_daily_report_delivery_total",
            "Daily report delivery attempts by channel and status",
            ["channel", "status", "is_synthetic"],
        )
        self._skipped_total = get_or_create_counter(
            f"{self.PREFIX}_daily_report_skipped_total",
            "Daily reports skipped by reason",
            ["reason"],
        )
        self._entries_dropped_total = get_or_create_counter(
            f"{self.PREFIX}_daily_report_entries_dropped_total",
            "Daily report entries dropped by reason (write-side losses)",
            ["reason"],
        )
        self._last_generated_gauge = get_or_create_gauge(
            f"{self.PREFIX}_daily_report_last_generated_timestamp_seconds",
            "Unix timestamp of the last successful daily-report generation",
            [],
        )

    def record_generated(self) -> None:
        """Record a successful report generation."""
        try:
            is_synthetic = self._get_synthetic_label()
            self._generated_total.labels(is_synthetic=is_synthetic).inc()
            self._last_generated_gauge.set(time.time())
        except Exception:
            logger.debug("daily_report.metric_record_failed", metric="generated")

    def record_delivery(self, channel: str, success: bool) -> None:
        """Record a report delivery attempt."""
        try:
            is_synthetic = self._get_synthetic_label()
            status = "success" if success else "failure"
            self._delivery_total.labels(
                channel=channel, status=status, is_synthetic=is_synthetic
            ).inc()
        except Exception:
            logger.debug("daily_report.metric_record_failed", metric="delivery")

    def record_skipped(self, reason: str) -> None:
        """Record a skipped report."""
        try:
            self._skipped_total.labels(reason=reason).inc()
        except Exception:
            logger.debug("daily_report.metric_record_failed", metric="skipped")

    def record_entry_dropped(self, reason: DropReason, count: int = 1) -> None:
        """Record dropped entries by reason (write-side losses)."""
        try:
            self._entries_dropped_total.labels(reason=reason).inc(count)
        except Exception:
            logger.debug("daily_report.metric_record_failed", metric="entries_dropped")
