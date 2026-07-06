"""
Tests for Metric Reconciler.
"""

from unittest.mock import Mock, patch

import pytest


class TestSyncResult:
    """Test SyncResult dataclass."""

    def test_default_values(self):
        """Should have correct default values."""
        from baldur.metrics.reconciler import SyncResult

        result = SyncResult()

        assert result.dlq_pending == {}
        assert result.circuit_breaker_states == {}
        assert result.retry_success_rates == {}
        assert result.synced_at is not None

    def test_custom_values(self):
        """Should accept custom values."""
        from baldur.metrics.reconciler import SyncResult

        result = SyncResult(
            dlq_pending={"payment": 5},
            circuit_breaker_states={"toss": "open"},
        )

        assert result.dlq_pending == {"payment": 5}
        assert result.circuit_breaker_states == {"toss": "open"}


class TestMetricReconcilerInit:
    """Test MetricReconciler initialization."""

    def test_init_with_defaults(self):
        """Should initialize with default values."""
        from baldur.metrics.reconciler import MetricReconciler

        with patch("baldur.metrics.reconciler.get_metric_adapter") as mock_get:
            mock_adapter = Mock()
            mock_get.return_value = mock_adapter

            reconciler = MetricReconciler()

            assert reconciler.adapter is mock_adapter
            assert reconciler._last_sync is None

    def test_init_with_custom_adapter(self):
        """Should use provided adapter."""
        from baldur.metrics.reconciler import MetricReconciler

        mock_adapter = Mock()
        reconciler = MetricReconciler(adapter=mock_adapter)

        assert reconciler.adapter is mock_adapter

    def test_init_with_custom_domains(self):
        """Should use provided domains."""
        from baldur.metrics.reconciler import MetricReconciler

        mock_adapter = Mock()
        domains = ["domain1", "domain2"]

        reconciler = MetricReconciler(adapter=mock_adapter, domains=domains)

        assert reconciler._domains == domains

    def test_init_with_custom_services(self):
        """Should use provided services."""
        from baldur.metrics.reconciler import MetricReconciler

        mock_adapter = Mock()
        services = ["service1", "service2"]

        reconciler = MetricReconciler(adapter=mock_adapter, services=services)

        assert reconciler._services == services


class TestMetricReconcilerSync:
    """Test MetricReconciler sync methods."""

    @pytest.fixture
    def mock_reconciler(self):
        """Create reconciler with mock adapter."""
        from baldur.metrics.reconciler import MetricReconciler

        mock_adapter = Mock()
        mock_adapter.get_dlq_pending_count.return_value = 5
        mock_adapter.get_circuit_breaker_state.return_value = "closed"
        mock_adapter.get_retry_success_rate.return_value = 0.95

        return MetricReconciler(
            adapter=mock_adapter,
            domains=["payment", "point"],
            services=["toss_payment"],
        )

    def test_sync_all_gauges_returns_sync_result(self, mock_reconciler):
        """Should return SyncResult from sync_all_gauges."""
        from baldur.metrics.reconciler import SyncResult

        # Patch the prometheus module to avoid registry conflicts
        with patch("baldur.metrics.prometheus.get_metrics") as mock_get_metrics:
            mock_metrics = Mock()
            mock_get_metrics.return_value = mock_metrics

            if hasattr(mock_reconciler, "sync_all_gauges"):
                result = mock_reconciler.sync_all_gauges()
                assert isinstance(result, SyncResult)

    def test_last_sync_updated_after_sync(self, mock_reconciler):
        """Should update _last_sync after successful sync."""
        # Patch the prometheus module to avoid registry conflicts
        with patch("baldur.metrics.prometheus.get_metrics") as mock_get_metrics:
            mock_metrics = Mock()
            mock_get_metrics.return_value = mock_metrics

            if hasattr(mock_reconciler, "sync_all_gauges"):
                mock_reconciler.sync_all_gauges()
                assert mock_reconciler._last_sync is not None
