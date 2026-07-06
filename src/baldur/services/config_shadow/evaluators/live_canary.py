"""
Live Canary Evaluator.

Evaluates the real impact of a configuration change based on live metrics
from the canary nodes. Implements the ConfigEvaluator Protocol and queries
real-time data from the TimeSeriesMetricsProvider using the
EvaluationContext's time_window_seconds + labels.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from baldur.models.canary import PassCriteria
from baldur.services.config_shadow.metrics_provider import (
    TimeSeriesMetricsProvider,
)
from baldur.services.config_shadow.models import EvaluationContext, EvaluatorResult
from baldur.utils.time import utc_now

logger = logging.getLogger(__name__)


class LiveCanaryEvaluator:
    """Evaluates the real impact of a config change from live canary metrics.

    Implements the ConfigEvaluator Protocol and queries real-time data from
    the TimeSeriesMetricsProvider using the EvaluationContext's
    time_window_seconds + labels.

    Reads PassCriteria threshold values as the pass/fail criteria.
    """

    def __init__(
        self,
        metrics_provider: TimeSeriesMetricsProvider,
        pass_criteria: PassCriteria | None = None,
    ) -> None:
        self._metrics = metrics_provider
        self._criteria = pass_criteria or PassCriteria()

    @property
    def name(self) -> str:
        return "live_canary"

    @property
    def event_types(self) -> list[str]:
        return ["canary_metrics"]

    def evaluate(self, context: EvaluationContext) -> EvaluatorResult:
        """Query live metrics and compare baseline vs candidate behavior."""
        now = utc_now()
        start = now - timedelta(seconds=context.time_window_seconds)
        warnings: list[str] = []
        criteria = self._criteria

        # 1. Weighted error-rate scalar query
        baseline_error = self._metrics.query_error_rate_aggregated(
            service_name=context.service_name,
            start=start,
            end=now,
            labels=context.baseline_labels,
        )
        candidate_error = self._metrics.query_error_rate_aggregated(
            service_name=context.service_name,
            start=start,
            end=now,
            labels=context.candidate_labels,
        )
        error_delta = candidate_error - baseline_error

        # 2. Total request count query
        candidate_request_count = self._metrics.query_request_count(
            service_name=context.service_name,
            start=start,
            end=now,
            labels=context.candidate_labels,
        )

        # 3. Latency P95/P99 scalar query
        baseline_p95 = self._metrics.query_latency_aggregated(
            service_name=context.service_name,
            start=start,
            end=now,
            percentile=0.95,
            labels=context.baseline_labels,
        )
        candidate_p95 = self._metrics.query_latency_aggregated(
            service_name=context.service_name,
            start=start,
            end=now,
            percentile=0.95,
            labels=context.candidate_labels,
        )
        baseline_p99 = self._metrics.query_latency_aggregated(
            service_name=context.service_name,
            start=start,
            end=now,
            percentile=0.99,
            labels=context.baseline_labels,
        )
        candidate_p99 = self._metrics.query_latency_aggregated(
            service_name=context.service_name,
            start=start,
            end=now,
            percentile=0.99,
            labels=context.candidate_labels,
        )

        # 4. Data sufficiency → confidence
        confidence, conf_warnings = self._calculate_confidence(
            candidate_request_count,
        )
        warnings.extend(conf_warnings)

        # 5. Pass determination (PassCriteria thresholds)
        passed = True
        details_parts: list[str] = []

        # 5a. Minimum traffic floor — too little traffic is "not enough
        # evidence", never a pass: with a near-empty window the threshold
        # comparisons below are vacuous (all-zero metrics look healthy).
        if candidate_request_count < criteria.min_requests_required:
            passed = False
            details_parts.append(
                f"Insufficient canary traffic ({candidate_request_count} < "
                f"{criteria.min_requests_required} required) — "
                f"not enough evidence to promote"
            )

        # 5b. Error rate absolute threshold
        if candidate_error > criteria.error_rate_absolute_max:
            passed = False
            details_parts.append(
                f"Canary error rate {candidate_error:.3f} > "
                f"threshold {criteria.error_rate_absolute_max:.3f}"
            )

        # 5c. Error rate increase threshold
        if error_delta > criteria.error_rate_increase_max:
            passed = False
            details_parts.append(
                f"Error rate increase {error_delta:.3f} > "
                f"threshold {criteria.error_rate_increase_max:.3f}"
            )

        # 5d. P95 latency absolute increase
        p95_delta = candidate_p95 - baseline_p95
        if p95_delta > criteria.latency_p95_delta_ms:
            passed = False
            details_parts.append(
                f"P95 latency delta {p95_delta:.1f}ms > "
                f"threshold {criteria.latency_p95_delta_ms:.1f}ms"
            )

        # 5e. P99 latency percentage increase
        p99_pct = (
            (candidate_p99 - baseline_p99) / baseline_p99 if baseline_p99 > 0 else 0.0
        )
        if baseline_p99 > 0 and p99_pct > criteria.latency_p99_delta_pct:
            passed = False
            details_parts.append(
                f"P99 latency increased by {p99_pct:.1%} > "
                f"threshold {criteria.latency_p99_delta_pct:.1%}"
            )

        if passed:
            details_parts.append(
                f"Canary healthy: error_rate={candidate_error:.3f}, "
                f"delta={error_delta:+.3f}, "
                f"p95={candidate_p95:.1f}ms, p99={candidate_p99:.1f}ms, "
                f"requests={candidate_request_count}"
            )

        return EvaluatorResult(
            evaluator_name=self.name,
            passed=passed,
            confidence_score=confidence,
            baseline_metrics={
                "error_rate": baseline_error,
                "latency_p95_ms": baseline_p95,
                "latency_p99_ms": baseline_p99,
            },
            candidate_metrics={
                "error_rate": candidate_error,
                "request_count": candidate_request_count,
                "latency_p95_ms": candidate_p95,
                "latency_p99_ms": candidate_p99,
            },
            delta={
                "error_rate_delta": error_delta,
                "p95_delta_ms": p95_delta,
                "p99_delta_pct": p99_pct,
            },
            details="; ".join(details_parts),
            warnings=warnings,
        )

    def _calculate_confidence(
        self,
        request_count: int,
    ) -> tuple[float, list[str]]:
        """Calculate confidence from the request volume."""
        warnings: list[str] = []
        min_requests = self._criteria.min_requests_required

        if request_count < min_requests:
            warnings.append(
                f"Low request volume ({request_count} < {min_requests}). "
                f"Confidence reduced."
            )
            if request_count == 0:
                return 0.1, warnings
            return 0.4, warnings

        if request_count < min_requests * 5:
            return 0.7, warnings

        return 0.95, warnings
