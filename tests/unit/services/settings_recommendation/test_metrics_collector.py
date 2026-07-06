"""Unit tests for settings_recommendation.metrics_collector."""

from __future__ import annotations

import math
from unittest.mock import MagicMock

from baldur.services.settings_recommendation.metrics_collector import (
    ClusterMetricsCollector,
)

# ---------------------------------------------------------------------------
# Contract Tests
# ---------------------------------------------------------------------------


class TestClusterMetricsCollectorContract:
    """Design contract values for ClusterMetricsCollector."""

    def test_required_metric_keys_count(self):
        """Design contract: 5 required metric keys."""
        assert len(ClusterMetricsCollector.REQUIRED_METRIC_KEYS) == 5

    def test_required_metric_keys_values(self):
        """Design contract: required metrics match PromQL queries."""
        expected = {
            "error_rate",
            "p99_latency_ms",
            "retry_exhaustion_rate",
            "throttle_rate",
            "cb_open_ratio",
        }
        assert ClusterMetricsCollector.REQUIRED_METRIC_KEYS == expected

    def test_promql_queries_cover_all_required_keys(self):
        """Every required key has a corresponding PromQL query."""
        for key in ClusterMetricsCollector.REQUIRED_METRIC_KEYS:
            assert key in ClusterMetricsCollector.PROMQL_QUERIES


# ---------------------------------------------------------------------------
# Behavior Tests
# ---------------------------------------------------------------------------


class TestCollectBehavior:
    """collect() behavior: fallback, NaN normalization."""

    def test_collect_with_no_sources_returns_nan_for_all_keys(self):
        """Without prometheus or fallback, all values are NaN."""
        collector = ClusterMetricsCollector(
            prometheus_url=None,
            fallback_adapter=None,
        )
        result = collector.collect()

        assert set(result.keys()) == ClusterMetricsCollector.REQUIRED_METRIC_KEYS
        for value in result.values():
            assert math.isnan(value)

    def test_collect_with_fallback_uses_adapter_values(self):
        """When prometheus unavailable, fallback adapter values are used."""
        from baldur.services.settings_recommendation.metrics_collector import (
            MetricsAdapterProtocol,
        )

        mock_adapter = MagicMock(spec=MetricsAdapterProtocol)
        mock_adapter.fetch_current_metrics.return_value = {
            "error_rate": 0.02,
            "p99_latency_ms": 150.0,
        }
        collector = ClusterMetricsCollector(
            prometheus_url=None,
            fallback_adapter=mock_adapter,
        )

        result = collector.collect()

        assert result["error_rate"] == 0.02
        assert result["p99_latency_ms"] == 150.0
        # Missing keys filled with NaN
        assert math.isnan(result["retry_exhaustion_rate"])
        assert math.isnan(result["throttle_rate"])
        assert math.isnan(result["cb_open_ratio"])

    def test_collect_always_returns_all_required_keys(self):
        """Result always contains all REQUIRED_METRIC_KEYS."""
        from baldur.services.settings_recommendation.metrics_collector import (
            MetricsAdapterProtocol,
        )

        mock_adapter = MagicMock(spec=MetricsAdapterProtocol)
        mock_adapter.fetch_current_metrics.return_value = {"error_rate": 0.01}
        collector = ClusterMetricsCollector(fallback_adapter=mock_adapter)

        result = collector.collect()
        assert set(result.keys()) == ClusterMetricsCollector.REQUIRED_METRIC_KEYS


class TestMiniCircuitBreakerBehavior:
    """Mini circuit breaker state transition behavior."""

    def test_circuit_opens_after_max_failures(self):
        """Circuit opens after max_consecutive_failures."""
        collector = ClusterMetricsCollector(
            prometheus_url="http://fake:9090",
            max_consecutive_failures=3,
            retry_after_seconds=60.0,
        )

        # Simulate 3 failures
        for _ in range(3):
            collector._record_failure()

        assert collector._circuit_open_since is not None

    def test_circuit_closed_before_max_failures(self):
        """Circuit stays closed before reaching max failures."""
        collector = ClusterMetricsCollector(
            prometheus_url="http://fake:9090",
            max_consecutive_failures=3,
        )

        collector._record_failure()
        collector._record_failure()

        assert collector._circuit_open_since is None

    def test_should_use_prometheus_returns_false_when_no_url(self):
        """Without prometheus_url, prometheus is not used."""
        collector = ClusterMetricsCollector(prometheus_url=None)
        assert collector._should_use_prometheus() is False

    def test_should_use_prometheus_returns_true_when_closed(self):
        """With valid URL and closed circuit, prometheus is used."""
        collector = ClusterMetricsCollector(prometheus_url="http://prom:9090")
        assert collector._should_use_prometheus() is True
