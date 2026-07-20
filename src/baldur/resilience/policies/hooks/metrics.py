"""
Metrics Hook — record the pipeline execution result as Prometheus metrics.

Registered as a PolicyComposer Hook to collect success/failure/rejection
metrics. prometheus_client is lazily imported so the hook works without error
where it is not installed.

Fail-open principle: if prometheus_client fails to import, metric collection is
skipped.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from baldur.interfaces.resilience_policy import PolicyResult

if TYPE_CHECKING:
    from baldur.interfaces.resilience_policy import PolicyContext

logger = structlog.get_logger()

# Lazy-initialized Prometheus instruments
_metrics_initialized = False
_pipeline_success_total = None
_pipeline_failure_total = None
_pipeline_rejected_total = None
_pipeline_duration_seconds = None


def _ensure_metrics() -> bool:
    """Lazily initialize the Prometheus instruments. False if unavailable."""
    global _metrics_initialized, _pipeline_success_total, _pipeline_failure_total
    global _pipeline_rejected_total, _pipeline_duration_seconds

    if _metrics_initialized:
        return _pipeline_success_total is not None

    _metrics_initialized = True

    try:
        from baldur.metrics.registry import (
            get_or_create_counter,
            get_or_create_histogram,
        )

        _pipeline_success_total = get_or_create_counter(
            "baldur_pipeline_success_total",
            "Total successful pipeline executions",
            ["pipeline"],
        )
        _pipeline_failure_total = get_or_create_counter(
            "baldur_pipeline_failure_total",
            "Total failed pipeline executions",
            ["pipeline", "error_type"],
        )
        _pipeline_rejected_total = get_or_create_counter(
            "baldur_pipeline_rejected_total",
            "Total rejected pipeline executions",
            ["pipeline", "guard"],
        )
        _pipeline_duration_seconds = get_or_create_histogram(
            "baldur_pipeline_duration_seconds",
            "Pipeline execution duration in seconds",
            ["pipeline"],
        )
        return True
    except ImportError:
        logger.debug("metrics.collection_disabled")
        return False


class MetricsHook:
    """Prometheus metrics hook.

    Observes only the end-to-end pipeline result. Metric collection is skipped
    when prometheus_client is not installed.
    """

    def on_execute(
        self, policy_name: str, attempt: int, context: PolicyContext | None = None
    ) -> None:
        """Execution start — no metric."""

    def on_success(
        self,
        policy_name: str,
        result: PolicyResult,
        context: PolicyContext | None = None,
    ) -> None:
        """Record metrics on pipeline success."""
        if not _ensure_metrics():
            return

        _pipeline_success_total.labels(pipeline=policy_name).inc()  # type: ignore[union-attr]
        _pipeline_duration_seconds.labels(pipeline=policy_name).observe(  # type: ignore[union-attr]
            result.total_duration_ms / 1000.0
        )

    def on_failure(
        self,
        policy_name: str,
        error: Exception,
        attempt: int,
        context: PolicyContext | None = None,
    ) -> None:
        """Record metrics on pipeline failure."""
        if not _ensure_metrics():
            return

        error_type = type(error).__name__
        if _pipeline_failure_total is not None:
            _pipeline_failure_total.labels(
                pipeline=policy_name, error_type=error_type
            ).inc()

    def on_retry(
        self,
        policy_name: str,
        attempt: int,
        delay: float,
        context: PolicyContext | None = None,
    ) -> None:
        """Retry — unused at the Composer level."""

    def on_reject(
        self, guard_name: str, reason: str, context: PolicyContext | None = None
    ) -> None:
        """Record metrics on pipeline rejection."""
        if not _ensure_metrics():
            return

        _pipeline_rejected_total.labels(pipeline="composer", guard=guard_name).inc()  # type: ignore[union-attr]
