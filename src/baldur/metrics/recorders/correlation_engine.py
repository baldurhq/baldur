"""
Correlation Engine metric recorder — operational visibility for the
correlation analysis engine.

Provides quantitative data for monitoring engine health, ML bulkhead
utilization, and strategy fallback frequency. This data drives
evidence-based refactoring decisions (see 358 trigger conditions).

Metrics (8):
- baldur_correlation_engine_state: Engine state gauge
- baldur_correlation_engine_periodic_analysis_total: Periodic analysis count
- baldur_correlation_engine_periodic_analysis_duration_seconds: Analysis duration
- baldur_correlation_engine_incident_analysis_total: On-demand analysis count
- baldur_correlation_engine_ml_bulkhead_active: Active ML tasks gauge
- baldur_correlation_engine_ml_bulkhead_rejected_total: ML task rejections
- baldur_correlation_engine_correlations_tracked: Tracked correlation pairs
- baldur_correlation_engine_strategy_fallback_total: Strategy fallback count
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

__all__ = ["CorrelationEngineMetricRecorder"]


class CorrelationEngineMetricRecorder(BaseMetricRecorder):
    """Correlation Engine metric definitions and recording (8 metrics)."""

    def __init__(self) -> None:
        self._state = get_or_create_gauge(
            f"{self.PREFIX}_correlation_engine_state",
            "Engine state (0=disabled, 1=initialized, 2=running)",
            [],
        )
        self._periodic_analysis_total = get_or_create_counter(
            f"{self.PREFIX}_correlation_engine_periodic_analysis_total",
            "Periodic analysis execution count",
            [],
        )
        self._periodic_analysis_duration = get_or_create_histogram(
            f"{self.PREFIX}_correlation_engine_periodic_analysis_duration_seconds",
            "Periodic analysis duration in seconds",
            [],
            buckets=(0.1, 0.5, 1, 2, 5, 10, 30, 60, 120),
        )
        self._incident_analysis_total = get_or_create_counter(
            f"{self.PREFIX}_correlation_engine_incident_analysis_total",
            "On-demand incident analysis total count",
            [],
        )
        self._ml_bulkhead_active = get_or_create_gauge(
            f"{self.PREFIX}_correlation_engine_ml_bulkhead_active",
            "Active ML inference tasks in bulkhead",
            [],
        )
        self._ml_bulkhead_rejected_total = get_or_create_counter(
            f"{self.PREFIX}_correlation_engine_ml_bulkhead_rejected_total",
            "ML task rejections by priority",
            ["priority"],
        )
        self._correlations_tracked = get_or_create_gauge(
            f"{self.PREFIX}_correlation_engine_correlations_tracked",
            "Number of correlation pairs currently tracked",
            [],
        )
        self._strategy_fallback_total = get_or_create_counter(
            f"{self.PREFIX}_correlation_engine_strategy_fallback_total",
            "Strategy fallback occurrences",
            [],
        )

    def set_state(self, state: int) -> None:
        """Set engine state (0=disabled, 1=initialized, 2=running)."""
        try:
            self._state.labels().set(state)
        except Exception:
            logger.debug("correlation_engine.metric_record_failed", metric="state")

    def record_periodic_analysis(self, duration_seconds: float) -> None:
        """Record a periodic analysis execution and its duration."""
        try:
            self._periodic_analysis_total.labels().inc()
            self._periodic_analysis_duration.labels().observe(
                self._clamp_non_negative(duration_seconds, "periodic_analysis_duration")
            )
        except Exception:
            logger.debug(
                "correlation_engine.metric_record_failed",
                metric="periodic_analysis",
            )

    def record_incident_analysis(self) -> None:
        """Record an on-demand incident analysis."""
        try:
            self._incident_analysis_total.labels().inc()
        except Exception:
            logger.debug(
                "correlation_engine.metric_record_failed",
                metric="incident_analysis",
            )

    def set_ml_bulkhead_active(self, count: int) -> None:
        """Set the number of active ML inference tasks."""
        try:
            self._ml_bulkhead_active.labels().set(
                self._clamp_non_negative(count, "ml_bulkhead_active")
            )
        except Exception:
            logger.debug(
                "correlation_engine.metric_record_failed",
                metric="ml_bulkhead_active",
            )

    def record_ml_bulkhead_rejected(self, priority: str) -> None:
        """Record an ML task rejection by priority."""
        try:
            self._ml_bulkhead_rejected_total.labels(priority=priority).inc()
        except Exception:
            logger.debug(
                "correlation_engine.metric_record_failed",
                metric="ml_bulkhead_rejected",
            )

    def set_correlations_tracked(self, count: int) -> None:
        """Set the number of tracked correlation pairs."""
        try:
            self._correlations_tracked.labels().set(
                self._clamp_non_negative(count, "correlations_tracked")
            )
        except Exception:
            logger.debug(
                "correlation_engine.metric_record_failed",
                metric="correlations_tracked",
            )

    def record_strategy_fallback(self) -> None:
        """Record a strategy fallback occurrence."""
        try:
            self._strategy_fallback_total.labels().inc()
        except Exception:
            logger.debug(
                "correlation_engine.metric_record_failed",
                metric="strategy_fallback",
            )
