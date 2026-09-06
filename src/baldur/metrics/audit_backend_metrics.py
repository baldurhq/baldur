"""
Audit Backend Prometheus metrics.

Exposes whether the audit subsystem actually resolves to a real backend.
``audit_backend_wired`` is 0 exactly when the master switch is on while the
resolved default provider is the no-op adapter — the one condition that
silently voids the audit trail: records are written, accepted, and reach
nothing.

``audit_distributed_chain_degraded`` answers a different question on a
different axis: records do land and the backend is wired, but the chain
sequencing them is not the cross-host one the deployment asked for. Only a
process that wanted a distributed chain publishes it, so an absent series
means "nobody asked", never "everything is fine".

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
    "audit_distributed_chain_degraded",
    "set_audit_backend_wired",
    "set_audit_distributed_chain_degraded",
    "METRICS_AVAILABLE",
]

audit_backend_wired: GaugeMetric
audit_distributed_chain_degraded: GaugeMetric

try:
    from baldur.metrics.registry import get_or_create_gauge

    audit_backend_wired = get_or_create_gauge(
        "audit_backend_wired",
        "1 when the audit subsystem resolves to a real backend, "
        "0 when it is enabled but resolves to the no-op adapter",
        [],
    )

    audit_distributed_chain_degraded = get_or_create_gauge(
        "audit_distributed_chain_degraded",
        "1 when a distributed audit hash chain was asked for but its Redis "
        "did not answer the admission probe, 0 when it did; absent when no "
        "distributed chain was asked for",
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
    audit_distributed_chain_degraded = _DummyMetric()


def set_audit_backend_wired(wired: bool) -> None:
    """Publish whether the resolved audit backend is a real one.

    Args:
        wired: ``True`` when the resolved default provider delivers
            somewhere, ``False`` when audit is enabled but the resolved
            default is the no-op adapter.
    """
    audit_backend_wired.set(1 if wired else 0)


def set_audit_distributed_chain_degraded(degraded: bool) -> None:
    """Publish whether the distributed chain this process asked for answered.

    Called on both outcomes of the admission probe so a healthy process
    publishes ``0`` rather than leaving the series absent — absence is
    reserved for "this process never asked for a distributed chain", which is
    a third state an alert has to be able to tell apart.

    Args:
        degraded: ``True`` when the probe failed and the chain is writing
            through its labelled local fallback, ``False`` when Redis
            answered and the chain is genuinely distributed.
    """
    audit_distributed_chain_degraded.set(1 if degraded else 0)
