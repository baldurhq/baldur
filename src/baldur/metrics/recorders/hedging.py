"""
Hedging metric recorder — replaces module-level functions in
core/hedging/metrics.py with BaseMetricRecorder subclass.

Gains: D14 synthetic label support, built-in try-except error handling,
and facade registration via BaldurMetrics.

Metrics (11):
- baldur_hedging_total: Total hedging executions by mode
- baldur_hedging_success_total: Successful executions by source
- baldur_hedging_failed_total: All-candidates-failed count
- baldur_hedging_timeout_total: Timeout count
- baldur_hedging_non_retryable_total: Non-retryable abort count
- baldur_hedging_hedged_total: Secondary-selected count
- baldur_hedging_disabled_due_to_load_total: Load-disabled count
- baldur_hedging_latency_seconds: Response latency histogram
- baldur_hedging_benefit_ms: Latency improvement histogram
- baldur_hedging_candidate_tried_total: Per-candidate execution count
- baldur_hedging_mismatch_total: Result mismatch count
"""

from __future__ import annotations

import structlog

from baldur.metrics.recorders.base import BaseMetricRecorder
from baldur.metrics.registry import (
    get_or_create_counter,
    get_or_create_histogram,
)

logger = structlog.get_logger()

__all__ = ["HedgingMetricRecorder"]


class HedgingMetricRecorder(BaseMetricRecorder):
    """Hedging metric definitions and recording (11 metrics)."""

    def __init__(self) -> None:
        self._total = get_or_create_counter(
            f"{self.PREFIX}_hedging_total",
            "Total number of hedging executions",
            ["mode", "is_synthetic"],
        )
        self._success_total = get_or_create_counter(
            f"{self.PREFIX}_hedging_success_total",
            "Number of successful hedging executions by source",
            ["source", "is_synthetic"],
        )
        self._failed_total = get_or_create_counter(
            f"{self.PREFIX}_hedging_failed_total",
            "Number of hedging executions where all candidates failed",
            ["is_synthetic"],
        )
        self._timeout_total = get_or_create_counter(
            f"{self.PREFIX}_hedging_timeout_total",
            "Number of hedging executions that timed out",
            ["is_synthetic"],
        )
        self._non_retryable_total = get_or_create_counter(
            f"{self.PREFIX}_hedging_non_retryable_total",
            "Number of hedging executions aborted due to non-retryable errors",
            ["is_synthetic"],
        )
        self._hedged_total = get_or_create_counter(
            f"{self.PREFIX}_hedging_hedged_total",
            "Number of times hedging selected a non-primary candidate",
            ["is_synthetic"],
        )
        self._disabled_due_to_load = get_or_create_counter(
            f"{self.PREFIX}_hedging_disabled_due_to_load_total",
            "Number of times hedging was disabled due to high load",
            ["load_level", "is_synthetic"],
        )
        self._latency_seconds = get_or_create_histogram(
            f"{self.PREFIX}_hedging_latency_seconds",
            "Hedging response latency in seconds",
            ["source", "is_synthetic"],
            buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
        )
        self._benefit_ms = get_or_create_histogram(
            f"{self.PREFIX}_hedging_benefit_ms",
            "Latency improvement achieved by hedging in milliseconds",
            ["is_synthetic"],
            buckets=(10, 50, 100, 250, 500, 1000, 2500),
        )
        self._candidate_tried = get_or_create_counter(
            f"{self.PREFIX}_hedging_candidate_tried_total",
            "Number of times each candidate was tried",
            ["candidate", "is_synthetic"],
        )
        self._mismatch_total = get_or_create_counter(
            f"{self.PREFIX}_hedging_mismatch_total",
            "Number of result mismatches detected between candidates",
            ["mismatch_type", "is_synthetic"],
        )

    def record_execution(self, mode: str) -> None:
        """Record a hedging execution."""
        try:
            is_synthetic = self._get_synthetic_label()
            self._total.labels(mode=mode, is_synthetic=is_synthetic).inc()
        except Exception:
            logger.debug("hedging.metric_record_failed", metric="execution")

    def record_success(self, source: str, latency_seconds: float) -> None:
        """Record a successful hedging execution with latency."""
        try:
            is_synthetic = self._get_synthetic_label()
            self._success_total.labels(source=source, is_synthetic=is_synthetic).inc()
            self._latency_seconds.labels(
                source=source, is_synthetic=is_synthetic
            ).observe(latency_seconds)
        except Exception:
            logger.debug("hedging.metric_record_failed", metric="success")

    def record_failure(self) -> None:
        """Record all-candidates-failed."""
        try:
            is_synthetic = self._get_synthetic_label()
            self._failed_total.labels(is_synthetic=is_synthetic).inc()
        except Exception:
            logger.debug("hedging.metric_record_failed", metric="failure")

    def record_timeout(self) -> None:
        """Record a hedging timeout."""
        try:
            is_synthetic = self._get_synthetic_label()
            self._timeout_total.labels(is_synthetic=is_synthetic).inc()
        except Exception:
            logger.debug("hedging.metric_record_failed", metric="timeout")

    def record_non_retryable(self) -> None:
        """Record non-retryable error abort."""
        try:
            is_synthetic = self._get_synthetic_label()
            self._non_retryable_total.labels(is_synthetic=is_synthetic).inc()
        except Exception:
            logger.debug("hedging.metric_record_failed", metric="non_retryable")

    def record_hedged(self) -> None:
        """Record secondary candidate selected."""
        try:
            is_synthetic = self._get_synthetic_label()
            self._hedged_total.labels(is_synthetic=is_synthetic).inc()
        except Exception:
            logger.debug("hedging.metric_record_failed", metric="hedged")

    def record_disabled(self, load_level: str) -> None:
        """Record hedging disabled due to high load."""
        try:
            is_synthetic = self._get_synthetic_label()
            self._disabled_due_to_load.labels(
                load_level=load_level, is_synthetic=is_synthetic
            ).inc()
        except Exception:
            logger.debug("hedging.metric_record_failed", metric="disabled")

    def record_benefit(self, benefit_ms: float) -> None:
        """Record latency improvement from hedging."""
        try:
            if benefit_ms > 0:
                is_synthetic = self._get_synthetic_label()
                self._benefit_ms.labels(is_synthetic=is_synthetic).observe(benefit_ms)
        except Exception:
            logger.debug("hedging.metric_record_failed", metric="benefit")

    def record_candidate_tried(self, candidate: str) -> None:
        """Record candidate execution."""
        try:
            is_synthetic = self._get_synthetic_label()
            self._candidate_tried.labels(
                candidate=candidate, is_synthetic=is_synthetic
            ).inc()
        except Exception:
            logger.debug("hedging.metric_record_failed", metric="candidate_tried")

    def record_result_mismatch(self, mismatch_type: str) -> None:
        """Record result mismatch between candidates."""
        try:
            is_synthetic = self._get_synthetic_label()
            self._mismatch_total.labels(
                mismatch_type=mismatch_type, is_synthetic=is_synthetic
            ).inc()
        except Exception:
            logger.debug("hedging.metric_record_failed", metric="mismatch")
