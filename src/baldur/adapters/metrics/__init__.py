"""
Metric Source Adapters for Baldur System.

This module provides pluggable adapters for collecting metrics from various
data sources without direct dependency on user's database schema.
"""

from baldur.adapters.metrics.base import MetricSourceAdapter
from baldur.adapters.metrics.factory import (
    configure_metric_adapter,
    get_metric_adapter,
    reset_metric_adapter,
)

__all__ = [
    "MetricSourceAdapter",
    "get_metric_adapter",
    "configure_metric_adapter",
    "reset_metric_adapter",
]
