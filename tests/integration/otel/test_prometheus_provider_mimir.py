"""
Integration test — PrometheusTimeSeriesProvider against a real PromQL engine.

Seeds an OTLP HTTP-server histogram through the OTel Collector, lets the
``prometheusremotewrite`` exporter translate it into Grafana Mimir, then queries
it back through ``PrometheusTimeSeriesProvider`` with the ``otel`` naming preset.
This empirically pins two things a mock cannot:

1. the real PromQL engine accepts the provider's rendered queries
   (sum(rate)/histogram_quantile), and
2. the ``otel`` preset's metric names match the ``prometheusremotewrite`` name
   translation (``http.server.request.duration`` [s] →
   ``http_server_request_duration_seconds`` with ``_bucket``/``_count``).

Reuses the existing Mimir + OTLP-JSON-POST lane (no new compose service, marker,
or autoskip wiring). Auto-skips without ``TEST_OTEL_AVAILABLE``.

Target: baldur.services.config_shadow.providers.prometheus (otel preset)
"""

from __future__ import annotations

import os
import time
import uuid
from datetime import UTC, datetime, timedelta

import pytest
import requests

from baldur.services.config_shadow.providers.prometheus import (
    PrometheusTimeSeriesProvider,
)
from baldur.settings.prometheus import PrometheusSettings

pytestmark = pytest.mark.requires_otel

OTEL_COLLECTOR_ENDPOINT = os.getenv(
    "OTEL_COLLECTOR_ENDPOINT", "http://otel-collector:4318"
)
MIMIR_ENDPOINT = os.getenv("MIMIR_ENDPOINT", "http://mimir:9009")

# The OTel semantic-convention HTTP-server histogram (unit seconds), which the
# otel preset targets after prometheusremotewrite translation.
_OTLP_METRIC_NAME = "http.server.request.duration"
_TRANSLATED_COUNT = "http_server_request_duration_seconds_count"


def _histogram_payload(test_id: str, sample_index: int) -> dict:
    """A cumulative OTLP histogram data point tagged with a unique test_id."""
    now_ns = int(time.time() * 1e9)
    start_ns = now_ns - int(600 * 1e9)
    # Cumulative counts grow between the two seedings so rate()/increase() has
    # a non-zero delta over the query window.
    count = 5 * (sample_index + 1)
    return {
        "resourceMetrics": [
            {
                "resource": {
                    "attributes": [
                        {
                            "key": "service.name",
                            "value": {"stringValue": "prom-provider-mimir-test"},
                        }
                    ]
                },
                "scopeMetrics": [
                    {
                        "scope": {"name": "prometheus-provider-test"},
                        "metrics": [
                            {
                                "name": _OTLP_METRIC_NAME,
                                "unit": "s",
                                "histogram": {
                                    "aggregationTemporality": 2,  # CUMULATIVE
                                    "dataPoints": [
                                        {
                                            "startTimeUnixNano": str(start_ns),
                                            "timeUnixNano": str(now_ns),
                                            "count": str(count),
                                            "sum": 0.15 * count,
                                            "bucketCounts": [
                                                str(count // 2),
                                                str(count - count // 2),
                                                "0",
                                            ],
                                            "explicitBounds": [0.1, 0.5],
                                            "attributes": [
                                                {
                                                    "key": "http.response.status_code",
                                                    "value": {"intValue": "200"},
                                                },
                                                {
                                                    "key": "test_id",
                                                    "value": {"stringValue": test_id},
                                                },
                                            ],
                                        }
                                    ],
                                },
                            }
                        ],
                    }
                ],
            }
        ]
    }


def _seed(test_id: str, sample_index: int) -> None:
    response = requests.post(
        f"{OTEL_COLLECTOR_ENDPOINT}/v1/metrics",
        json=_histogram_payload(test_id, sample_index),
        headers={"Content-Type": "application/json"},
        timeout=10,
    )
    assert response.status_code in (200, 202), (
        f"collector rejected OTLP metric: {response.status_code} {response.text}"
    )


def _wait_for_count_series(test_id: str, max_wait: int = 90) -> float:
    """Poll Mimir until the translated _count series appears; return its value."""
    query = f'{_TRANSLATED_COUNT}{{test_id="{test_id}"}}'
    deadline = time.time() + max_wait
    while time.time() < deadline:
        try:
            resp = requests.get(
                f"{MIMIR_ENDPOINT}/prometheus/api/v1/query",
                params={"query": query},
                timeout=10,
            )
            if resp.status_code == 200:
                data = resp.json()
                result = data.get("data", {}).get("result", [])
                if result:
                    return float(result[0]["value"][1])
        except requests.RequestException:
            pass
        time.sleep(5)
    raise AssertionError(
        f"{_TRANSLATED_COUNT}{{test_id={test_id}}} not in Mimir within {max_wait}s "
        "— OTLP → collector → prometheusremotewrite → Mimir translation failed"
    )


class TestPrometheusProviderMimirIntegration:
    """The otel-preset provider queries real Mimir end-to-end."""

    def test_otel_preset_provider_reads_seeded_histogram(self):
        test_id = f"prom_prov_{uuid.uuid4().hex[:12]}"

        # Seed twice a few seconds apart so rate()/increase() has a delta.
        _seed(test_id, sample_index=0)
        time.sleep(3)
        _seed(test_id, sample_index=1)

        # Name-translation pin: the translated _count series is queryable.
        count_value = _wait_for_count_series(test_id)
        assert count_value > 0

        settings = PrometheusSettings(
            url=f"{MIMIR_ENDPOINT}/prometheus",
            metric_naming="otel",
            extra_label_selectors={"test_id": test_id},
        )
        provider = PrometheusTimeSeriesProvider(settings=settings)

        end = datetime.now(UTC)
        start = end - timedelta(seconds=600)

        # Round-trip pin: the provider's rendered PromQL executes on real Mimir.
        request_count = provider.query_request_count("svc", start, end)
        latency_ms = provider.query_latency_aggregated(
            "svc", start, end, percentile=0.95
        )
        error_rate = provider.query_error_rate_aggregated("svc", start, end)

        assert isinstance(request_count, int)
        assert isinstance(latency_ms, float)
        assert isinstance(error_rate, float)
        # No 5xx were seeded, so the error rate is 0.0 (all traffic is status 200).
        assert error_rate == 0.0
