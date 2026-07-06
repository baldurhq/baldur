"""
Learning metric recorder — domain-level quality metrics for the
Learning service.

Different from C3 event handlers which record infrastructure event
counts; this recorder tracks learning quality and operational state.

Metrics (7):
- baldur_learning_patterns_total: Pattern learning counter
- baldur_learning_pattern_confidence: Pattern confidence histogram
- baldur_learning_suggestions_generated_total: Suggestion generation counter
- baldur_learning_suggestions_applied_total: Suggestion application counter
- baldur_learning_blacklisted_total: Blacklist registration counter
- baldur_learning_manual_only_mode: Manual-only mode gauge
- baldur_learning_anomalies_detected_total: Anomaly detection counter
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

__all__ = ["LearningMetricRecorder"]


class LearningMetricRecorder(BaseMetricRecorder):
    """Learning metric definitions and recording (7 metrics)."""

    def __init__(self) -> None:
        self._patterns_total = get_or_create_counter(
            f"{self.PREFIX}_learning_patterns_total",
            "Total patterns learned by type",
            ["pattern_type", "is_synthetic"],
        )
        self._pattern_confidence = get_or_create_histogram(
            f"{self.PREFIX}_learning_pattern_confidence",
            "Pattern confidence distribution (learning quality indicator)",
            ["pattern_type"],
            buckets=(0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0),
        )
        self._suggestions_generated = get_or_create_counter(
            f"{self.PREFIX}_learning_suggestions_generated_total",
            "Total suggestions generated",
            ["pattern_type", "priority", "is_synthetic"],
        )
        self._suggestions_applied = get_or_create_counter(
            f"{self.PREFIX}_learning_suggestions_applied_total",
            "Total suggestions applied",
            ["pattern_type", "is_synthetic"],
        )
        self._blacklisted_total = get_or_create_counter(
            f"{self.PREFIX}_learning_blacklisted_total",
            "Total blacklist registrations",
            ["module", "reason", "is_synthetic"],
        )
        self._manual_only_mode = get_or_create_gauge(
            f"{self.PREFIX}_learning_manual_only_mode",
            "Manual-only mode state per module (1=on, 0=off)",
            ["module"],
        )
        self._anomalies_detected = get_or_create_counter(
            f"{self.PREFIX}_learning_anomalies_detected_total",
            "Total anomalies detected",
            ["metric_name", "is_synthetic"],
        )

    def record_pattern(self, pattern_type: str, confidence: float) -> None:
        """Record a learned pattern."""
        try:
            is_synthetic = self._get_synthetic_label()
            self._patterns_total.labels(
                pattern_type=pattern_type, is_synthetic=is_synthetic
            ).inc()
            self._pattern_confidence.labels(pattern_type=pattern_type).observe(
                confidence
            )
        except Exception:
            logger.debug("learning.metric_record_failed", metric="pattern")

    def record_suggestion_generated(self, pattern_type: str, priority: str) -> None:
        """Record a suggestion generation."""
        try:
            is_synthetic = self._get_synthetic_label()
            self._suggestions_generated.labels(
                pattern_type=pattern_type,
                priority=priority,
                is_synthetic=is_synthetic,
            ).inc()
        except Exception:
            logger.debug("learning.metric_record_failed", metric="suggestion_generated")

    def record_suggestion_applied(self, pattern_type: str) -> None:
        """Record a suggestion application."""
        try:
            is_synthetic = self._get_synthetic_label()
            self._suggestions_applied.labels(
                pattern_type=pattern_type, is_synthetic=is_synthetic
            ).inc()
        except Exception:
            logger.debug("learning.metric_record_failed", metric="suggestion_applied")

    def record_blacklisted(self, module: str, reason: str) -> None:
        """Record a blacklist registration."""
        try:
            is_synthetic = self._get_synthetic_label()
            self._blacklisted_total.labels(
                module=module, reason=reason, is_synthetic=is_synthetic
            ).inc()
        except Exception:
            logger.debug("learning.metric_record_failed", metric="blacklisted")

    def set_manual_only(self, module: str, enabled: bool) -> None:
        """Set manual-only mode state for a module."""
        try:
            self._manual_only_mode.labels(module=module).set(1 if enabled else 0)
        except Exception:
            logger.debug("learning.metric_record_failed", metric="manual_only_mode")

    def record_anomaly(self, metric_name: str) -> None:
        """Record an anomaly detection."""
        try:
            is_synthetic = self._get_synthetic_label()
            self._anomalies_detected.labels(
                metric_name=metric_name, is_synthetic=is_synthetic
            ).inc()
        except Exception:
            logger.debug("learning.metric_record_failed", metric="anomaly")
