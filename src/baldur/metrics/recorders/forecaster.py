"""
Forecaster metric recorder — predictive forecaster service metrics.

Metrics (7):
- baldur_forecaster_forecasts_total: Forecast execution counter
- baldur_forecaster_anomalies_total: Anomaly detection counter
- baldur_forecaster_spikes_total: Spike classification counter
- baldur_forecaster_actions_total: Proactive action counter
- baldur_forecaster_action_rejections_total: Action rejection counter
- baldur_forecaster_prediction_accuracy: Prediction accuracy histogram
- baldur_forecaster_mispredictions_consecutive: Consecutive misprediction gauge
"""

from __future__ import annotations

import structlog

from baldur.metrics.recorders.base import BaseMetricRecorder
from baldur.metrics.registry import (
    get_or_create_counter,
    get_or_create_gauge,
    get_or_create_histogram,
    sanitize_label_value,
)

logger = structlog.get_logger()

__all__ = ["ForecasterMetricRecorder"]


class ForecasterMetricRecorder(BaseMetricRecorder):
    """Forecaster metric definitions and recording (7 metrics)."""

    def __init__(self) -> None:
        self._forecasts_total = get_or_create_counter(
            f"{self.PREFIX}_forecaster_forecasts_total",
            "Total forecasts executed",
            ["metric_name", "is_synthetic"],
        )
        self._anomalies_total = get_or_create_counter(
            f"{self.PREFIX}_forecaster_anomalies_total",
            "Total anomalies detected",
            ["metric_name", "detector_type", "is_synthetic"],
        )
        self._spikes_total = get_or_create_counter(
            f"{self.PREFIX}_forecaster_spikes_total",
            "Total spikes classified",
            ["metric_name", "spike_type", "is_synthetic"],
        )
        self._actions_total = get_or_create_counter(
            f"{self.PREFIX}_forecaster_actions_total",
            "Total proactive actions generated",
            ["spike_type", "dry_run", "is_synthetic"],
        )
        self._action_rejections_total = get_or_create_counter(
            f"{self.PREFIX}_forecaster_action_rejections_total",
            "Total proactive action rejections",
            ["reason", "is_synthetic"],
        )
        self._prediction_accuracy = get_or_create_histogram(
            f"{self.PREFIX}_forecaster_prediction_accuracy",
            "Prediction accuracy distribution (0-1)",
            ["metric_name"],
            buckets=(0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0),
        )
        self._mispredictions_consecutive = get_or_create_gauge(
            f"{self.PREFIX}_forecaster_mispredictions_consecutive",
            "Consecutive misprediction count per metric (alerts before auto-blacklist at threshold 3)",
            ["metric_name"],
        )

    def _safe_metric_name(self, metric_name: str) -> str:
        """Apply sanitize_label_value() as safety net for metric_name."""
        return sanitize_label_value(metric_name)

    def record_forecast(self, metric_name: str) -> None:
        """Record a forecast execution."""
        try:
            is_synthetic = self._get_synthetic_label()
            self._forecasts_total.labels(
                metric_name=self._safe_metric_name(metric_name),
                is_synthetic=is_synthetic,
            ).inc()
        except Exception:
            logger.debug("forecaster.metric_record_failed", metric="forecast")

    def record_anomaly(self, metric_name: str, detector_type: str) -> None:
        """Record an anomaly detection (detector_type=zscore|iqr)."""
        try:
            is_synthetic = self._get_synthetic_label()
            self._anomalies_total.labels(
                metric_name=self._safe_metric_name(metric_name),
                detector_type=detector_type,
                is_synthetic=is_synthetic,
            ).inc()
        except Exception:
            logger.debug("forecaster.metric_record_failed", metric="anomaly")

    def record_spike(self, metric_name: str, spike_type: str) -> None:
        """Record a spike classification."""
        try:
            is_synthetic = self._get_synthetic_label()
            self._spikes_total.labels(
                metric_name=self._safe_metric_name(metric_name),
                spike_type=spike_type,
                is_synthetic=is_synthetic,
            ).inc()
        except Exception:
            logger.debug("forecaster.metric_record_failed", metric="spike")

    def record_action(self, spike_type: str, dry_run: bool) -> None:
        """Record a proactive action generation."""
        try:
            is_synthetic = self._get_synthetic_label()
            self._actions_total.labels(
                spike_type=spike_type,
                dry_run=str(dry_run).lower(),
                is_synthetic=is_synthetic,
            ).inc()
        except Exception:
            logger.debug("forecaster.metric_record_failed", metric="action")

    def record_action_rejection(self, reason: str) -> None:
        """Record a proactive action rejection."""
        try:
            is_synthetic = self._get_synthetic_label()
            self._action_rejections_total.labels(
                reason=reason, is_synthetic=is_synthetic
            ).inc()
        except Exception:
            logger.debug("forecaster.metric_record_failed", metric="action_rejection")

    def observe_accuracy(self, metric_name: str, accuracy: float) -> None:
        """Record prediction accuracy observation."""
        try:
            self._prediction_accuracy.labels(
                metric_name=self._safe_metric_name(metric_name)
            ).observe(accuracy)
        except Exception:
            logger.debug("forecaster.metric_record_failed", metric="accuracy")

    def set_misprediction_count(self, metric_name: str, count: int) -> None:
        """Set consecutive misprediction count for a metric."""
        try:
            self._mispredictions_consecutive.labels(
                metric_name=self._safe_metric_name(metric_name)
            ).set(self._clamp_non_negative(count, "mispredictions"))
        except Exception:
            logger.debug(
                "forecaster.metric_record_failed", metric="misprediction_count"
            )
