"""
Metrics collection and export for the baldur system.

This module provides Prometheus metrics, event handlers, and other
observability tools.

Status: Internal
"""

# Lazy barrel — register names in `_LAZY_IMPORTS`; never add an eager
# top-level `from baldur.X import ...` here (defeats the lazy import path
# and is caught by the import-weight gate).

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
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
    from baldur.utils.jitter import (
        JitterConfig,
        calculate_jitter,
        sleep_with_jitter,
        with_jitter,
    )

_LAZY_IMPORTS: dict[str, tuple[str, str]] = {
    "track_counter": ("baldur.metrics.decorators", "track_counter"),
    "track_dlq_creation": ("baldur.metrics.decorators", "track_dlq_creation"),
    "track_dlq_resolution": ("baldur.metrics.decorators", "track_dlq_resolution"),
    "track_execution_time": ("baldur.metrics.decorators", "track_execution_time"),
    "track_replay": ("baldur.metrics.decorators", "track_replay"),
    "CircuitBreakerEventHandler": (
        "baldur.metrics.event_handlers",
        "CircuitBreakerEventHandler",
    ),
    "DLQMetricEventHandler": ("baldur.metrics.event_handlers", "DLQMetricEventHandler"),
    "ReplayEventHandler": ("baldur.metrics.event_handlers", "ReplayEventHandler"),
    "reset_event_handler_cache": (
        "baldur.metrics.event_handlers",
        "reset_event_handler_cache",
    ),
    "BaldurMetrics": ("baldur.metrics.prometheus", "BaldurMetrics"),
    "get_metrics": ("baldur.metrics.prometheus", "get_metrics"),
    "MetricReconciler": ("baldur.metrics.reconciler", "MetricReconciler"),
    "SyncResult": ("baldur.metrics.reconciler", "SyncResult"),
    "get_reconciler": ("baldur.metrics.reconciler", "get_reconciler"),
    "get_or_create_counter": ("baldur.metrics.registry", "get_or_create_counter"),
    "get_or_create_gauge": ("baldur.metrics.registry", "get_or_create_gauge"),
    "get_or_create_histogram": ("baldur.metrics.registry", "get_or_create_histogram"),
    "register_domain": ("baldur.metrics.registry", "register_domain"),
    "resolve_domain_label": ("baldur.metrics.registry", "resolve_domain_label"),
    "MetricReliability": ("baldur.metrics.reliability", "MetricReliability"),
    "get_metric_reliability": ("baldur.metrics.reliability", "get_metric_reliability"),
    "NoOpGaugeChild": ("baldur.metrics.safe_gauge", "NoOpGaugeChild"),
    "SafeGauge": ("baldur.metrics.safe_gauge", "SafeGauge"),
    "SafeGaugeChild": ("baldur.metrics.safe_gauge", "SafeGaugeChild"),
    "SyncInfo": ("baldur.metrics.safe_gauge", "SyncInfo"),
    "SyncStatus": ("baldur.metrics.safe_gauge", "SyncStatus"),
    "clamp_non_negative": ("baldur.metrics.safe_gauge", "clamp_non_negative"),
    "clamp_percentage": ("baldur.metrics.safe_gauge", "clamp_percentage"),
    "safe_set_gauge": ("baldur.metrics.safe_gauge", "safe_set_gauge"),
    "JitterConfig": ("baldur.utils.jitter", "JitterConfig"),
    "calculate_jitter": ("baldur.utils.jitter", "calculate_jitter"),
    "sleep_with_jitter": ("baldur.utils.jitter", "sleep_with_jitter"),
    "with_jitter": ("baldur.utils.jitter", "with_jitter"),
}


def __getattr__(name: str):
    if name in _LAZY_IMPORTS:
        module_path, attr_name = _LAZY_IMPORTS[name]
        # Resolve live on each access (no globals() memoization) so the barrel
        # transparently reflects the current submodule attribute — a test that
        # patches `<this package>.<submodule>.<name>` must not be shadowed by a
        # value cached from an earlier patch. importlib already caches the module
        # import, so the cost is a dict lookup.
        return getattr(importlib.import_module(module_path), attr_name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return list(__all__)


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
