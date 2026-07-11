"""
Config Shadow time-series metrics providers.

Concrete ``TimeSeriesMetricsProvider`` implementations registered via
``set_metrics_provider()``.
"""

from __future__ import annotations

from baldur.services.config_shadow.providers.prometheus import (
    PrometheusTimeSeriesProvider,
    setup_prometheus_metrics_provider,
)

__all__ = [
    "PrometheusTimeSeriesProvider",
    "setup_prometheus_metrics_provider",
]
