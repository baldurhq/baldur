"""
Prometheus metric exposure.

Exposes backpressure-related metrics in Prometheus format.
Degrades to a no-op when the prometheus_client library is absent.
"""

from __future__ import annotations

import importlib.util

import structlog

from baldur.scaling.config import BackpressureSettings, get_backpressure_settings

logger = structlog.get_logger()

# Use prometheus_client when it is available
HAS_PROMETHEUS = importlib.util.find_spec("prometheus_client") is not None


class BackpressureMetrics:
    """
    Backpressure Prometheus metrics.

    Metrics:
    - baldur_queue_depth: current queue depth
    - baldur_processing_rate: processing rate (items/second)
    - baldur_backpressure_level: backpressure level (0-4)
    - baldur_processed_total: total items processed
    - baldur_dropped_total: total items dropped
    - baldur_processing_duration_seconds: processing duration histogram
    """

    def __init__(
        self,
        settings: BackpressureSettings | None = None,
    ):
        """
        Args:
            settings: Backpressure settings
        """
        self._settings = settings or get_backpressure_settings()
        self._prefix = self._settings.metrics_prefix

        if not HAS_PROMETHEUS:
            logger.warning("backpressure_metrics.prometheus_unavailable")
            return

        if not self._settings.metrics_enabled:
            return

        from baldur.metrics.registry import (
            get_or_create_counter,
            get_or_create_gauge,
            get_or_create_histogram,
        )

        self.queue_depth = get_or_create_gauge(
            f"{self._prefix}queue_depth",
            "Current queue depth",
            ["queue_name"],
        )

        self.processing_rate = get_or_create_gauge(
            f"{self._prefix}processing_rate",
            "Current processing rate (items/second)",
            ["component"],
        )

        self.backpressure_level = get_or_create_gauge(
            f"{self._prefix}backpressure_level",
            "Current backpressure level",
            ["component"],
        )

        self.processed_total = get_or_create_counter(
            f"{self._prefix}processed_total",
            "Total processed items",
            ["component", "status"],
        )

        self.dropped_total = get_or_create_counter(
            f"{self._prefix}dropped_total",
            "Total dropped items",
            ["component", "reason"],
        )

        self.dropped_by_tier_total = get_or_create_counter(
            f"{self._prefix}rate_controller_dropped_total",
            "Total dropped items per tier for starvation monitoring",
            ["tier"],
        )

        self.processed_by_tier_total = get_or_create_counter(
            f"{self._prefix}rate_controller_processed_total",
            "Total processed items per tier for starvation monitoring",
            ["tier"],
        )

        self.processing_duration = get_or_create_histogram(
            f"{self._prefix}processing_duration_seconds",
            "Processing duration in seconds",
            ["component", "operation"],
            buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0),
        )

    def set_queue_depth(self, queue_name: str, depth: int) -> None:
        """Set the queue depth."""
        if HAS_PROMETHEUS and self._settings.metrics_enabled:
            self.queue_depth.labels(queue_name=queue_name).set(depth)

    def set_processing_rate(self, component: str, rate: float) -> None:
        """Set the processing rate."""
        if HAS_PROMETHEUS and self._settings.metrics_enabled:
            self.processing_rate.labels(component=component).set(rate)

    def set_backpressure_level(self, component: str, level: int) -> None:
        """
        Set the backpressure level.

        Args:
            component: Component name
            level: Level value (0=NONE, 1=LOW, 2=MEDIUM, 3=HIGH, 4=CRITICAL)
        """
        if HAS_PROMETHEUS and self._settings.metrics_enabled:
            self.backpressure_level.labels(component=component).set(level)

    def inc_processed(self, component: str, status: str = "success") -> None:
        """Increment the processed counter."""
        if HAS_PROMETHEUS and self._settings.metrics_enabled:
            self.processed_total.labels(component=component, status=status).inc()

    def inc_dropped(self, component: str, reason: str = "backpressure") -> None:
        """Increment the dropped counter."""
        if HAS_PROMETHEUS and self._settings.metrics_enabled:
            self.dropped_total.labels(component=component, reason=reason).inc()

    def inc_dropped_by_tier(self, tier: str) -> None:
        """Increment the per-tier rejection counter (for starvation detection)."""
        if HAS_PROMETHEUS and self._settings.metrics_enabled:
            self.dropped_by_tier_total.labels(tier=tier).inc()

    def inc_processed_by_tier(self, tier: str) -> None:
        """Increment the per-tier processed counter (starvation-alert denominator)."""
        if HAS_PROMETHEUS and self._settings.metrics_enabled:
            self.processed_by_tier_total.labels(tier=tier).inc()

    def observe_duration(
        self,
        component: str,
        operation: str,
        duration: float,
    ) -> None:
        """
        Record the processing duration.

        Args:
            component: Component name
            operation: Operation name
            duration: Elapsed time (seconds)
        """
        if HAS_PROMETHEUS and self._settings.metrics_enabled:
            self.processing_duration.labels(
                component=component,
                operation=operation,
            ).observe(duration)


# =============================================================================
# Singleton
# =============================================================================

from baldur.utils.singleton import make_singleton_factory

get_backpressure_metrics, configure_backpressure_metrics, reset_backpressure_metrics = (
    make_singleton_factory("backpressure_metrics", BackpressureMetrics)
)
