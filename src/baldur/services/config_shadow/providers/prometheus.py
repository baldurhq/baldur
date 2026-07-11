"""
Prometheus TimeSeriesMetricsProvider.

The first real ``TimeSeriesMetricsProvider`` implementation: it renders each
Protocol query as PromQL (naming preset + per-field overrides), issues it via
the shared :class:`PrometheusQueryClient`, and parses/converts the result. It
holds no verdict logic and adds no fail-open layer of its own — a query failure
propagates as ``PrometheusQueryError`` and the consuming promotion gate already
fail-opens (skips) on any provider exception.

Query templates cover two metric-naming pipelines, selected by the
``metric_naming`` setting:

- ``baldur`` (default): the ``baldur_http_*`` RED metrics recorded zero-config
  by the framework adapters — the only naming guaranteed present.
- ``otel``: the OpenTelemetry HTTP-server semantic-convention histogram as
  translated by Prometheus-side OTLP ingestion.

The per-field override settings (metric names, status label, error regex) serve
third-party exporter naming without a preset.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import structlog

from baldur.adapters.metrics.prometheus_query_client import PrometheusQueryClient
from baldur.services.config_shadow.metrics_provider import set_metrics_provider
from baldur.settings.prometheus import PrometheusSettings, get_prometheus_settings

logger = structlog.get_logger()

_SECONDS_TO_MILLIS = 1000

__all__ = [
    "PrometheusTimeSeriesProvider",
    "setup_prometheus_metrics_provider",
]


# ==========================================================================
# Naming presets + resolution
# ==========================================================================
@dataclass(frozen=True)
class _PresetNames:
    requests_total: str
    duration_histogram: str  # base name; ``_bucket`` appended for quantiles
    status_code_label: str


# The otel preset derives request rate/count from the histogram's ``_count``
# series, so requests_total is that ``_count`` metric.
_PRESETS: dict[str, _PresetNames] = {
    "baldur": _PresetNames(
        requests_total="baldur_http_requests_total",
        duration_histogram="baldur_http_request_duration_seconds",
        status_code_label="status_code",
    ),
    "otel": _PresetNames(
        requests_total="http_server_request_duration_seconds_count",
        duration_histogram="http_server_request_duration_seconds",
        status_code_label="http_response_status_code",
    ),
}


@dataclass(frozen=True)
class _MetricNames:
    requests_total: str
    duration_histogram: str
    status_code_label: str
    error_status_regex: str


def _resolve_metric_names(settings: PrometheusSettings) -> _MetricNames:
    """Preset defaults with per-field settings overrides layered on top."""
    preset = _PRESETS[settings.metric_naming]
    return _MetricNames(
        requests_total=settings.requests_total_metric or preset.requests_total,
        duration_histogram=(
            settings.duration_histogram_metric or preset.duration_histogram
        ),
        status_code_label=settings.status_code_label or preset.status_code_label,
        error_status_regex=settings.error_status_regex or "5..",
    )


# ==========================================================================
# Selector rendering (pure functions — the golden-text test surface)
# ==========================================================================
def _escape_label_value(value: str) -> str:
    """Escape a label value per PromQL Go-string rules.

    Operator-set config, not untrusted input — but an unescaped quote or
    newline silently breaks the query into an empty result, which reads as a
    blocked promote that is hard to debug.
    """
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _build_matchers(
    labels: dict[str, str] | None,
    service_name: str,
    service_label: str,
    extra_selectors: dict[str, str],
) -> list[str]:
    """Merge static + service + dynamic matchers into sorted ``k="v"`` terms.

    ``service_name`` is injected only when ``service_label`` is set — the canary
    evaluator passes a config-type identifier as ``service_name``, not a
    Prometheus job/service label, so injecting it unconditionally would empty
    every result.
    """
    merged: dict[str, str] = {}
    merged.update(extra_selectors)
    if service_label:
        merged[service_label] = service_name
    if labels:
        merged.update(labels)
    return [
        f'{key}="{_escape_label_value(value)}"' for key, value in sorted(merged.items())
    ]


def _selector(matchers: list[str]) -> str:
    return "{" + ",".join(matchers) + "}"


def _error_selector(matchers: list[str], names: _MetricNames) -> str:
    status = (
        f'{names.status_code_label}=~"{_escape_label_value(names.error_status_regex)}"'
    )
    return "{" + ",".join([status, *matchers]) + "}"


# ==========================================================================
# PromQL query templates (pure functions)
# ==========================================================================
def _render_error_rate_aggregated(
    names: _MetricNames, matchers: list[str], window_seconds: int
) -> str:
    req = names.requests_total
    return (
        f"sum(rate({req}{_error_selector(matchers, names)}[{window_seconds}s]))"
        f" / sum(rate({req}{_selector(matchers)}[{window_seconds}s]))"
    )


def _render_request_count(
    names: _MetricNames, matchers: list[str], window_seconds: int
) -> str:
    return (
        f"sum(increase({names.requests_total}{_selector(matchers)}[{window_seconds}s]))"
    )


def _render_latency(
    names: _MetricNames, matchers: list[str], window_seconds: int, percentile: float
) -> str:
    bucket = f"{names.duration_histogram}_bucket"
    return (
        f"histogram_quantile({percentile}, "
        f"sum(rate({bucket}{_selector(matchers)}[{window_seconds}s])) by (le))"
        f" * {_SECONDS_TO_MILLIS}"
    )


def _render_error_rate_range(
    names: _MetricNames, matchers: list[str], step_seconds: int
) -> str:
    req = names.requests_total
    return (
        f"sum(rate({req}{_error_selector(matchers, names)}[{step_seconds}s]))"
        f" / sum(rate({req}{_selector(matchers)}[{step_seconds}s]))"
    )


def _render_request_rate_range(
    names: _MetricNames, matchers: list[str], step_seconds: int
) -> str:
    return f"sum(rate({names.requests_total}{_selector(matchers)}[{step_seconds}s]))"


# ==========================================================================
# Result parsing
# ==========================================================================
def _parse_float(raw: Any, default: float = 0.0) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    if math.isnan(value) or math.isinf(value):
        return default
    return value


def _scalar(result: list[dict[str, Any]], default: float = 0.0) -> float:
    """Extract the single scalar from an instant-vector result."""
    if not result:
        return default
    value = result[0].get("value")
    if not value or len(value) < 2:
        return default
    return _parse_float(value[1], default)


def _series(result: list[dict[str, Any]]) -> list[tuple[datetime, float]]:
    """Convert a range-matrix result into (timestamp, value) tuples."""
    if not result:
        return []
    out: list[tuple[datetime, float]] = []
    for point in result[0].get("values", []):
        if len(point) < 2:
            continue
        timestamp = datetime.fromtimestamp(float(point[0]), tz=UTC)
        out.append((timestamp, _parse_float(point[1])))
    return out


def _window_seconds(start: datetime, end: datetime) -> int:
    return max(1, int(round((end - start).total_seconds())))


# ==========================================================================
# Provider
# ==========================================================================
class PrometheusTimeSeriesProvider:
    """Remote-Prometheus TimeSeriesMetricsProvider (thin PromQL composition).

    Each Protocol method renders a PromQL template, issues it via the shared
    query client, and parses the result. Latency percentiles are computed
    server-side via ``histogram_quantile`` (percentiles cannot be averaged) and
    converted seconds → milliseconds. Empty results and PromQL NaN convert to
    ``0.0`` — safe because the evaluator's minimum-traffic floor already turns
    no-data into a blocked promote, never a pass.

    The two range methods (``query_error_rate``, ``query_request_rate``) have no
    verdict consumer today; they serve trend/dashboard use and Protocol
    completeness.
    """

    def __init__(
        self,
        *,
        client: PrometheusQueryClient | None = None,
        settings: PrometheusSettings | None = None,
    ) -> None:
        self._settings = settings or get_prometheus_settings()
        self._client = client or PrometheusQueryClient(settings=self._settings)
        self._names = _resolve_metric_names(self._settings)

    def _matchers(self, service_name: str, labels: dict[str, str] | None) -> list[str]:
        return _build_matchers(
            labels,
            service_name,
            self._settings.service_label,
            self._settings.extra_label_selectors,
        )

    # --- Scalar aggregate methods (evaluator verdicts) ---

    def query_error_rate_aggregated(
        self,
        service_name: str,
        start: datetime,
        end: datetime,
        labels: dict[str, str] | None = None,
    ) -> float:
        promql = _render_error_rate_aggregated(
            self._names,
            self._matchers(service_name, labels),
            _window_seconds(start, end),
        )
        return _scalar(self._client.query(promql, at=end))

    def query_request_count(
        self,
        service_name: str,
        start: datetime,
        end: datetime,
        labels: dict[str, str] | None = None,
    ) -> int:
        promql = _render_request_count(
            self._names,
            self._matchers(service_name, labels),
            _window_seconds(start, end),
        )
        return int(round(_scalar(self._client.query(promql, at=end))))

    def query_latency_aggregated(
        self,
        service_name: str,
        start: datetime,
        end: datetime,
        percentile: float = 0.99,
        labels: dict[str, str] | None = None,
    ) -> float:
        promql = _render_latency(
            self._names,
            self._matchers(service_name, labels),
            _window_seconds(start, end),
            percentile,
        )
        return _scalar(self._client.query(promql, at=end))

    # --- Time-series methods (trend analysis / UI dashboards) ---

    def query_error_rate(
        self,
        service_name: str,
        start: datetime,
        end: datetime,
        step_seconds: int = 60,
        labels: dict[str, str] | None = None,
    ) -> list[tuple[datetime, float]]:
        promql = _render_error_rate_range(
            self._names, self._matchers(service_name, labels), step_seconds
        )
        return _series(self._client.query_range(promql, start, end, step_seconds))

    def query_request_rate(
        self,
        service_name: str,
        start: datetime,
        end: datetime,
        step_seconds: int = 60,
        labels: dict[str, str] | None = None,
    ) -> list[tuple[datetime, float]]:
        promql = _render_request_rate_range(
            self._names, self._matchers(service_name, labels), step_seconds
        )
        return _series(self._client.query_range(promql, start, end, step_seconds))


# ==========================================================================
# Registration (DI seam call site — ADR framework-init pattern)
# ==========================================================================
def setup_prometheus_metrics_provider(
    settings: PrometheusSettings | None = None,
) -> PrometheusTimeSeriesProvider:
    """Build the remote-Prometheus provider and register it as the DI seam.

    When ``extra_label_selectors`` is empty, logs a scoping warning: without a
    static selector the queries aggregate the whole Prometheus, so a
    multi-service cluster would blend cross-service traffic into the verdict.
    """
    settings = settings or get_prometheus_settings()
    provider = PrometheusTimeSeriesProvider(settings=settings)
    set_metrics_provider(provider)
    if not settings.extra_label_selectors:
        logger.warning(
            "prometheus_provider.scoping_unset",
            reason=(
                "extra_label_selectors is empty: queries aggregate the whole "
                "Prometheus; multi-service clusters must set "
                "BALDUR_PROMETHEUS_EXTRA_LABEL_SELECTORS to scope the verdict"
            ),
        )
    return provider
