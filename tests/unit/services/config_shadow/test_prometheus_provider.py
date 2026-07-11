"""
Unit tests for PrometheusTimeSeriesProvider — remote-Prometheus provider.

Test classification (UNIT_TEST_GUIDELINES §0):
- Contract: golden PromQL text per naming preset × method (the exact query
  strings the provider issues, incl. server-side histogram_quantile and PromQL
  value escaping) are the design spec, hardcoded.
- Behavior: metric-name resolution, result parsing, provider method returns,
  registration, and an end-to-end verdict flip through LiveCanaryEvaluator —
  computed against source behavior.

Target: baldur.services.config_shadow.providers.prometheus
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import Mock

import pytest
import structlog

from baldur.adapters.metrics.prometheus_query_client import (
    PrometheusQueryClient,
    PrometheusQueryError,
)
from baldur.services.config_shadow.evaluators.live_canary import LiveCanaryEvaluator
from baldur.services.config_shadow.metrics_provider import (
    TimeSeriesMetricsProvider,
    get_metrics_provider,
    is_metrics_provider_registered,
    reset_metrics_provider,
)
from baldur.services.config_shadow.models import EvaluationContext
from baldur.services.config_shadow.providers.prometheus import (
    PrometheusTimeSeriesProvider,
    _build_matchers,
    _error_selector,
    _escape_label_value,
    _parse_float,
    _resolve_metric_names,
    _scalar,
    _series,
    setup_prometheus_metrics_provider,
)
from baldur.settings.prometheus import PrometheusSettings

# A fixed 300s window so rendered [Ws] windows are deterministic.
_START = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
_END = datetime(2026, 1, 1, 0, 5, tzinfo=UTC)
_STEP = 60
_CANARY = {"track": "canary"}


class _RecordingClient:
    """Query client double that records PromQL and returns canned results."""

    def __init__(
        self, scalar: float = 0.0, series_values: list[float] | None = None
    ) -> None:
        self._scalar = scalar
        self._series_values = series_values or []
        self.queries: list[str] = []
        self.range_queries: list[str] = []

    def query(self, promql: str, at: datetime | None = None) -> list[dict]:
        self.queries.append(promql)
        return [{"metric": {}, "value": [1_700_000_000, str(self._scalar)]}]

    def query_range(
        self, promql: str, start: datetime, end: datetime, step_seconds: int
    ) -> list[dict]:
        self.range_queries.append(promql)
        return [
            {
                "metric": {},
                "values": [
                    [1_700_000_000 + i, str(v)]
                    for i, v in enumerate(self._series_values)
                ],
            }
        ]


def _provider(preset: str = "baldur", **settings_kwargs) -> tuple:
    settings = PrometheusSettings(
        metric_naming=preset, url="http://p:9090", **settings_kwargs
    )
    client = _RecordingClient()
    provider = PrometheusTimeSeriesProvider(client=client, settings=settings)
    return provider, client


# ==========================================================================
# Golden query text — SC3 (server-side quantile) + SC4 (naming presets)
# ==========================================================================
_GOLDEN_BALDUR = {
    "error_rate_aggregated": (
        'sum(rate(baldur_http_requests_total{status_code=~"5..",track="canary"}[300s]))'
        ' / sum(rate(baldur_http_requests_total{track="canary"}[300s]))'
    ),
    "request_count": (
        'sum(increase(baldur_http_requests_total{track="canary"}[300s]))'
    ),
    "latency_p99": (
        "histogram_quantile(0.99, "
        'sum(rate(baldur_http_request_duration_seconds_bucket{track="canary"}[300s]))'
        " by (le)) * 1000"
    ),
    "latency_p95": (
        "histogram_quantile(0.95, "
        'sum(rate(baldur_http_request_duration_seconds_bucket{track="canary"}[300s]))'
        " by (le)) * 1000"
    ),
    "error_rate_range": (
        'sum(rate(baldur_http_requests_total{status_code=~"5..",track="canary"}[60s]))'
        ' / sum(rate(baldur_http_requests_total{track="canary"}[60s]))'
    ),
    "request_rate_range": (
        'sum(rate(baldur_http_requests_total{track="canary"}[60s]))'
    ),
}

_GOLDEN_OTEL = {
    "error_rate_aggregated": (
        "sum(rate(http_server_request_duration_seconds_count"
        '{http_response_status_code=~"5..",track="canary"}[300s]))'
        " / sum(rate(http_server_request_duration_seconds_count"
        '{track="canary"}[300s]))'
    ),
    "request_count": (
        "sum(increase(http_server_request_duration_seconds_count"
        '{track="canary"}[300s]))'
    ),
    "latency_p99": (
        "histogram_quantile(0.99, "
        "sum(rate(http_server_request_duration_seconds_bucket"
        '{track="canary"}[300s])) by (le)) * 1000'
    ),
    "latency_p95": (
        "histogram_quantile(0.95, "
        "sum(rate(http_server_request_duration_seconds_bucket"
        '{track="canary"}[300s])) by (le)) * 1000'
    ),
    "error_rate_range": (
        "sum(rate(http_server_request_duration_seconds_count"
        '{http_response_status_code=~"5..",track="canary"}[60s]))'
        " / sum(rate(http_server_request_duration_seconds_count"
        '{track="canary"}[60s]))'
    ),
    "request_rate_range": (
        'sum(rate(http_server_request_duration_seconds_count{track="canary"}[60s]))'
    ),
}


def _capture_query(provider: PrometheusTimeSeriesProvider, client, method: str) -> str:
    if method == "error_rate_aggregated":
        provider.query_error_rate_aggregated("svc", _START, _END, labels=_CANARY)
        return client.queries[-1]
    if method == "request_count":
        provider.query_request_count("svc", _START, _END, labels=_CANARY)
        return client.queries[-1]
    if method == "latency_p99":
        provider.query_latency_aggregated(
            "svc", _START, _END, percentile=0.99, labels=_CANARY
        )
        return client.queries[-1]
    if method == "latency_p95":
        provider.query_latency_aggregated(
            "svc", _START, _END, percentile=0.95, labels=_CANARY
        )
        return client.queries[-1]
    if method == "error_rate_range":
        provider.query_error_rate(
            "svc", _START, _END, step_seconds=_STEP, labels=_CANARY
        )
        return client.range_queries[-1]
    if method == "request_rate_range":
        provider.query_request_rate(
            "svc", _START, _END, step_seconds=_STEP, labels=_CANARY
        )
        return client.range_queries[-1]
    raise AssertionError(method)


class TestPrometheusQueryTextContract:
    """Exact rendered PromQL per preset × method (golden text)."""

    @pytest.mark.parametrize("method", sorted(_GOLDEN_BALDUR))
    def test_baldur_preset_query_text(self, method):
        provider, client = _provider("baldur")
        assert _capture_query(provider, client, method) == _GOLDEN_BALDUR[method]

    @pytest.mark.parametrize("method", sorted(_GOLDEN_OTEL))
    def test_otel_preset_query_text(self, method):
        provider, client = _provider("otel")
        assert _capture_query(provider, client, method) == _GOLDEN_OTEL[method]

    def test_latency_uses_histogram_quantile_not_average(self):
        """Percentiles are computed server-side via histogram_quantile (SC3)."""
        provider, client = _provider("baldur")
        provider.query_latency_aggregated("svc", _START, _END, percentile=0.95)
        query = client.queries[-1]
        assert query.startswith("histogram_quantile(0.95,")
        assert "avg" not in query

    def test_quote_in_label_value_is_escaped_in_query(self):
        """An injected label value with a quote is PromQL-escaped (D3)."""
        provider, client = _provider("baldur")
        provider.query_request_count("svc", _START, _END, labels={"track": 'a"b'})
        assert 'track="a\\"b"' in client.queries[-1]


class TestMetricNamingBehavior:
    """_resolve_metric_names: preset defaults + per-field override precedence."""

    @pytest.mark.parametrize(
        ("preset", "total", "histogram", "status"),
        [
            (
                "baldur",
                "baldur_http_requests_total",
                "baldur_http_request_duration_seconds",
                "status_code",
            ),
            (
                "otel",
                "http_server_request_duration_seconds_count",
                "http_server_request_duration_seconds",
                "http_response_status_code",
            ),
        ],
    )
    def test_preset_default_metric_names(self, preset, total, histogram, status):
        names = _resolve_metric_names(PrometheusSettings(metric_naming=preset))
        assert names.requests_total == total
        assert names.duration_histogram == histogram
        assert names.status_code_label == status

    def test_override_fields_take_precedence_over_preset(self):
        names = _resolve_metric_names(
            PrometheusSettings(
                metric_naming="baldur",
                requests_total_metric="custom_requests",
                duration_histogram_metric="custom_latency",
                status_code_label="code",
            )
        )
        assert names.requests_total == "custom_requests"
        assert names.duration_histogram == "custom_latency"
        assert names.status_code_label == "code"

    def test_error_status_regex_falls_back_to_5xx_when_empty(self):
        names = _resolve_metric_names(PrometheusSettings(error_status_regex=""))
        assert names.error_status_regex == "5.."


class TestEscapeAndMatcherBehavior:
    """Selector rendering — escaping, sorting, service-label gating, error class."""

    def test_escape_backslash(self):
        assert _escape_label_value("a\\b") == "a\\\\b"

    def test_escape_double_quote(self):
        assert _escape_label_value('a"b') == 'a\\"b'

    def test_escape_newline(self):
        assert _escape_label_value("a\nb") == "a\\nb"

    def test_matchers_sorted_by_key(self):
        matchers = _build_matchers({"zeta": "1", "alpha": "2"}, "svc", "", {})
        assert matchers == ['alpha="2"', 'zeta="1"']

    def test_service_name_injected_only_when_service_label_set(self):
        without = _build_matchers(None, "cfg-type", "", {})
        with_label = _build_matchers(None, "cfg-type", "job", {})
        assert without == []
        assert with_label == ['job="cfg-type"']

    def test_dynamic_labels_override_extra_selectors(self):
        matchers = _build_matchers(
            {"namespace": "override"}, "svc", "", {"namespace": "static"}
        )
        assert matchers == ['namespace="override"']

    def test_extra_selectors_included(self):
        matchers = _build_matchers(None, "svc", "", {"namespace": "prod"})
        assert matchers == ['namespace="prod"']

    def test_error_selector_prepends_status_matcher(self):
        names = _resolve_metric_names(PrometheusSettings(metric_naming="baldur"))
        selector = _error_selector(['track="canary"'], names)
        assert selector == '{status_code=~"5..",track="canary"}'


class TestParseBehavior:
    """Result parsing — scalar / series / float coercion."""

    def test_scalar_extracts_value(self):
        result = [{"metric": {}, "value": [1_700_000_000, "0.42"]}]
        assert _scalar(result) == pytest.approx(0.42)

    def test_scalar_empty_result_returns_default(self):
        assert _scalar([]) == 0.0

    def test_scalar_missing_value_returns_default(self):
        assert _scalar([{"metric": {}}]) == 0.0

    def test_scalar_nan_returns_default(self):
        result = [{"metric": {}, "value": [1_700_000_000, "NaN"]}]
        assert _scalar(result) == 0.0

    def test_scalar_inf_returns_default(self):
        result = [{"metric": {}, "value": [1_700_000_000, "+Inf"]}]
        assert _scalar(result) == 0.0

    def test_parse_float_non_numeric_returns_default(self):
        assert _parse_float("not-a-number") == 0.0

    def test_parse_float_none_returns_default(self):
        assert _parse_float(None) == 0.0

    def test_series_converts_points_to_tuples(self):
        result = [
            {
                "metric": {},
                "values": [[1_700_000_000, "1.0"], [1_700_000_060, "2.0"]],
            }
        ]
        series = _series(result)
        assert [v for _, v in series] == [1.0, 2.0]
        assert all(isinstance(ts, datetime) for ts, _ in series)

    def test_series_empty_result_returns_empty(self):
        assert _series([]) == []

    def test_series_skips_malformed_points(self):
        result = [{"metric": {}, "values": [[1_700_000_000], [1_700_000_060, "2.0"]]}]
        series = _series(result)
        assert [v for _, v in series] == [2.0]


class TestPrometheusProviderReturnBehavior:
    """Provider method return-value conversions."""

    def test_is_time_series_metrics_provider(self):
        provider, _ = _provider("baldur")
        assert isinstance(provider, TimeSeriesMetricsProvider)

    def test_error_rate_aggregated_returns_scalar(self):
        provider, client = _provider("baldur")
        client._scalar = 0.037
        assert provider.query_error_rate_aggregated("svc", _START, _END) == (
            pytest.approx(0.037)
        )

    def test_request_count_rounds_to_int(self):
        provider, client = _provider("baldur")
        client._scalar = 42.6
        result = provider.query_request_count("svc", _START, _END)
        assert result == 43
        assert isinstance(result, int)

    def test_latency_aggregated_returns_scalar_ms(self):
        provider, client = _provider("baldur")
        client._scalar = 123.0
        assert provider.query_latency_aggregated("svc", _START, _END) == (
            pytest.approx(123.0)
        )

    def test_error_rate_range_returns_series(self):
        provider, client = _provider("baldur")
        client._series_values = [0.01, 0.02, 0.03]
        series = provider.query_error_rate("svc", _START, _END, step_seconds=_STEP)
        assert [v for _, v in series] == [0.01, 0.02, 0.03]

    def test_request_rate_range_returns_series(self):
        provider, client = _provider("baldur")
        client._series_values = [100.0, 200.0]
        series = provider.query_request_rate("svc", _START, _END, step_seconds=_STEP)
        assert [v for _, v in series] == [100.0, 200.0]


class TestPrometheusProviderPropagationBehavior:
    """The provider adds no fail-open layer — errors propagate (SC5 OSS half)."""

    def test_query_error_propagates_from_provider_method(self):
        client = Mock()
        client.query.side_effect = PrometheusQueryError("boom", query="up")
        provider = PrometheusTimeSeriesProvider(
            client=client, settings=PrometheusSettings()
        )

        with pytest.raises(PrometheusQueryError):
            provider.query_error_rate_aggregated("svc", _START, _END)

    def test_query_error_propagates_through_live_evaluator(self):
        """A provider query error surfaces out of LiveCanaryEvaluator.evaluate —
        the OSS evaluator does not swallow it (the PRO gate fail-opens)."""
        client = Mock()
        client.query.side_effect = PrometheusQueryError("boom", query="up")
        provider = PrometheusTimeSeriesProvider(
            client=client, settings=PrometheusSettings()
        )
        evaluator = LiveCanaryEvaluator(metrics_provider=provider)
        context = EvaluationContext(
            baseline_config={},
            candidate_config={},
            service_name="svc",
            candidate_labels=_CANARY,
        )

        with pytest.raises(PrometheusQueryError):
            evaluator.evaluate(context)


class TestPrometheusRegistrationBehavior:
    """setup_prometheus_metrics_provider — DI registration + scoping warning."""

    @pytest.fixture(autouse=True)
    def _reset(self):
        reset_metrics_provider()
        yield
        reset_metrics_provider()

    def test_setup_registers_provider_via_di_seam(self):
        settings = PrometheusSettings(
            url="http://p:9090", extra_label_selectors={"namespace": "prod"}
        )
        provider = setup_prometheus_metrics_provider(settings=settings)

        assert is_metrics_provider_registered() is True
        assert get_metrics_provider() is provider

    def test_setup_warns_when_scoping_unset(self):
        settings = PrometheusSettings(url="http://p:9090")

        with structlog.testing.capture_logs() as logs:
            setup_prometheus_metrics_provider(settings=settings)

        events = [e["event"] for e in logs]
        assert "prometheus_provider.scoping_unset" in events

    def test_setup_no_warning_when_scoping_set(self):
        settings = PrometheusSettings(
            url="http://p:9090", extra_label_selectors={"namespace": "prod"}
        )

        with structlog.testing.capture_logs() as logs:
            setup_prometheus_metrics_provider(settings=settings)

        events = [e["event"] for e in logs]
        assert "prometheus_provider.scoping_unset" not in events


# ==========================================================================
# End-to-end verdict flip through LiveCanaryEvaluator — SC1
# ==========================================================================
class _VerdictResponse:
    """A Prometheus instant-vector HTTP response returning a single scalar."""

    def __init__(self, value: float) -> None:
        self.status_code = 200
        self._value = value

    def json(self) -> dict:
        return {
            "status": "success",
            "data": {
                "resultType": "vector",
                "result": [{"metric": {}, "value": [1_700_000_000, str(self._value)]}],
            },
        }


class _VerdictSession:
    """Fake HTTP session routing crafted scalars by PromQL shape and track."""

    def __init__(
        self,
        *,
        baseline_error: float,
        candidate_error: float,
        request_count: float,
        baseline_p95: float,
        candidate_p95: float,
        baseline_p99: float,
        candidate_p99: float,
    ) -> None:
        self._b_err = baseline_error
        self._c_err = candidate_error
        self._req = request_count
        self._b95 = baseline_p95
        self._c95 = candidate_p95
        self._b99 = baseline_p99
        self._c99 = candidate_p99

    def get(self, url, params=None, timeout=None, headers=None, verify=None):
        query = params["query"]
        is_candidate = 'track="canary"' in query
        if "histogram_quantile" in query:
            is_p95 = "0.95" in query
            if is_candidate:
                value = self._c95 if is_p95 else self._c99
            else:
                value = self._b95 if is_p95 else self._b99
        elif "increase(" in query:
            value = self._req
        else:
            value = self._c_err if is_candidate else self._b_err
        return _VerdictResponse(value)


def _wire_provider(session: _VerdictSession) -> PrometheusTimeSeriesProvider:
    client = PrometheusQueryClient(url="http://prom:9090", session=session)
    settings = PrometheusSettings(url="http://prom:9090")
    return PrometheusTimeSeriesProvider(client=client, settings=settings)


def _context() -> EvaluationContext:
    return EvaluationContext(
        baseline_config={},
        candidate_config={},
        service_name="svc",
        time_window_seconds=300,
        baseline_labels={"track": "stable"},
        candidate_labels={"track": "canary"},
    )


class TestPrometheusVerdictFlipBehavior:
    """The promote verdict flips with the Prometheus-served series (SC1)."""

    def test_healthy_series_yields_promote_verdict(self):
        session = _VerdictSession(
            baseline_error=0.01,
            candidate_error=0.01,
            request_count=500,
            baseline_p95=100.0,
            candidate_p95=100.0,
            baseline_p99=200.0,
            candidate_p99=200.0,
        )
        evaluator = LiveCanaryEvaluator(metrics_provider=_wire_provider(session))

        result = evaluator.evaluate(_context())

        assert result.passed is True

    def test_regressed_error_rate_yields_block_verdict(self):
        session = _VerdictSession(
            baseline_error=0.01,
            candidate_error=0.20,  # > error_rate_absolute_max (0.05)
            request_count=500,
            baseline_p95=100.0,
            candidate_p95=100.0,
            baseline_p99=200.0,
            candidate_p99=200.0,
        )
        evaluator = LiveCanaryEvaluator(metrics_provider=_wire_provider(session))

        result = evaluator.evaluate(_context())

        assert result.passed is False
        assert "error rate" in result.details.lower()
