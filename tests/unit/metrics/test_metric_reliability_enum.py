"""
Tests for Metric Reliability.
"""

from baldur.metrics.reliability import (
    METRIC_RELIABILITY_MAP,
    MetricReliability,
    get_metric_reliability,
    get_reliability_description,
)


class TestMetricReliability:
    """Tests for MetricReliability enum."""

    def test_reliability_values(self):
        """Should have correct string values."""
        assert MetricReliability.EXACT.value == "exact"
        assert MetricReliability.EVENTUAL.value == "eventual"
        assert MetricReliability.APPROXIMATE.value == "approx"


class TestMetricReliabilityMap:
    """Tests for METRIC_RELIABILITY_MAP."""

    def test_counters_are_exact(self):
        """Counter metrics should be marked as EXACT."""
        assert METRIC_RELIABILITY_MAP["dlq_items_total"] == MetricReliability.EXACT
        assert METRIC_RELIABILITY_MAP["retry_outcomes_total"] == MetricReliability.EXACT
        assert METRIC_RELIABILITY_MAP["sla_breach_total"] == MetricReliability.EXACT

    def test_histograms_are_exact(self):
        """Histogram metrics should be marked as EXACT."""
        assert (
            METRIC_RELIABILITY_MAP["recovery_time_seconds"] == MetricReliability.EXACT
        )
        assert METRIC_RELIABILITY_MAP["retry_delay_seconds"] == MetricReliability.EXACT

    def test_gauges_are_eventual(self):
        """Gauge metrics should be marked as EVENTUAL."""
        assert METRIC_RELIABILITY_MAP["dlq_pending_count"] == MetricReliability.EVENTUAL
        assert (
            METRIC_RELIABILITY_MAP["circuit_breaker_state"]
            == MetricReliability.EVENTUAL
        )
        assert (
            METRIC_RELIABILITY_MAP["retry_success_rate"] == MetricReliability.EVENTUAL
        )


class TestGetMetricReliability:
    """Tests for get_metric_reliability function."""

    def test_returns_exact_for_counters(self):
        """Should return EXACT for counter metrics."""
        reliability = get_metric_reliability("dlq_items_total")
        assert reliability == MetricReliability.EXACT

    def test_returns_eventual_for_gauges(self):
        """Should return EVENTUAL for gauge metrics."""
        reliability = get_metric_reliability("dlq_pending_count")
        assert reliability == MetricReliability.EVENTUAL

    def test_returns_approximate_for_unknown(self):
        """Should return APPROXIMATE for unknown metrics."""
        reliability = get_metric_reliability("unknown_metric")
        assert reliability == MetricReliability.APPROXIMATE


class TestGetReliabilityDescription:
    """Tests for get_reliability_description function."""

    def test_exact_description(self):
        """Should return meaningful description for EXACT."""
        desc = get_reliability_description(MetricReliability.EXACT)
        assert "100%" in desc

    def test_eventual_description(self):
        """Should return meaningful description for EVENTUAL."""
        desc = get_reliability_description(MetricReliability.EVENTUAL)
        assert "99%" in desc
        assert "restart" in desc.lower()

    def test_approximate_description(self):
        """Should return meaningful description for APPROXIMATE."""
        desc = get_reliability_description(MetricReliability.APPROXIMATE)
        assert "95%" in desc
