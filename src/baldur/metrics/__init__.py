"""
Metrics collection and export for the baldur system.

This module provides Prometheus metrics, event handlers, and other
observability tools.

Status: Internal
"""

from baldur.metrics.decorators import (
    track_counter,
    track_dlq_creation,
    track_dlq_resolution,
    track_execution_time,
    track_replay,
)
from baldur.metrics.event_handlers import (
    CircuitBreakerEventHandler,
    DLQMetricEventHandler,
    ReplayEventHandler,
    reset_event_handler_cache,
)
from baldur.metrics.prometheus import (
    BaldurMetrics,
    get_metrics,
)
from baldur.metrics.reconciler import (
    MetricReconciler,
    SyncResult,
    get_reconciler,
)
from baldur.metrics.registry import (
    get_or_create_counter,
    get_or_create_gauge,
    get_or_create_histogram,
    register_domain,
    resolve_domain_label,
)
from baldur.metrics.reliability import (
    MetricReliability,
    get_metric_reliability,
)

# Import from the refactored safe_gauge package
from baldur.metrics.safe_gauge import (
    NoOpGaugeChild,
    SafeGauge,
    SafeGaugeChild,
    SyncInfo,
    SyncStatus,
    clamp_non_negative,
    clamp_percentage,
    safe_set_gauge,
)

# Import from new location to avoid DeprecationWarning internally
from baldur.utils.jitter import (
    JitterConfig,
    calculate_jitter,
    sleep_with_jitter,
    with_jitter,
)

__all__ = [
    # Registry
    "get_or_create_counter",
    "get_or_create_gauge",
    "get_or_create_histogram",
    "register_domain",
    "resolve_domain_label",
    # Prometheus metrics
    "BaldurMetrics",
    "get_metrics",
    # Event handlers
    "DLQMetricEventHandler",
    "CircuitBreakerEventHandler",
    "ReplayEventHandler",
    "reset_event_handler_cache",
    # Safe Gauge (core)
    "SafeGauge",
    "SafeGaugeChild",
    # Safe Gauge (sync)
    "SyncStatus",
    "SyncInfo",
    # Safe Gauge (clamping)
    "clamp_non_negative",
    "clamp_percentage",
    "safe_set_gauge",
    # Safe Gauge (noop)
    "NoOpGaugeChild",
    # Decorators
    "track_dlq_creation",
    "track_dlq_resolution",
    "track_replay",
    "track_execution_time",
    "track_counter",
    # Jitter
    "with_jitter",
    "calculate_jitter",
    "sleep_with_jitter",
    "JitterConfig",
    # Reconciler
    "MetricReconciler",
    "SyncResult",
    "get_reconciler",
    # Reliability
    "MetricReliability",
    "get_metric_reliability",
]
