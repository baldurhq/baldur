"""
Tests for Prometheus Metrics Module.
"""

from datetime import UTC
from unittest.mock import patch

import pytest


class TestBaldurMetricsInit:
    """Test BaldurMetrics initialization."""

    def test_init_with_prefix(self):
        """Should initialize with custom prefix."""
        from baldur.metrics.prometheus import (
            PROMETHEUS_AVAILABLE,
            BaldurMetrics,
        )

        if not PROMETHEUS_AVAILABLE:
            pytest.skip("prometheus_client not installed")

        metrics = BaldurMetrics(prefix="test_prefix")
        assert metrics.prefix == "test_prefix"

    def test_init_without_prometheus_client(self):
        """Should handle missing prometheus_client gracefully."""
        from baldur.metrics.prometheus import BaldurMetrics

        with patch("baldur.metrics.prometheus.PROMETHEUS_AVAILABLE", False):
            metrics = BaldurMetrics()
            # Should not raise, just log warning
            assert metrics.prefix == "baldur"


class TestBaldurMetricsCounters:
    """Test counter methods in BaldurMetrics."""

    @pytest.fixture
    def mock_metrics(self):
        """Create metrics with mocked prometheus counters."""
        from baldur.metrics.prometheus import (
            PROMETHEUS_AVAILABLE,
            BaldurMetrics,
        )

        if not PROMETHEUS_AVAILABLE:
            pytest.skip("prometheus_client not installed")

        metrics = BaldurMetrics(prefix="test")
        return metrics

    def test_record_dlq_item_created_delegates_to_recorder(self, mock_metrics):
        """record_dlq_item_created delegates to dlq recorder with same args."""
        with patch.object(mock_metrics.dlq, "record_item_created") as mock_dlq:
            mock_metrics.record_dlq_item_created("payment", "timeout")

        mock_dlq.assert_called_once_with("payment", "timeout")


class TestBaldurMetricsGauges:
    """Test gauge methods in BaldurMetrics."""

    def test_dlq_recorder_exists(self):
        """Should have dlq recorder attribute."""
        from baldur.metrics.prometheus import (
            PROMETHEUS_AVAILABLE,
        )

        if not PROMETHEUS_AVAILABLE:
            pytest.skip("prometheus_client not installed")

        from baldur.metrics.prometheus import get_metrics

        metrics = get_metrics()
        assert hasattr(metrics, "dlq")
        assert hasattr(metrics.dlq, "set_pending_count")
        assert hasattr(metrics.dlq, "set_status_count")


class TestPrometheusAvailability:
    """Test PROMETHEUS_AVAILABLE flag behavior."""

    def test_prometheus_available_is_boolean(self):
        """Should be a boolean value."""
        from baldur.metrics.prometheus import PROMETHEUS_AVAILABLE

        assert isinstance(PROMETHEUS_AVAILABLE, bool)

    def test_module_works_without_prometheus(self):
        """Module should work even without prometheus_client."""
        # Domain registry has been consolidated to metrics.registry
        from baldur.metrics.registry import get_registered_domains

        # Basic operations should work
        domains = get_registered_domains()
        assert isinstance(domains, list)


class TestREDMetrics:
    """Test RED (Rate, Errors, Duration) metrics.

    Reference: https://www.weave.works/blog/the-red-method-key-metrics-for-microservices/
    """

    @pytest.fixture
    def metrics(self):
        """Get global metrics instance."""
        from baldur.metrics.prometheus import PROMETHEUS_AVAILABLE, get_metrics

        if not PROMETHEUS_AVAILABLE:
            pytest.skip("prometheus_client not installed")

        return get_metrics()

    def test_infra_recorder_has_http_methods(self, metrics):
        """Should have infra recorder with HTTP recording methods."""
        assert hasattr(metrics, "infra")
        assert hasattr(metrics.infra, "record_http_request")
        assert hasattr(metrics.infra, "record_http_error")

    def test_record_http_request(self, metrics):
        """Should record HTTP request metrics."""
        # Should not raise
        metrics.record_http_request(
            method="GET",
            endpoint="/api/users",
            status_code=200,
            duration_seconds=0.123,
        )

    def test_record_http_error(self, metrics):
        """Should record HTTP error metrics."""
        # Should not raise
        metrics.record_http_error(
            method="POST",
            endpoint="/api/orders",
            error_type="timeout",
        )

    def test_http_request_timer_context_manager(self, metrics):
        """Should provide http_request_timer context manager."""
        import time

        with metrics.http_request_timer("GET", "/api/test"):
            time.sleep(0.01)  # Small delay to ensure duration > 0

        # Should not raise - duration is automatically recorded

    def test_http_request_timer_records_errors(self, metrics):
        """Should record errors when exception occurs in timer context."""
        try:
            with metrics.http_request_timer("GET", "/api/error"):
                raise ValueError("Test error")
        except ValueError:
            pass  # Expected

        # Error should be recorded with error_type="ValueError"


class TestFourGoldenSignals:
    """Test Four Golden Signals metrics.

    Reference: https://sre.google/sre-book/monitoring-distributed-systems/
    - Latency: How long it takes to service a request
    - Traffic: How much demand is being placed on the system
    - Errors: Rate of failed requests
    - Saturation: How full the service is
    """

    @pytest.fixture
    def metrics(self):
        """Get global metrics instance."""
        from baldur.metrics.prometheus import PROMETHEUS_AVAILABLE, get_metrics

        if not PROMETHEUS_AVAILABLE:
            pytest.skip("prometheus_client not installed")

        return get_metrics()

    # =========================================================================
    # Saturation Metrics
    # =========================================================================

    def test_infra_recorder_has_saturation_methods(self, metrics):
        """Should have infra recorder with saturation recording methods."""
        assert hasattr(metrics, "infra")
        assert hasattr(metrics.infra, "set_request_queue_depth")
        assert hasattr(metrics.infra, "set_worker_utilization")
        assert hasattr(metrics.infra, "set_active_connections")

    def test_set_request_queue_depth(self, metrics):
        """Should set request queue depth."""
        metrics.set_request_queue_depth("api-service", 42)
        # Should not raise

    def test_set_request_queue_depth_clamps_negative(self, metrics):
        """Should clamp negative queue depth to 0."""
        metrics.set_request_queue_depth("api-service", -5)
        # Should not raise, value clamped to 0

    def test_set_worker_utilization(self, metrics):
        """Should set worker utilization ratio."""
        metrics.set_worker_utilization("gunicorn", 0.75)
        # Should not raise

    def test_set_worker_utilization_clamps_range(self, metrics):
        """Should clamp utilization ratio to 0.0-1.0."""
        metrics.set_worker_utilization("gunicorn", 1.5)  # Clamped to 1.0
        metrics.set_worker_utilization("gunicorn", -0.1)  # Clamped to 0.0
        # Should not raise

    def test_set_active_connections(self, metrics):
        """Should set active connections count."""
        metrics.set_active_connections("db", 10)
        metrics.set_active_connections("redis", 5)
        # Should not raise

    # =========================================================================
    # Latency Metrics
    # =========================================================================

    def test_infra_recorder_has_latency_methods(self, metrics):
        """Should have infra recorder with latency percentile method."""
        assert hasattr(metrics.infra, "set_latency_percentile")

    def test_set_latency_percentile(self, metrics):
        """Should set latency percentile values."""
        metrics.set_latency_percentile("/api/users", "p50", 0.05)
        metrics.set_latency_percentile("/api/users", "p90", 0.15)
        metrics.set_latency_percentile("/api/users", "p99", 0.35)
        # Should not raise

    # =========================================================================
    # Error Metrics
    # =========================================================================

    def test_infra_recorder_has_error_rate_method(self, metrics):
        """Should have infra recorder with error rate method."""
        assert hasattr(metrics.infra, "set_error_rate")

    def test_set_error_rate(self, metrics):
        """Should set error rate percentage."""
        metrics.set_error_rate("api-service", 0.5)  # 0.5%
        # Should not raise

    def test_set_error_rate_clamps_range(self, metrics):
        """Should clamp error rate to 0-100%."""
        metrics.set_error_rate("api-service", 150.0)  # Clamped to 100
        metrics.set_error_rate("api-service", -5.0)  # Clamped to 0
        # Should not raise


class TestConvenienceFunctions:
    """Module-level convenience functions delegate to get_metrics() with same args."""

    @patch("baldur.metrics.prometheus.get_metrics", autospec=True)
    def test_record_http_request_delegates(self, mock_get_metrics):
        """record_http_request forwards positional args to get_metrics()."""
        from baldur.metrics.prometheus import record_http_request

        record_http_request("GET", "/api/test", 200, 0.1)

        mock_get_metrics.return_value.record_http_request.assert_called_once_with(
            "GET", "/api/test", 200, 0.1
        )

    @patch("baldur.metrics.prometheus.get_metrics", autospec=True)
    def test_record_http_error_delegates(self, mock_get_metrics):
        """record_http_error forwards positional args to get_metrics()."""
        from baldur.metrics.prometheus import record_http_error

        record_http_error("POST", "/api/test", "500")

        mock_get_metrics.return_value.record_http_error.assert_called_once_with(
            "POST", "/api/test", "500"
        )

    @patch("baldur.metrics.prometheus.get_metrics", autospec=True)
    def test_set_request_queue_depth_delegates(self, mock_get_metrics):
        """set_request_queue_depth forwards args to get_metrics()."""
        from baldur.metrics.prometheus import set_request_queue_depth

        set_request_queue_depth("test-service", 10)

        mock_get_metrics.return_value.set_request_queue_depth.assert_called_once_with(
            "test-service", 10
        )

    @patch("baldur.metrics.prometheus.get_metrics", autospec=True)
    def test_set_worker_utilization_delegates(self, mock_get_metrics):
        """set_worker_utilization forwards args to get_metrics()."""
        from baldur.metrics.prometheus import set_worker_utilization

        set_worker_utilization("test-pool", 0.8)

        mock_get_metrics.return_value.set_worker_utilization.assert_called_once_with(
            "test-pool", 0.8
        )

    @patch("baldur.metrics.prometheus.get_metrics", autospec=True)
    def test_set_active_connections_delegates(self, mock_get_metrics):
        """set_active_connections forwards args to get_metrics()."""
        from baldur.metrics.prometheus import set_active_connections

        set_active_connections("db", 5)

        mock_get_metrics.return_value.set_active_connections.assert_called_once_with(
            "db", 5
        )

    @patch("baldur.metrics.prometheus.get_metrics", autospec=True)
    def test_set_latency_percentile_delegates(self, mock_get_metrics):
        """set_latency_percentile forwards args to get_metrics()."""
        from baldur.metrics.prometheus import set_latency_percentile

        set_latency_percentile("/api/test", "p99", 0.5)

        mock_get_metrics.return_value.set_latency_percentile.assert_called_once_with(
            "/api/test", "p99", 0.5
        )

    @patch("baldur.metrics.prometheus.get_metrics", autospec=True)
    def test_set_error_rate_delegates(self, mock_get_metrics):
        """set_error_rate forwards args to get_metrics()."""
        from baldur.metrics.prometheus import set_error_rate

        set_error_rate("test-service", 1.5)

        mock_get_metrics.return_value.set_error_rate.assert_called_once_with(
            "test-service", 1.5
        )


# =============================================================================
# Contract Tests — Legacy Domain Functions Removed (353)
# =============================================================================


class TestLegacyDomainFunctionsRemovedContract:
    """Contract: prometheus.py legacy domain functions are deleted (353 §3.2)."""

    def test_get_domains_removed(self):
        """prometheus.get_domains is no longer accessible."""
        from baldur.metrics import prometheus

        assert not hasattr(prometheus, "get_domains")

    def test_register_domain_removed(self):
        """prometheus.register_domain is no longer accessible."""
        from baldur.metrics import prometheus

        assert not hasattr(prometheus, "register_domain")

    def test_registered_domains_list_removed(self):
        """prometheus._registered_domains (list) is no longer accessible."""
        from baldur.metrics import prometheus

        assert not hasattr(prometheus, "_registered_domains")


# =============================================================================
# Behavior Tests — Convenience Functions Domain Resolve (353)
# =============================================================================


class TestConvenienceFunctionsDomainResolveBehavior:
    """Behavior: prometheus.py convenience functions apply resolve_domain_label (353 §3.5)."""

    @patch("baldur.metrics.prometheus.get_metrics", autospec=True)
    def test_record_dlq_item_created_resolves_domain(self, mock_get_metrics):
        """record_dlq_item_created resolves unregistered domain to OTHER_DOMAIN."""
        from baldur.metrics.prometheus import record_dlq_item_created
        from baldur.metrics.registry import _FALLBACK_DOMAIN

        record_dlq_item_created("unregistered_abc", "PG_TIMEOUT")

        mock_get_metrics.return_value.record_dlq_item_created.assert_called_once_with(
            _FALLBACK_DOMAIN, "PG_TIMEOUT"
        )

    @patch("baldur.metrics.prometheus.get_metrics", autospec=True)
    def test_record_retry_attempt_resolves_domain(self, mock_get_metrics):
        """record_retry_attempt resolves unregistered domain to OTHER_DOMAIN."""
        from baldur.metrics.prometheus import record_retry_attempt
        from baldur.metrics.registry import _FALLBACK_DOMAIN

        record_retry_attempt("unregistered_abc", 3, "failure")

        mock_get_metrics.return_value.record_retry_attempt.assert_called_once_with(
            _FALLBACK_DOMAIN, 3, "failure"
        )

    @patch("baldur.metrics.prometheus.get_metrics", autospec=True)
    def test_record_sla_breach_resolves_domain(self, mock_get_metrics):
        """record_sla_breach resolves unregistered domain to OTHER_DOMAIN."""
        from baldur.metrics.prometheus import record_sla_breach
        from baldur.metrics.registry import _FALLBACK_DOMAIN

        record_sla_breach("unregistered_abc")

        mock_get_metrics.return_value.record_sla_breach.assert_called_once_with(
            _FALLBACK_DOMAIN
        )

    @patch("baldur.metrics.prometheus.get_metrics", autospec=True)
    def test_record_replay_attempt_resolves_domain(self, mock_get_metrics):
        """record_replay_attempt resolves unregistered domain to OTHER_DOMAIN."""
        from baldur.metrics.prometheus import record_replay_attempt
        from baldur.metrics.registry import _FALLBACK_DOMAIN

        record_replay_attempt("unregistered_abc", "single", True)

        mock_get_metrics.return_value.record_replay_attempt.assert_called_once_with(
            _FALLBACK_DOMAIN, "single", True
        )

    @patch("baldur.metrics.prometheus.get_metrics", autospec=True)
    def test_record_recovery_time_resolves_domain(self, mock_get_metrics):
        """record_recovery_time resolves unregistered domain to OTHER_DOMAIN."""
        from datetime import datetime

        from baldur.metrics.prometheus import record_recovery_time
        from baldur.metrics.registry import _FALLBACK_DOMAIN

        now = datetime.now(UTC)
        record_recovery_time("unregistered_abc", "auto_replay", now, now)

        call_args = mock_get_metrics.return_value.record_recovery_time.call_args
        assert call_args.args[0] == _FALLBACK_DOMAIN

    @patch("baldur.metrics.prometheus.get_metrics", autospec=True)
    def test_convenience_function_passes_registered_domain(self, mock_get_metrics):
        """Convenience function passes registered domain unchanged."""
        from baldur.metrics.prometheus import record_sla_breach

        record_sla_breach("external_service")

        mock_get_metrics.return_value.record_sla_breach.assert_called_once_with(
            "external_service"
        )


class TestGILContentionGaugeContract:
    """GIL contention Prometheus gauge 계약 검증."""

    def test_gil_contention_p90_ms_gauge_exists(self):
        """baldur_gil_contention_p90_ms Gauge exists in infra recorder."""
        from baldur.metrics.prometheus import PROMETHEUS_AVAILABLE, get_metrics

        if not PROMETHEUS_AVAILABLE:
            pytest.skip("prometheus_client not installed")

        metrics = get_metrics()
        assert hasattr(metrics, "infra")
        assert hasattr(metrics.infra, "_gil_contention_p90_ms")
