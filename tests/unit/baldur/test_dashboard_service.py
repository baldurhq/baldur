"""
Unit tests for DashboardService.

Tests for the dashboard service methods:
- get_summary()
- get_status_counts()
- get_recent_activity()
- get_distribution()
- get_alerts()
- calculate_resolution_rate()
- determine_health_status()
"""

from datetime import UTC
from unittest.mock import MagicMock, Mock, patch

import pytest

from baldur.services.dashboard_service import (
    AlertInfo,
    DashboardService,
    DashboardSummary,
    Distribution,
    RecentActivity,
    StatusCounts,
    get_dashboard_service,
)


class TestStatusCounts:
    """Tests for StatusCounts dataclass."""

    def test_default_values(self):
        """Test default values."""
        counts = StatusCounts()
        assert counts.total == 0
        assert counts.pending == 0
        assert counts.resolved == 0
        assert counts.failed == 0
        assert counts.archived == 0

    def test_custom_values(self):
        """Test custom values."""
        counts = StatusCounts(
            total=100,
            pending=20,
            resolved=50,
            failed=10,
            archived=20,
        )
        assert counts.total == 100
        assert counts.pending == 20
        assert counts.resolved == 50
        assert counts.failed == 10
        assert counts.archived == 20


class TestRecentActivity:
    """Tests for RecentActivity dataclass."""

    def test_default_values(self):
        """Test default values."""
        activity = RecentActivity()
        assert activity.new_in_24h == 0
        assert activity.resolved_in_24h == 0
        assert activity.new_in_7d == 0
        assert activity.resolved_in_7d == 0

    def test_custom_values(self):
        """Test custom values."""
        activity = RecentActivity(
            new_in_24h=5,
            resolved_in_24h=3,
            new_in_7d=20,
            resolved_in_7d=15,
        )
        assert activity.new_in_24h == 5
        assert activity.resolved_in_24h == 3
        assert activity.new_in_7d == 20
        assert activity.resolved_in_7d == 15


class TestDistribution:
    """Tests for Distribution dataclass."""

    def test_default_values(self):
        """Test default values."""
        dist = Distribution()
        assert dist.by_domain == []
        assert dist.by_failure_type == []

    def test_custom_values(self):
        """Test custom values."""
        dist = Distribution(
            by_domain=[{"domain": "order", "count": 10}],
            by_failure_type=[{"failure_type": "network", "count": 5}],
        )
        assert len(dist.by_domain) == 1
        assert dist.by_domain[0]["domain"] == "order"
        assert len(dist.by_failure_type) == 1


class TestAlertInfo:
    """Tests for AlertInfo dataclass."""

    def test_default_values(self):
        """Test default values."""
        alert = AlertInfo()
        assert alert.high_retry_count == 0
        assert alert.avg_retry_count == 0.0

    def test_custom_values(self):
        """Test custom values."""
        alert = AlertInfo(high_retry_count=5, avg_retry_count=2.5)
        assert alert.high_retry_count == 5
        assert alert.avg_retry_count == 2.5


class TestDashboardSummary:
    """Tests for DashboardSummary dataclass."""

    def test_to_dict(self):
        """Test to_dict conversion."""
        summary = DashboardSummary(
            timestamp="2025-12-19T10:00:00+09:00",
            health_status="healthy",
            status_counts=StatusCounts(
                total=100, pending=0, resolved=80, failed=0, archived=20
            ),
            recent_activity=RecentActivity(
                new_in_24h=2, resolved_in_24h=5, new_in_7d=10, resolved_in_7d=15
            ),
            distribution=Distribution(
                by_domain=[{"domain": "order", "count": 5}],
                by_failure_type=[{"failure_type": "network", "count": 3}],
            ),
            alerts=AlertInfo(high_retry_count=0, avg_retry_count=1.5),
            resolution_rate_percent=100.0,
            recommendations=[],
        )

        result = summary.to_dict()

        assert result["timestamp"] == "2025-12-19T10:00:00+09:00"
        assert result["health_status"] == "healthy"
        assert result["overview"]["total"] == 100
        assert result["overview"]["pending"] == 0
        assert result["overview"]["resolution_rate_percent"] == 100.0
        assert result["recent_activity"]["new_failures_24h"] == 2
        assert result["distribution"]["by_domain"] == [{"domain": "order", "count": 5}]
        assert result["alerts"]["high_retry_count"] == 0
        assert result["recommendations"] == []


class TestDashboardServiceHealthStatus:
    """Tests for determine_health_status method."""

    def test_healthy_status(self):
        """Test healthy status when no pending or failed."""
        service = DashboardService()
        assert service.determine_health_status(pending=0, failed=0) == "healthy"

    def test_good_status(self):
        """Test good status when low pending and no failed."""
        service = DashboardService()
        assert service.determine_health_status(pending=5, failed=0) == "good"
        assert service.determine_health_status(pending=10, failed=0) == "good"

    def test_warning_status(self):
        """Test warning status."""
        service = DashboardService()
        # pending <= 50
        assert service.determine_health_status(pending=30, failed=0) == "warning"
        assert service.determine_health_status(pending=50, failed=0) == "warning"
        # failed <= 5
        assert service.determine_health_status(pending=0, failed=3) == "warning"
        assert service.determine_health_status(pending=0, failed=5) == "warning"

    def test_critical_status(self):
        """Test critical status when both thresholds exceeded."""
        service = DashboardService()
        assert service.determine_health_status(pending=60, failed=10) == "critical"
        assert service.determine_health_status(pending=100, failed=20) == "critical"


class TestDashboardServiceResolutionRate:
    """Tests for calculate_resolution_rate method."""

    def test_zero_total(self):
        """Test resolution rate with zero total."""
        service = DashboardService()
        assert service.calculate_resolution_rate(resolved=0, total=0, archived=0) == 0.0

    def test_all_archived(self):
        """Test resolution rate when all entries are archived."""
        service = DashboardService()
        assert (
            service.calculate_resolution_rate(resolved=0, total=100, archived=100)
            == 0.0
        )

    def test_full_resolution(self):
        """Test 100% resolution rate."""
        service = DashboardService()
        rate = service.calculate_resolution_rate(resolved=80, total=100, archived=20)
        assert rate == 100.0

    def test_partial_resolution(self):
        """Test partial resolution rate."""
        service = DashboardService()
        # 40 resolved out of 80 active (100 - 20 archived)
        rate = service.calculate_resolution_rate(resolved=40, total=100, archived=20)
        assert rate == 50.0

    def test_resolution_rate_rounding(self):
        """Test resolution rate rounding."""
        service = DashboardService()
        rate = service.calculate_resolution_rate(resolved=33, total=100, archived=0)
        assert rate == 33.0


class TestDashboardServiceWithMockedRepo:
    """Tests for DashboardService with mocked stats_repo."""

    @pytest.fixture
    def mock_stats_repo(self):
        """Create a mock statistics repository."""
        mock = MagicMock()
        mock.get_status_counts.return_value = StatusCounts(
            total=80, pending=10, resolved=50, failed=5, archived=15
        )
        mock.get_recent_activity.return_value = RecentActivity(
            new_in_24h=5, resolved_in_24h=3, new_in_7d=20, resolved_in_7d=15
        )
        mock.get_distribution.return_value = MagicMock(
            by_domain=[{"domain": "order", "count": 10}],
            by_failure_type=[{"failure_type": "network", "count": 5}],
        )
        mock.get_alerts.return_value = MagicMock(
            high_retry_count=3, avg_retry_count=2.5
        )
        return mock

    @pytest.fixture
    def service_with_mock(self, mock_stats_repo):
        """Create a DashboardService with mocked stats_repo."""
        service = DashboardService()
        service._stats_repo = mock_stats_repo
        return service

    def test_get_status_counts(self, service_with_mock, mock_stats_repo):
        """Test get_status_counts method via stats_repo."""
        result = service_with_mock.get_status_counts()

        assert isinstance(result, StatusCounts)
        assert result.total == 80
        assert result.pending == 10
        assert result.resolved == 50
        mock_stats_repo.get_status_counts.assert_called_once()


class TestGetDashboardService:
    """Tests for get_dashboard_service singleton function."""

    def test_returns_dashboard_service(self):
        """Test that get_dashboard_service returns a DashboardService instance."""
        # Reset singleton
        import baldur.services.dashboard_service as module

        module._dashboard_service = None

        service = get_dashboard_service()
        assert isinstance(service, DashboardService)

    def test_returns_same_instance(self):
        """Test that get_dashboard_service returns the same instance."""
        import baldur.services.dashboard_service as module

        module._dashboard_service = None

        service1 = get_dashboard_service()
        service2 = get_dashboard_service()
        assert service1 is service2


class TestDashboardServiceGetSummary:
    """Tests for get_summary method integration."""

    @patch("baldur.services.dashboard_service.DashboardService.get_status_counts")
    @patch("baldur.services.dashboard_service.DashboardService.get_recent_activity")
    @patch("baldur.services.dashboard_service.DashboardService.get_distribution")
    @patch("baldur.services.dashboard_service.DashboardService.get_alerts")
    @patch("baldur.services.dashboard_service.now")
    def test_get_summary_integration(
        self,
        mock_now,
        mock_alerts,
        mock_dist,
        mock_activity,
        mock_counts,
    ):
        """Test get_summary integrates all components."""
        from datetime import datetime

        mock_now.return_value = datetime(2025, 12, 19, 10, 0, 0, tzinfo=UTC)
        mock_counts.return_value = StatusCounts(
            total=100, pending=10, resolved=70, failed=5, archived=15
        )
        mock_activity.return_value = RecentActivity(
            new_in_24h=3, resolved_in_24h=5, new_in_7d=15, resolved_in_7d=20
        )
        mock_dist.return_value = Distribution(
            by_domain=[{"domain": "order", "count": 5}],
            by_failure_type=[{"failure_type": "network", "count": 3}],
        )
        mock_alerts.return_value = AlertInfo(high_retry_count=2, avg_retry_count=1.5)

        service = DashboardService()
        summary = service.get_summary()

        assert isinstance(summary, DashboardSummary)
        assert (
            summary.health_status == "warning"
        )  # pending=10 (good) but failed=5 triggers warning
        assert summary.resolution_rate_percent > 0

        # Verify to_dict works
        result_dict = summary.to_dict()
        assert "timestamp" in result_dict
        assert "health_status" in result_dict
        assert "overview" in result_dict
        assert "recent_activity" in result_dict
        assert "distribution" in result_dict
        assert "alerts" in result_dict


class TestDashboardCaching:
    """Tests for Dashboard Redis caching."""

    @pytest.fixture
    def mock_cache(self):
        """Create a mock cache provider."""
        cache = Mock()
        cache.get.return_value = None  # Cache miss by default
        cache.set.return_value = None
        return cache

    @pytest.fixture
    def service_with_cache(self, mock_cache):
        """Create DashboardService with mocked cache."""
        service = DashboardService(cache=mock_cache)
        # Also mock stats_repo to avoid ProviderRegistry dependency
        mock_repo = Mock()
        mock_repo.get_status_counts.return_value = StatusCounts(
            total=100, pending=10, resolved=70, failed=5, archived=15
        )
        mock_repo.get_recent_activity.return_value = RecentActivity(
            new_in_24h=5, resolved_in_24h=3, new_in_7d=20, resolved_in_7d=15
        )
        mock_repo.get_distribution.return_value = Distribution(
            by_domain=[], by_failure_type=[]
        )
        mock_repo.get_alerts.return_value = AlertInfo(
            high_retry_count=0, avg_retry_count=0.0
        )
        service._stats_repo = mock_repo
        return service

    def test_cache_hit(self, mock_cache, service_with_cache):
        """Test that cache is used when available."""
        cached_data = {
            "timestamp": "2025-12-21T10:00:00Z",
            "health_status": "healthy",
            "overview": {
                "total": 10,
                "pending": 0,
                "resolved": 5,
                "failed": 0,
                "archived": 5,
                "resolution_rate_percent": 50.0,
            },
            "recent_activity": {
                "new_failures_24h": 0,
                "resolved_24h": 2,
                "new_failures_7d": 5,
                "resolved_7d": 5,
            },
            "distribution": {"by_domain": [], "by_failure_type": []},
            "alerts": {"high_retry_count": 0, "avg_retry_count": 0.0},
            "recommendations": [],
        }
        mock_cache.get.return_value = cached_data

        result = service_with_cache.get_summary()

        # Cache should be queried
        assert mock_cache.get.called
        # Result should match cached data
        assert result.health_status == "healthy"
        assert result.status_counts.total == 10

    def test_cache_miss_and_set(self, mock_cache, service_with_cache):
        """Test that cache is populated on cache miss."""
        mock_cache.get.return_value = None  # Cache miss

        service_with_cache.get_summary()

        # Cache should be set after fetching fresh data
        assert mock_cache.set.called

    def test_skip_cache_flag(self, mock_cache, service_with_cache):
        """Test that skip_cache=True bypasses cache."""
        cached_data = {"health_status": "healthy", "overview": {"total": 999}}
        mock_cache.get.return_value = cached_data

        result = service_with_cache.get_summary(skip_cache=True)

        # stats_repo should be accessed (cache bypassed)
        assert service_with_cache.stats_repo.get_status_counts.called
        # Result should be fresh data, not cached
        assert result.status_counts.total == 100  # From mock_repo, not 999
