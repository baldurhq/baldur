"""
Metric Reliability Levels.

Documents the accuracy level of each metric type.
"""

from __future__ import annotations

from enum import Enum


class MetricReliability(str, Enum):
    """
    Metric reliability level.

    Indicates the accuracy level each metric provides.
    """

    EXACT = "exact"  # 100% accurate, identical to the source
    EVENTUAL = "eventual"  # ~99%, synced on restart
    APPROXIMATE = "approx"  # ~95%, sampled or estimated


# Per-metric reliability mapping
METRIC_RELIABILITY_MAP: dict[str, MetricReliability] = {
    # Counter: cumulative and monotonic, so 100% accurate
    "dlq_items_total": MetricReliability.EXACT,
    "dlq_created_total": MetricReliability.EXACT,
    "retry_outcomes_total": MetricReliability.EXACT,
    "sla_breach_total": MetricReliability.EXACT,
    "circuit_breaker_failures_total": MetricReliability.EXACT,
    "circuit_breaker_trips_total": MetricReliability.EXACT,
    "circuit_breaker_transitions_total": MetricReliability.EXACT,
    "replay_attempts_total": MetricReliability.EXACT,
    "replay_outcomes_total": MetricReliability.EXACT,
    "security_incidents_total": MetricReliability.EXACT,
    # Histogram: recorded at observation time, 100% accurate
    "recovery_time_seconds": MetricReliability.EXACT,
    "retry_attempts_distribution": MetricReliability.EXACT,
    "retry_delay_seconds": MetricReliability.EXACT,
    "human_review_queue_time_seconds": MetricReliability.EXACT,
    "circuit_breaker_open_duration_seconds": MetricReliability.EXACT,
    "replay_duration_seconds": MetricReliability.EXACT,
    # Gauge: state value, synced on restart (~99% accurate)
    "dlq_pending_count": MetricReliability.EVENTUAL,
    "dlq_items_by_status": MetricReliability.EVENTUAL,
    "circuit_breaker_state": MetricReliability.EVENTUAL,
    "retry_success_rate": MetricReliability.EVENTUAL,
}


def get_metric_reliability(metric_name: str) -> MetricReliability:
    """
    Return the reliability level for a metric name.

    Args:
        metric_name: Metric name (without the prefix)

    Returns:
        MetricReliability level

    Example:
        >>> reliability = get_metric_reliability("dlq_items_total")
        >>> print(reliability.value)  # "exact"
    """
    return METRIC_RELIABILITY_MAP.get(metric_name, MetricReliability.APPROXIMATE)


def get_reliability_description(reliability: MetricReliability) -> str:
    """
    Return a description of a reliability level.

    Args:
        reliability: MetricReliability level

    Returns:
        Human-readable description
    """
    descriptions = {
        MetricReliability.EXACT: "100% accurate - matches source data exactly",
        MetricReliability.EVENTUAL: "~99% accurate - synchronized on restart",
        MetricReliability.APPROXIMATE: "~95% accurate - sampled or estimated",
    }
    return descriptions.get(reliability, "Unknown reliability level")


__all__ = [
    "MetricReliability",
    "METRIC_RELIABILITY_MAP",
    "get_metric_reliability",
    "get_reliability_description",
]
