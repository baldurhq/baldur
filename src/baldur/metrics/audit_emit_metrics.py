"""
Audit Emission Prometheus metrics.

Tracks best-effort audit emissions that were dropped because the configured
audit adapter's ``log()`` raised and there is no WAL backstop to recover the
entry.

Only the three best-effort, no-backstop emitters increment this counter:
``unified_notification``, ``celery_notifying_task``, and ``forensic_recorder``.
The WAL-first emitters (DLQ audit helpers, WAL meta-events) are intentionally
excluded — there a ``log()`` failure is not a drop because the entry is already
durable in the WAL and the sync worker backfills the adapter, so a
``*_dropped_total`` increment would be semantically wrong.

Clones the ``get_or_create_counter`` + ``_DummyMetric`` fallback pattern of
``metrics/audit_buffer_metrics.py``: ``.inc()`` never raises when
prometheus_client is absent, preserving the fail-open guarantee of the
callers' except blocks.
"""

from __future__ import annotations

from typing import Any

from baldur.metrics._metric_protocol import CounterMetric

__all__ = [
    "audit_emit_dropped_total",
    "record_audit_emit_dropped",
    "METRICS_AVAILABLE",
]

audit_emit_dropped_total: CounterMetric

try:
    from baldur.metrics.registry import get_or_create_counter

    audit_emit_dropped_total = get_or_create_counter(
        "audit_emit_dropped_total",
        "Best-effort audit emissions dropped when adapter.log() raised "
        "(no WAL backstop)",
        ["site"],
    )

    METRICS_AVAILABLE = True

except ImportError:
    # prometheus_client unavailable — use a dummy metric. _DummyMetric is a
    # superset of CounterMetric (labels + inc), so .inc() is a no-op that
    # never raises inside a fail-open except block.
    METRICS_AVAILABLE = False

    class _DummyMetric:
        """Dummy metric used when prometheus_client is unavailable."""

        def labels(self, *args: Any, **kwargs: Any) -> _DummyMetric:
            return self

        def inc(self, amount: float = 1) -> None:
            pass

    audit_emit_dropped_total = _DummyMetric()


def record_audit_emit_dropped(site: str) -> None:
    """Record a dropped best-effort audit emission for ``site``.

    Args:
        site: The emitting component label (e.g. ``"unified_notification"``,
            ``"celery_notifying_task"``, ``"forensic_recorder"``).
    """
    audit_emit_dropped_total.labels(site=site).inc()
