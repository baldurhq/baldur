"""
Audit Buffer Prometheus metrics.

Exposes the state and backpressure level of the Redis audit buffer to
Prometheus.
"""

from __future__ import annotations

from typing import Any

from baldur.metrics._metric_protocol import CounterMetric, GaugeMetric

audit_buffer_size: GaugeMetric
audit_buffer_backpressure: GaugeMetric
audit_buffer_dropped_total: CounterMetric
audit_buffer_batch_writes_total: CounterMetric
audit_buffer_batch_errors_total: CounterMetric
audit_buffer_flush_total: CounterMetric
audit_buffer_orphan_recovery_total: CounterMetric
audit_buffer_safety_ltrim_total: CounterMetric
audit_buffer_fallback_size: GaugeMetric

try:
    from baldur.metrics.registry import get_or_create_counter, get_or_create_gauge

    # Current buffer size (per domain)
    audit_buffer_size = get_or_create_gauge(
        "audit_buffer_size",
        "Current size of audit buffer by domain",
        ["domain"],
    )

    # Backpressure level (0.0 - 1.0, per domain)
    audit_buffer_backpressure = get_or_create_gauge(
        "audit_buffer_backpressure",
        "Backpressure level of audit buffer (0.0-1.0)",
        ["domain"],
    )

    # Dropped entry count (caused by safety LTRIM)
    audit_buffer_dropped_total = get_or_create_counter(
        "audit_buffer_dropped_total",
        "Total dropped audit entries due to buffer overflow",
        ["domain"],
    )

    # Successful batch writes
    audit_buffer_batch_writes_total = get_or_create_counter(
        "audit_buffer_batch_writes_total",
        "Total successful batch writes to audit buffer",
        ["domain"],
    )

    # Failed batch writes
    audit_buffer_batch_errors_total = get_or_create_counter(
        "audit_buffer_batch_errors_total",
        "Total failed batch writes to audit buffer",
        ["domain"],
    )

    # Successful flushes
    audit_buffer_flush_total = get_or_create_counter(
        "audit_buffer_flush_total",
        "Total flushed entries from audit buffer",
        ["domain"],
    )

    # Orphaned-queue recoveries
    audit_buffer_orphan_recovery_total = get_or_create_counter(
        "audit_buffer_orphan_recovery_total",
        "Total recovered entries from orphaned processing queues",
        ["domain"],
    )

    # Safety LTRIM occurrences
    audit_buffer_safety_ltrim_total = get_or_create_counter(
        "audit_buffer_safety_ltrim_total",
        "Total safety LTRIM operations performed",
        ["domain"],
    )

    # Fallback buffer size
    audit_buffer_fallback_size = get_or_create_gauge(
        "audit_buffer_fallback_size",
        "Current size of in-memory fallback buffer",
        [],
    )

    METRICS_AVAILABLE = True

except ImportError:
    # Build dummy metrics when prometheus_client is absent. _DummyMetric is a
    # superset of GaugeMetric (labels + set + inc), so it is assignable to both
    # GaugeMetric and CounterMetric.
    METRICS_AVAILABLE = False

    class _DummyMetric:
        """Dummy metric used when prometheus_client is absent."""

        def labels(self, *args: Any, **kwargs: Any) -> _DummyMetric:
            return self

        def set(self, value: float) -> None:
            pass

        def inc(self, amount: float = 1) -> None:
            pass

    audit_buffer_size = _DummyMetric()
    audit_buffer_backpressure = _DummyMetric()
    audit_buffer_dropped_total = _DummyMetric()
    audit_buffer_batch_writes_total = _DummyMetric()
    audit_buffer_batch_errors_total = _DummyMetric()
    audit_buffer_flush_total = _DummyMetric()
    audit_buffer_orphan_recovery_total = _DummyMetric()
    audit_buffer_safety_ltrim_total = _DummyMetric()
    audit_buffer_fallback_size = _DummyMetric()


def update_buffer_metrics(
    domain: str,
    size: int,
    max_size: int,
) -> None:
    """
    Helper that updates the buffer metrics.

    Args:
        domain: Domain name
        size: Current buffer size
        max_size: Maximum buffer size
    """
    audit_buffer_size.labels(domain=domain).set(size)

    backpressure = min(1.0, size / max(1, max_size))
    audit_buffer_backpressure.labels(domain=domain).set(backpressure)


def record_batch_write(domain: str, success: bool) -> None:
    """Record the outcome of a batch write."""
    if success:
        audit_buffer_batch_writes_total.labels(domain=domain).inc()
    else:
        audit_buffer_batch_errors_total.labels(domain=domain).inc()


def record_flush(domain: str, count: int) -> None:
    """Record the outcome of a flush."""
    audit_buffer_flush_total.labels(domain=domain).inc(count)


def record_orphan_recovery(domain: str, count: int) -> None:
    """Record an orphaned-queue recovery."""
    audit_buffer_orphan_recovery_total.labels(domain=domain).inc(count)


def record_safety_ltrim(domain: str, dropped_count: int) -> None:
    """Record a safety LTRIM."""
    audit_buffer_safety_ltrim_total.labels(domain=domain).inc()
    audit_buffer_dropped_total.labels(domain=domain).inc(dropped_count)
