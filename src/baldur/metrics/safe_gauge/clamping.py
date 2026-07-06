"""
Value Clamping Utilities for Metrics.

Prevents invalid metric values (negative counts, out-of-range percentages).
"""

from __future__ import annotations

from typing import Any

import structlog

logger = structlog.get_logger()


def clamp_non_negative(value: float, metric_name: str = "unknown") -> float:
    """
    Clamp a value to be non-negative (>= 0).

    Use this for count/quantity metrics that should never be negative.

    Args:
        value: The value to clamp
        metric_name: Name of the metric (for logging)

    Returns:
        The value clamped to >= 0
    """
    if value < 0:
        logger.warning(
            "safe_gauge.clamping_negative_value_metric",
            clamped_value=value,
            metric_name=metric_name,
        )
        return 0.0
    return float(value)


def clamp_percentage(value: float, metric_name: str = "unknown") -> float:
    """
    Clamp a value to be within 0-100 range.

    Use this for percentage/rate metrics.

    Args:
        value: The value to clamp
        metric_name: Name of the metric (for logging)

    Returns:
        The value clamped to 0-100
    """
    if value < 0:
        logger.warning(
            "safe_gauge.clamping_negative_percentage_metric",
            clamped_value=value,
            metric_name=metric_name,
        )
        return 0.0
    if value > 100:
        logger.warning(
            "safe_gauge.clamping_percentage_metric",
            clamped_value=value,
            metric_name=metric_name,
        )
        return 100.0
    return float(value)


def safe_set_gauge(
    gauge: Any,
    value: float,
    clamp_type: str = "non_negative",
    metric_name: str = "unknown",
    **labels,
) -> None:
    """
    Safely set a gauge value with clamping.

    This is a convenience function for setting gauge values with
    automatic clamping to prevent invalid values.

    Args:
        gauge: Prometheus Gauge instance
        value: Value to set
        clamp_type: Type of clamping ('non_negative', 'percentage', 'none')
        metric_name: Name of the metric (for logging)
        **labels: Label key-value pairs

    Example:
        >>> safe_set_gauge(dlq_by_status_gauge, count, "non_negative", "dlq_by_status", status="pending")
    """
    if gauge is None:
        return

    try:
        if clamp_type == "non_negative":
            value = clamp_non_negative(value, metric_name)
        elif clamp_type == "percentage":
            value = clamp_percentage(value, metric_name)
        # 'none' or other: no clamping

        if labels:
            gauge.labels(**labels).set(value)
        else:
            gauge.set(value)
    except Exception as e:
        logger.warning(
            "safe_gauge.set_gauge_failed",
            metric_name=metric_name,
            error=e,
        )


__all__ = [
    "clamp_non_negative",
    "clamp_percentage",
    "safe_set_gauge",
]
