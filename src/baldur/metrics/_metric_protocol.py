"""
Structural Protocols for Prometheus-style metric objects.

These Protocols give mypy a stable contract that BOTH real
prometheus_client Counter/Gauge classes AND in-process fallback dummies
satisfy, so a single variable annotation (`audit_buffer_total: CounterMetric`)
accepts either branch of the customary `try: ... import prometheus_client
... except ImportError: ...` pattern without type-ignore noise.

Two Protocols instead of one: prometheus_client's `Counter` exposes
`labels()` + `inc()` but *not* `set()` (counters are monotonic). `Gauge`
exposes all three. A single combined Protocol would force every counter
to satisfy `set` (which it does not), so we split by metric kind. In-
process dummies declare the union and satisfy both Protocols.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

__all__ = ["CounterMetric", "GaugeMetric", "HistogramMetric"]


@runtime_checkable
class CounterMetric(Protocol):
    """Counter-style metric: labels + monotonic increment."""

    def labels(self, *args: Any, **kwargs: Any) -> CounterMetric: ...

    def inc(self, amount: float = 1) -> None: ...


@runtime_checkable
class GaugeMetric(Protocol):
    """Gauge-style metric: labels + settable value + increment/decrement."""

    def labels(self, *args: Any, **kwargs: Any) -> GaugeMetric: ...

    def set(self, value: float) -> None: ...

    def inc(self, amount: float = 1) -> None: ...


@runtime_checkable
class HistogramMetric(Protocol):
    """Histogram-style metric: labels + observe (bucket recording)."""

    def labels(self, *args: Any, **kwargs: Any) -> HistogramMetric: ...

    def observe(self, value: float) -> None: ...
