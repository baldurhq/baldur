"""
Time Series Metrics Provider.

Protocol and Mock implementation for querying historical raw data during
simulation. The existing MetricsProvider (core/auto_rollback_guard.py)
returns only current values; Config Shadow needs time-series over a past
time range, so it defines its own provider contract here.
"""

from __future__ import annotations

import threading
from datetime import datetime
from typing import Protocol, runtime_checkable

__all__ = [
    "MockTimeSeriesProvider",
    "TimeSeriesMetricsProvider",
    "get_metrics_provider",
    "is_metrics_provider_registered",
    "reset_metrics_provider",
    "set_metrics_provider",
]


@runtime_checkable
class TimeSeriesMetricsProvider(Protocol):
    """Time-series metrics provider.

    Serves trend data (time-series lists) and verdict-grade aggregates
    (scalars) as separate method families.

    Implementations:
    - MockTimeSeriesProvider: tests and development
    - PrometheusTimeSeriesProvider: remote PromQL range/instant queries against
      Prometheus or a PromQL-compatible backend (auto-registered from
      ``BALDUR_PROMETHEUS_URL`` at startup, or via ``set_metrics_provider()``)
    - Other remote range-query providers registered via ``set_metrics_provider()``
    """

    # --- Time-series methods (trend analysis / UI dashboards) ---

    def query_error_rate(
        self,
        service_name: str,
        start: datetime,
        end: datetime,
        step_seconds: int = 60,
        labels: dict[str, str] | None = None,
    ) -> list[tuple[datetime, float]]:
        """Return the error-rate time series for a time range.

        Args:
            service_name: Target service.
            start: Query start time (UTC).
            end: Query end time (UTC).
            step_seconds: Series resolution (default 60 seconds).
            labels: Compound K8s labels (e.g. {"track": "canary", "namespace": "prod"}).

        Returns:
            List of (timestamp, error_rate) tuples. error_rate is 0.0 ~ 1.0.
        """
        ...

    def query_request_rate(
        self,
        service_name: str,
        start: datetime,
        end: datetime,
        step_seconds: int = 60,
        labels: dict[str, str] | None = None,
    ) -> list[tuple[datetime, float]]:
        """Return the request-rate (RPS) time series for a time range."""
        ...

    # --- Scalar aggregate methods (evaluator verdicts) ---

    def query_error_rate_aggregated(
        self,
        service_name: str,
        start: datetime,
        end: datetime,
        labels: dict[str, str] | None = None,
    ) -> float:
        """Weighted error-rate scalar over the whole window.

        Internally runs a sum(rate(errors)) / sum(rate(requests)) style
        PromQL/Datadog query.

        Returns:
            Weighted error rate (0.0 ~ 1.0).
        """
        ...

    def query_request_count(
        self,
        service_name: str,
        start: datetime,
        end: datetime,
        labels: dict[str, str] | None = None,
    ) -> int:
        """Total request count over the whole window."""
        ...

    def query_latency_aggregated(
        self,
        service_name: str,
        start: datetime,
        end: datetime,
        percentile: float = 0.99,
        labels: dict[str, str] | None = None,
    ) -> float:
        """Latency percentile scalar over the whole window (milliseconds).

        Internally runs a histogram_quantile(percentile, ...) style PromQL
        query. Percentiles cannot be averaged, so the provider must compute
        them in a single pass.

        Args:
            percentile: 0.95 (P95) or 0.99 (P99).

        Returns:
            Latency at the given percentile (milliseconds).
        """
        ...


class MockTimeSeriesProvider:
    """Time-series metrics provider for tests.

    Inject arbitrary time-series data to verify simulator logic. In
    production, replace with a real source registered via
    ``set_metrics_provider()``.

    Key layout:
    - labels=None: "{service}:{metric}" (backward compatible)
    - with labels: "{service}:{metric}:{k}={v},..." (label-aware)
    """

    def __init__(self, data: dict[str, list[tuple[datetime, float]]] | None = None):
        self._data = data or {}
        self._scalars: dict[str, float] = {}

    @staticmethod
    def _label_suffix(labels: dict[str, str] | None) -> str:
        if not labels:
            return ""
        return ":" + ",".join(f"{k}={v}" for k, v in sorted(labels.items()))

    def _scalar_key(
        self,
        service_name: str,
        metric: str,
        labels: dict[str, str] | None = None,
    ) -> str:
        return f"{service_name}:{metric}{self._label_suffix(labels)}"

    def _resolve_scalar(
        self,
        service_name: str,
        metric: str,
        labels: dict[str, str] | None,
        default: float = 0.0,
    ) -> float:
        labeled_key = self._scalar_key(service_name, metric, labels)
        if labeled_key in self._scalars:
            return self._scalars[labeled_key]
        return self._scalars.get(f"{service_name}:{metric}", default)

    # --- Time-series methods (labels parameter, default None) ---

    def query_error_rate(
        self,
        service_name: str,
        start: datetime,
        end: datetime,
        step_seconds: int = 60,
        labels: dict[str, str] | None = None,
    ) -> list[tuple[datetime, float]]:
        key = f"{service_name}:error_rate{self._label_suffix(labels)}"
        if key not in self._data:
            key = f"{service_name}:error_rate"
        return [(ts, val) for ts, val in self._data.get(key, []) if start <= ts < end]

    def query_request_rate(
        self,
        service_name: str,
        start: datetime,
        end: datetime,
        step_seconds: int = 60,
        labels: dict[str, str] | None = None,
    ) -> list[tuple[datetime, float]]:
        key = f"{service_name}:request_rate{self._label_suffix(labels)}"
        if key not in self._data:
            key = f"{service_name}:request_rate"
        return [(ts, val) for ts, val in self._data.get(key, []) if start <= ts < end]

    # --- Scalar aggregate methods ---

    def query_error_rate_aggregated(
        self,
        service_name: str,
        start: datetime,
        end: datetime,
        labels: dict[str, str] | None = None,
    ) -> float:
        return self._resolve_scalar(service_name, "error_rate_agg", labels)

    def query_request_count(
        self,
        service_name: str,
        start: datetime,
        end: datetime,
        labels: dict[str, str] | None = None,
    ) -> int:
        return int(self._resolve_scalar(service_name, "request_count", labels))

    def query_latency_aggregated(
        self,
        service_name: str,
        start: datetime,
        end: datetime,
        percentile: float = 0.99,
        labels: dict[str, str] | None = None,
    ) -> float:
        return self._resolve_scalar(
            service_name,
            f"latency_p{int(percentile * 100)}",
            labels,
        )


_metrics_provider: TimeSeriesMetricsProvider | None = None
_metrics_provider_registered = False
_metrics_provider_lock = threading.Lock()


def get_metrics_provider() -> TimeSeriesMetricsProvider:
    """Return the TimeSeriesMetricsProvider singleton.

    Falls back to a lazily-created MockTimeSeriesProvider when no provider
    has been registered. The lazy Mock default does NOT count as registered —
    consumers that must not evaluate on synthetic data gate on
    ``is_metrics_provider_registered()`` first.
    """
    global _metrics_provider
    if _metrics_provider is None:
        with _metrics_provider_lock:
            if _metrics_provider is None:
                _metrics_provider = MockTimeSeriesProvider()
    return _metrics_provider


def set_metrics_provider(provider: TimeSeriesMetricsProvider) -> None:
    """Register a time-series metrics provider (DI seam).

    Registration is explicit: any provider passed here counts as registered,
    including a deliberately registered MockTimeSeriesProvider (tests,
    staging).
    """
    global _metrics_provider, _metrics_provider_registered
    with _metrics_provider_lock:
        _metrics_provider = provider
        _metrics_provider_registered = True


def reset_metrics_provider() -> None:
    """Reset the singleton and clear the registration mark (for tests)."""
    global _metrics_provider, _metrics_provider_registered
    with _metrics_provider_lock:
        _metrics_provider = None
        _metrics_provider_registered = False


def is_metrics_provider_registered() -> bool:
    """Whether a provider was explicitly registered via set_metrics_provider().

    The lazy Mock default created by ``get_metrics_provider()`` does not
    count as registered.
    """
    return _metrics_provider_registered
