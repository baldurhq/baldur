"""
Retry metric recorder — metric definitions and recording for retry and recovery.

Owns all Retry-related and Recovery-related Prometheus metrics.
Recovery metrics (recovery_time, sla_breach) are closely related to retry
lifecycle, so they belong here rather than in a separate recorder.
"""

from __future__ import annotations

from datetime import datetime

import structlog

from baldur.metrics.recorders.base import BaseMetricRecorder
from baldur.metrics.registry import (
    get_or_create_counter,
    get_or_create_gauge,
    get_or_create_histogram,
)

logger = structlog.get_logger()

__all__ = ["RetryMetricRecorder"]


class RetryMetricRecorder(BaseMetricRecorder):
    """Retry and Recovery metric definitions and recording (5 methods)."""

    def __init__(self) -> None:
        self._attempts_histogram = get_or_create_histogram(
            f"{self.PREFIX}_retry_attempts_distribution",
            "Number of retry attempts before resolution",
            ["domain", "is_synthetic"],
            buckets=(1, 2, 3, 4, 5, 6, 7, 8, 9, 10),
        )
        self._outcomes_total = get_or_create_counter(
            f"{self.PREFIX}_retry_outcomes_total",
            "Retry outcomes by domain and result",
            ["domain", "outcome", "is_synthetic"],
        )
        self._success_rate = get_or_create_gauge(
            f"{self.PREFIX}_retry_success_rate",
            "Percentage of successful retries (0-100)",
            ["domain"],
        )
        self._delay_seconds = get_or_create_histogram(
            f"{self.PREFIX}_retry_delay_seconds",
            "Retry delay in seconds",
            ["domain"],
            buckets=(1, 5, 10, 30, 60, 120, 300, 600),
        )
        # Recovery metrics — closely related to retry lifecycle
        self._recovery_time_seconds = get_or_create_histogram(
            f"{self.PREFIX}_recovery_time_seconds",
            "Time from failure to resolution in seconds",
            ["domain", "resolution_type"],
            buckets=(60, 300, 900, 1800, 3600, 7200, 14400, 28800, 86400),
        )
        self._sla_breach_total = get_or_create_counter(
            f"{self.PREFIX}_sla_breach_total",
            "Total SLA breaches detected",
            ["domain"],
        )
        self._human_review_queue_time = get_or_create_histogram(
            f"{self.PREFIX}_human_review_queue_time_seconds",
            "Time items wait in queue for human review",
            ["domain"],
            buckets=(300, 900, 1800, 3600, 7200, 14400, 28800),
        )

    def record_attempt(self, domain: str, attempt_count: int, outcome: str) -> None:
        """Record a retry attempt outcome."""
        try:
            is_synthetic = self._get_synthetic_label()
            self._attempts_histogram.labels(
                domain=domain, is_synthetic=is_synthetic
            ).observe(attempt_count)
            self._outcomes_total.labels(
                domain=domain, outcome=outcome, is_synthetic=is_synthetic
            ).inc()
            logger.debug(
                "metrics.retry_recorded",
                healing_domain=domain,
                attempt_count=attempt_count,
                outcome=outcome,
                is_synthetic=is_synthetic,
            )
        except Exception as e:
            logger.warning("metrics.record_retry_metric_failed", error=e)

    def record_retry(
        self, domain: str, success: bool, delay: float | None = None
    ) -> None:
        """Record a retry attempt with optional delay."""
        try:
            is_synthetic = self._get_synthetic_label()
            outcome = "success" if success else "failure"
            self._outcomes_total.labels(
                domain=domain, outcome=outcome, is_synthetic=is_synthetic
            ).inc()
            if delay is not None:
                self._delay_seconds.labels(domain=domain).observe(delay)
        except Exception as e:
            logger.warning("metrics.record_retry_metric_failed", error=e)

    def set_success_rate(self, domain: str, rate: float) -> None:
        """Set the retry success rate for a domain (0-100)."""
        try:
            safe_rate = self._clamp_percentage(rate, f"retry_success_rate[{domain}]")
            self._success_rate.labels(domain=domain).set(safe_rate)
        except Exception as e:
            logger.warning("metrics.set_retry_success_failed", error=e)

    def record_recovery_duration(
        self,
        domain: str,
        resolution_type: str,
        duration_seconds: float,
    ) -> None:
        """Record time from failure to resolution (pre-computed duration)."""
        try:
            self._recovery_time_seconds.labels(
                domain=domain, resolution_type=resolution_type
            ).observe(duration_seconds)
            logger.debug(
                "metrics.recovery_time_recorded",
                healing_domain=domain,
                resolution_type=resolution_type,
                duration=duration_seconds,
            )
        except Exception as e:
            logger.warning("metrics.record_recovery_time_failed", error=e)

    def record_recovery_time(
        self,
        domain: str,
        resolution_type: str,
        created_at: datetime,
        resolved_at: datetime,
    ) -> None:
        """Record time from failure to resolution."""
        duration = (resolved_at - created_at).total_seconds()
        self.record_recovery_duration(domain, resolution_type, duration)

    def record_sla_breach(self, domain: str) -> None:
        """Record an SLA breach event."""
        try:
            self._sla_breach_total.labels(domain=domain).inc()
            logger.info(
                "metrics.sla_breach_recorded",
                healing_domain=domain,
            )
        except Exception as e:
            logger.warning("metrics.record_sla_breach_failed", error=e)
