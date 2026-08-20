"""
Audit Backend Prometheus metrics.

Exposes whether the audit subsystem actually resolves to a real backend.
``audit_backend_wired`` is 0 exactly when the master switch is on while the
resolved default provider is the no-op adapter — the one condition that
silently voids the audit trail: records are written, accepted, and reach
nothing.

A boot WARNING alone is the weakest channel for that condition, since the
operators this feature is sold to alert on series rather than on log greps.
The gauge is primed from ``init()`` so a deployment can alert on
``audit_backend_wired == 0`` without waiting for the first audited event.

Clones the ``get_or_create_gauge`` + ``_DummyMetric`` fallback pattern of
``metrics/audit_buffer_metrics.py``: ``.set()`` never raises when
prometheus_client is absent, preserving the fail-open guarantee of the
caller's except block.
"""

from __future__ import annotations

from typing import Any

from baldur.metrics._metric_protocol import GaugeMetric

__all__ = [
    "audit_backend_wired",
    "set_audit_backend_wired",
    "METRICS_AVAILABLE",
]

audit_backend_wired: GaugeMetric

try:
    from baldur.metrics.registry import get_or_create_gauge

    audit_backend_wired = get_or_create_gauge(
        "audit_backend_wired",
        "1 when the audit subsystem resolves to a real backend, "
        "0 when it is enabled but resolves to the no-op adapter",
        [],
    )

    METRICS_AVAILABLE = True

except ImportError:
    # prometheus_client unavailable — use a dummy metric. _DummyMetric
    # implements GaugeMetric in full (labels + set + inc), so .set() is a no-op
    # that never raises inside a fail-open except block.
    METRICS_AVAILABLE = False

    class _DummyMetric:
        """Dummy metric used when prometheus_client is unavailable."""

        def labels(self, *args: Any, **kwargs: Any) -> _DummyMetric:
            return self

        def set(self, value: float) -> None:
            pass

        def inc(self, amount: float = 1) -> None:
            pass

    audit_backend_wired = _DummyMetric()


def set_audit_backend_wired(wired: bool) -> None:
    """Publish whether the resolved audit backend is a real one.

    Args:
        wired: ``True`` when the resolved default provider delivers
            somewhere, ``False`` when audit is enabled but the resolved
            default is the no-op adapter.
    """
    audit_backend_wired.set(1 if wired else 0)
