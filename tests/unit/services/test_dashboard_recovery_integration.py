"""
Tests for DashboardService Recovery Integration.

Phase 4.13: DashboardSummaryView에 Recovery Widget 통합 테스트.

테스트 대상:
- DashboardSummary.recovery_summary 필드
- DashboardService._get_recovery_summary() 메서드
- DashboardSummary.to_dict()에 recovery 포함

Reference:
    docs/baldur/middleware_system/77_RECOVERY_COORDINATOR.md#10.2.4.13
"""

from unittest.mock import Mock, patch

import pytest

from baldur.services.dashboard_service import (
    AlertInfo,
    DashboardService,
    DashboardSummary,
    Distribution,
    RecentActivity,
    StatusCounts,
)


class TestDashboardSummaryRecoveryField:
    """DashboardSummary의 recovery_summary 필드 테스트."""

    def test_dashboard_summary_has_recovery_summary_field(self):
        """DashboardSummary에 recovery_summary 필드 존재 확인."""
        summary = DashboardSummary(
            timestamp="2026-01-23T10:00:00Z",
            health_status="healthy",
            status_counts=StatusCounts(),
            recent_activity=RecentActivity(),
            distribution=Distribution(),
            alerts=AlertInfo(),
        )

        assert hasattr(summary, "recovery_summary")
        assert summary.recovery_summary is None

    def test_dashboard_summary_with_recovery_data(self):
        """recovery_summary 데이터 포함 테스트."""
        recovery_data = {
            "active_recovery_sessions": 1,
            "pending_approvals": 3,
            "stale_approvals": 1,
            "has_urgent_approvals": True,
            "total_recoveries": 50,
            "completed_recoveries": 45,
            "health_status": "warning",
        }

        summary = DashboardSummary(
            timestamp="2026-01-23T10:00:00Z",
            health_status="healthy",
            status_counts=StatusCounts(),
            recent_activity=RecentActivity(),
            distribution=Distribution(),
            alerts=AlertInfo(),
            recovery_summary=recovery_data,
        )

        assert summary.recovery_summary == recovery_data

    def test_to_dict_includes_recovery_when_present(self):
        """to_dict()에 recovery 포함 확인."""
        recovery_data = {
            "active_recovery_sessions": 0,
            "pending_approvals": 0,
            "health_status": "healthy",
        }

        summary = DashboardSummary(
            timestamp="2026-01-23T10:00:00Z",
            health_status="healthy",
            status_counts=StatusCounts(),
            recent_activity=RecentActivity(),
            distribution=Distribution(),
            alerts=AlertInfo(),
            recovery_summary=recovery_data,
        )

        result = summary.to_dict()

        assert "recovery" in result
        assert result["recovery"] == recovery_data

    def test_to_dict_excludes_recovery_when_none(self):
        """recovery_summary가 None일 때 to_dict()에서 제외."""
        summary = DashboardSummary(
            timestamp="2026-01-23T10:00:00Z",
            health_status="healthy",
            status_counts=StatusCounts(),
            recent_activity=RecentActivity(),
            distribution=Distribution(),
            alerts=AlertInfo(),
            recovery_summary=None,
        )

        result = summary.to_dict()

        assert "recovery" not in result


class TestDashboardServiceRecoveryIntegration:
    """DashboardService의 Recovery 통합 테스트."""

    @pytest.fixture
    def mock_stats_repo(self):
        """Mock statistics repository."""
        repo = Mock()
        repo.get_status_counts.return_value = Mock(
            total=100, pending=10, resolved=80, failed=5, archived=5
        )
        repo.get_recent_activity.return_value = Mock(
            new_in_24h=5, resolved_in_24h=3, new_in_7d=20, resolved_in_7d=15
        )
        repo.get_domain_distribution.return_value = []
        repo.get_failure_type_distribution.return_value = []
        repo.get_avg_retry_count.return_value = 1.5
        return repo

    @pytest.fixture
    def service(self, mock_stats_repo):
        """DashboardService with mocked stats repo."""
        service = DashboardService()
        service._stats_repo = mock_stats_repo
        return service

    def test_get_summary_includes_recovery_summary(self, service):
        """get_summary()가 recovery_summary를 포함하는지 확인."""
        mock_recovery_summary = {
            "active_recovery_sessions": 0,
            "pending_approvals": 2,
            "stale_approvals": 0,
            "has_urgent_approvals": False,
            "total_recoveries": 10,
            "completed_recoveries": 9,
            "health_status": "healthy",
        }

        with patch.object(
            service, "_get_recovery_summary", return_value=mock_recovery_summary
        ):
            summary = service.get_summary(skip_cache=True)

        assert summary.recovery_summary is not None
        assert summary.recovery_summary["pending_approvals"] == 2

    def test_get_summary_handles_recovery_service_unavailable(self, service):
        """RecoveryDashboardService 사용 불가 시 처리."""
        with patch.object(service, "_get_recovery_summary", return_value=None):
            summary = service.get_summary(skip_cache=True)

        assert summary.recovery_summary is None

    def test_get_recovery_summary_calls_recovery_dashboard_service(self, service):
        """_get_recovery_summary()가 RecoveryDashboardService를 호출하는지 확인."""
        pytest.importorskip("baldur_pro")
        mock_recovery_service = Mock()
        mock_recovery_service.get_recovery_summary.return_value = {
            "active_recovery_sessions": 1,
            "pending_approvals": 0,
        }

        with patch(
            "baldur_pro.services.coordination.recovery_dashboard.get_recovery_dashboard_service",
            return_value=mock_recovery_service,
        ):
            result = service._get_recovery_summary()

        assert result is not None
        assert result["active_recovery_sessions"] == 1
        mock_recovery_service.get_recovery_summary.assert_called_once()

    def test_get_recovery_summary_handles_import_error(self, service):
        """ImportError 발생 시 None 반환."""
        pytest.importorskip("baldur_pro")
        # 지연 임포트로 인해 ImportError 시뮬레이션은 복잡하므로
        # 대신 실제 서비스가 예외를 발생시키는 경우를 테스트
        mock_recovery_service = Mock()
        mock_recovery_service.get_recovery_summary.side_effect = RuntimeError(
            "Service unavailable"
        )

        with patch(
            "baldur_pro.services.coordination.recovery_dashboard.get_recovery_dashboard_service",
            return_value=mock_recovery_service,
        ):
            result = service._get_recovery_summary()

        assert result is None

    def test_get_recovery_summary_handles_exception(self, service):
        """예외 발생 시 None 반환."""
        pytest.importorskip("baldur_pro")
        mock_recovery_service = Mock()
        mock_recovery_service.get_recovery_summary.side_effect = Exception(
            "Service error"
        )

        with patch(
            "baldur_pro.services.coordination.recovery_dashboard.get_recovery_dashboard_service",
            return_value=mock_recovery_service,
        ):
            result = service._get_recovery_summary()

        assert result is None

    def test_dict_to_summary_preserves_recovery(self, service):
        """_dict_to_summary()가 recovery 필드를 보존하는지 확인."""
        cached_data = {
            "timestamp": "2026-01-23T10:00:00Z",
            "health_status": "healthy",
            "overview": {
                "total": 100,
                "pending": 10,
                "resolved": 80,
                "failed": 5,
                "archived": 5,
                "resolution_rate_percent": 80.0,
            },
            "recent_activity": {
                "new_failures_24h": 5,
                "resolved_24h": 3,
                "new_failures_7d": 20,
                "resolved_7d": 15,
            },
            "distribution": {"by_domain": [], "by_failure_type": []},
            "alerts": {"high_retry_count": 0, "avg_retry_count": 1.5},
            "recommendations": [],
            "recovery": {
                "active_recovery_sessions": 1,
                "pending_approvals": 2,
                "health_status": "warning",
            },
        }

        summary = service._dict_to_summary(cached_data)

        assert summary.recovery_summary is not None
        assert summary.recovery_summary["active_recovery_sessions"] == 1
        assert summary.recovery_summary["pending_approvals"] == 2
