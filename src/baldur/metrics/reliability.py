"""
Metric Reliability Levels.

Documents the accuracy level of each metric type.
"""

from __future__ import annotations

from enum import Enum


class MetricReliability(str, Enum):
    """
    메트릭 신뢰도 레벨.

    각 메트릭이 제공하는 정확도 수준을 나타냅니다.
    """

    EXACT = "exact"  # 100% 정확, 원본과 동일
    EVENTUAL = "eventual"  # ~99%, 재시작 시 동기화
    APPROXIMATE = "approx"  # ~95%, 샘플링 또는 추정


# 메트릭별 신뢰도 매핑
METRIC_RELIABILITY_MAP: dict[str, MetricReliability] = {
    # Counter: 누적값, 증가만 하므로 100% 정확
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
    # Histogram: 관측 시점 기록, 100% 정확
    "recovery_time_seconds": MetricReliability.EXACT,
    "retry_attempts_distribution": MetricReliability.EXACT,
    "retry_delay_seconds": MetricReliability.EXACT,
    "human_review_queue_time_seconds": MetricReliability.EXACT,
    "circuit_breaker_open_duration_seconds": MetricReliability.EXACT,
    "replay_duration_seconds": MetricReliability.EXACT,
    # Gauge: 상태값, 재시작 시 동기화 (~99% 정확)
    "dlq_pending_count": MetricReliability.EVENTUAL,
    "dlq_items_by_status": MetricReliability.EVENTUAL,
    "circuit_breaker_state": MetricReliability.EVENTUAL,
    "retry_success_rate": MetricReliability.EVENTUAL,
}


def get_metric_reliability(metric_name: str) -> MetricReliability:
    """
    메트릭 이름에 대한 신뢰도 레벨을 반환합니다.

    Args:
        metric_name: 메트릭 이름 (prefix 제외)

    Returns:
        MetricReliability 레벨

    Example:
        >>> reliability = get_metric_reliability("dlq_items_total")
        >>> print(reliability.value)  # "exact"
    """
    return METRIC_RELIABILITY_MAP.get(metric_name, MetricReliability.APPROXIMATE)


def get_reliability_description(reliability: MetricReliability) -> str:
    """
    신뢰도 레벨에 대한 설명을 반환합니다.

    Args:
        reliability: MetricReliability 레벨

    Returns:
        사람이 읽을 수 있는 설명
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
