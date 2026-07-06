"""
DeploymentCorrelator 단위 테스트.

테스트 대상:
- MockDeploymentAdapter: 테스트용 어댑터 동작
- DeploymentCorrelator: 배포 연관성 분석
- Fallback 동작: 어댑터 장애 시 동작
"""

from __future__ import annotations

import pytest

pytest.importorskip("baldur_pro")

pytestmark = pytest.mark.requires_pro


from datetime import UTC, datetime, timedelta

import pytest

from baldur.adapters.deployment import (
    DeploymentConfigChange,
    DeploymentEvent,
    DeploymentSource,
    DeploymentType,
    MockDeploymentAdapter,
)
from baldur_pro.services.postmortem.deployment_correlator import (
    CorrelationType,
    DeploymentCorrelator,
    reset_deployment_correlator,
)

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def mock_adapter():
    """Mock 어댑터 fixture."""
    return MockDeploymentAdapter()


@pytest.fixture
def sample_deployment():
    """샘플 배포 이벤트."""
    deployed_at = datetime.now(UTC) - timedelta(minutes=20)
    return DeploymentEvent(
        deployment_id="deploy-001",
        service_name="payment-service",
        version_from="v1.2.3",
        version_to="v1.2.4",
        deployed_at=deployed_at.isoformat(),
        deployed_by="ci-pipeline",
        deployment_type=DeploymentType.ROLLING,
        source=DeploymentSource.MOCK,
        namespace="default",
    )


@pytest.fixture
def sample_config_change():
    """샘플 설정 변경 이벤트."""
    changed_at = datetime.now(UTC) - timedelta(minutes=5)
    return DeploymentConfigChange(
        change_id="config-001",
        config_key="payment.timeout",
        old_value="30",
        new_value="60",
        changed_at=changed_at.isoformat(),
        changed_by="admin",
        service_name="payment-service",
        namespace="default",
    )


@pytest.fixture(autouse=True)
def reset_correlator(monkeypatch):
    """각 테스트 전후 correlator 리셋.

    impl 527 (v1.1 deferred) flipped deployment_correlator_enabled default
    to False; re-enable here so the correlation behavior tests pass.
    """
    from baldur.settings.postmortem import reset_postmortem_settings

    monkeypatch.setenv("BALDUR_POSTMORTEM_DEPLOYMENT_CORRELATOR_ENABLED", "true")
    reset_postmortem_settings()
    reset_deployment_correlator()
    yield
    reset_deployment_correlator()
    reset_postmortem_settings()


# =============================================================================
# DeploymentEvent 모델 테스트
# =============================================================================


class TestDeploymentEvent:
    """DeploymentEvent 모델 테스트."""

    def test_to_dict(self, sample_deployment):
        """to_dict 변환 테스트."""
        result = sample_deployment.to_dict()

        assert result["deployment_id"] == "deploy-001"
        assert result["service_name"] == "payment-service"
        assert result["version_from"] == "v1.2.3"
        assert result["version_to"] == "v1.2.4"
        assert result["deployed_by"] == "ci-pipeline"
        assert result["deployment_type"] == "rolling"
        assert result["source"] == "mock"

    def test_to_timeline_event(self, sample_deployment):
        """타임라인 이벤트 변환 테스트."""
        result = sample_deployment.to_timeline_event()

        assert "timestamp" in result
        assert "[DEPLOY]" in result["event_type"]
        assert "v1.2.3 → v1.2.4" in result["event_type"]
        assert "details" in result

    def test_rollback_timeline_event(self):
        """롤백 타임라인 이벤트 테스트."""
        deployed_at = datetime.now(UTC).isoformat()
        deploy = DeploymentEvent(
            deployment_id="deploy-002",
            service_name="payment-service",
            version_from="v1.2.4",
            version_to="v1.2.3",
            deployed_at=deployed_at,
            is_rollback=True,
            source=DeploymentSource.MOCK,
        )

        result = deploy.to_timeline_event()
        assert "[ROLLBACK]" in result["event_type"]


class TestDeploymentConfigChange:
    """DeploymentConfigChange 모델 테스트."""

    def test_to_dict(self, sample_config_change):
        """to_dict 변환 테스트."""
        result = sample_config_change.to_dict()

        assert result["change_id"] == "config-001"
        assert result["config_key"] == "payment.timeout"
        assert result["old_value"] == "30"
        assert result["new_value"] == "60"

    def test_to_timeline_event(self, sample_config_change):
        """타임라인 이벤트 변환 테스트."""
        result = sample_config_change.to_timeline_event()

        assert "timestamp" in result
        assert "[CONFIG]" in result["event_type"]
        assert "payment.timeout" in result["event_type"]


# =============================================================================
# MockDeploymentAdapter 테스트
# =============================================================================


class TestMockDeploymentAdapter:
    """MockDeploymentAdapter 테스트."""

    def test_empty_adapter(self, mock_adapter):
        """빈 어댑터 테스트."""
        result = mock_adapter.get_deployments_in_range(
            service_name="test-service",
            start_time=datetime.now(UTC) - timedelta(hours=1),
            end_time=datetime.now(UTC),
        )

        assert result == []

    def test_set_mock_deployments(self, mock_adapter, sample_deployment):
        """Mock 배포 데이터 설정 테스트."""
        mock_adapter.set_mock_deployments([sample_deployment])

        result = mock_adapter.get_deployments_in_range(
            service_name="payment-service",
            start_time=datetime.now(UTC) - timedelta(hours=1),
            end_time=datetime.now(UTC),
        )

        assert len(result) == 1
        assert result[0].deployment_id == "deploy-001"

    def test_service_name_filter(self, mock_adapter, sample_deployment):
        """서비스 이름 필터 테스트."""
        mock_adapter.set_mock_deployments([sample_deployment])

        result = mock_adapter.get_deployments_in_range(
            service_name="other-service",
            start_time=datetime.now(UTC) - timedelta(hours=1),
            end_time=datetime.now(UTC),
        )

        assert result == []

    def test_time_range_filter(self, mock_adapter):
        """시간 범위 필터 테스트."""
        # 2시간 전 배포
        old_deploy = DeploymentEvent(
            deployment_id="old-001",
            service_name="payment-service",
            version_from="v1.0.0",
            version_to="v1.1.0",
            deployed_at=(datetime.now(UTC) - timedelta(hours=2)).isoformat(),
            source=DeploymentSource.MOCK,
        )

        # 30분 전 배포
        recent_deploy = DeploymentEvent(
            deployment_id="recent-001",
            service_name="payment-service",
            version_from="v1.1.0",
            version_to="v1.2.0",
            deployed_at=(datetime.now(UTC) - timedelta(minutes=30)).isoformat(),
            source=DeploymentSource.MOCK,
        )

        mock_adapter.set_mock_deployments([old_deploy, recent_deploy])

        # 1시간 범위로 조회
        result = mock_adapter.get_deployments_in_range(
            service_name="payment-service",
            start_time=datetime.now(UTC) - timedelta(hours=1),
            end_time=datetime.now(UTC),
        )

        assert len(result) == 1
        assert result[0].deployment_id == "recent-001"

    def test_get_current_version(self, mock_adapter):
        """현재 버전 조회 테스트."""
        deploy1 = DeploymentEvent(
            deployment_id="deploy-001",
            service_name="payment-service",
            version_from="v1.0.0",
            version_to="v1.1.0",
            deployed_at=(datetime.now(UTC) - timedelta(hours=2)).isoformat(),
            source=DeploymentSource.MOCK,
        )
        deploy2 = DeploymentEvent(
            deployment_id="deploy-002",
            service_name="payment-service",
            version_from="v1.1.0",
            version_to="v1.2.0",
            deployed_at=(datetime.now(UTC) - timedelta(hours=1)).isoformat(),
            source=DeploymentSource.MOCK,
        )

        mock_adapter.set_mock_deployments([deploy1, deploy2])

        result = mock_adapter.get_current_version("payment-service")
        assert result == "v1.2.0"

    def test_get_rollback_history(self, mock_adapter):
        """롤백 이력 조회 테스트."""
        normal_deploy = DeploymentEvent(
            deployment_id="deploy-001",
            service_name="payment-service",
            version_from="v1.0.0",
            version_to="v1.1.0",
            deployed_at=(datetime.now(UTC) - timedelta(hours=2)).isoformat(),
            source=DeploymentSource.MOCK,
        )
        rollback = DeploymentEvent(
            deployment_id="rollback-001",
            service_name="payment-service",
            version_from="v1.1.0",
            version_to="v1.0.0",
            deployed_at=(datetime.now(UTC) - timedelta(hours=1)).isoformat(),
            is_rollback=True,
            source=DeploymentSource.MOCK,
        )

        mock_adapter.set_mock_deployments([normal_deploy, rollback])

        result = mock_adapter.get_rollback_history("payment-service")
        assert len(result) == 1
        assert result[0].is_rollback is True

    def test_set_availability(self, mock_adapter, sample_deployment):
        """가용성 설정 테스트 (Fallback 테스트용)."""
        mock_adapter.set_mock_deployments([sample_deployment])
        mock_adapter.set_availability(False)

        assert mock_adapter.is_available() is False

        result = mock_adapter.get_deployments_in_range(
            service_name="payment-service",
            start_time=datetime.now(UTC) - timedelta(hours=1),
            end_time=datetime.now(UTC),
        )

        # 비가용 상태면 빈 목록 반환
        assert result == []


# =============================================================================
# DeploymentCorrelator 테스트
# =============================================================================


class TestDeploymentCorrelator:
    """DeploymentCorrelator 테스트."""

    def test_correlate_incident_no_deployments(self, mock_adapter):
        """배포 없을 때 상관관계 분석."""
        correlator = DeploymentCorrelator(adapter=mock_adapter)

        result = correlator._correlate_incident(
            incident_time=datetime.now(UTC),
            service_name="payment-service",
        )

        assert result.correlation_type == CorrelationType.UNLIKELY
        assert result.correlation_score == 0.0
        assert len(result.deployments) == 0

    def test_correlate_incident_deployment_triggered(self, mock_adapter):
        """배포 후 30분 내 인시던트 - 높은 상관관계."""
        # 20분 전 배포
        deploy = DeploymentEvent(
            deployment_id="deploy-001",
            service_name="payment-service",
            version_from="v1.2.3",
            version_to="v1.2.4",
            deployed_at=(datetime.now(UTC) - timedelta(minutes=20)).isoformat(),
            source=DeploymentSource.MOCK,
        )
        mock_adapter.set_mock_deployments([deploy])

        correlator = DeploymentCorrelator(adapter=mock_adapter)

        result = correlator._correlate_incident(
            incident_time=datetime.now(UTC),
            service_name="payment-service",
        )

        assert result.correlation_type == CorrelationType.DEPLOYMENT_TRIGGERED
        assert result.correlation_score >= 0.8
        assert len(result.deployments) == 1

    def test_correlate_incident_config_changed(self, mock_adapter):
        """설정 변경 후 10분 내 인시던트 - 높은 상관관계."""
        # 5분 전 설정 변경
        config_change = DeploymentConfigChange(
            change_id="config-001",
            config_key="payment.timeout",
            old_value="30",
            new_value="60",
            changed_at=(datetime.now(UTC) - timedelta(minutes=5)).isoformat(),
            service_name="payment-service",
        )
        mock_adapter.set_mock_config_changes([config_change])

        correlator = DeploymentCorrelator(adapter=mock_adapter)

        result = correlator._correlate_incident(
            incident_time=datetime.now(UTC),
            service_name="payment-service",
        )

        assert result.correlation_type == CorrelationType.CONFIG_CHANGED
        assert result.correlation_score >= 0.8

    def test_correlate_incident_possible_correlation(self, mock_adapter):
        """배포 후 1시간 내 인시던트 - 중간 상관관계."""
        # 45분 전 배포
        deploy = DeploymentEvent(
            deployment_id="deploy-001",
            service_name="payment-service",
            version_from="v1.2.3",
            version_to="v1.2.4",
            deployed_at=(datetime.now(UTC) - timedelta(minutes=45)).isoformat(),
            source=DeploymentSource.MOCK,
        )
        mock_adapter.set_mock_deployments([deploy])

        correlator = DeploymentCorrelator(adapter=mock_adapter)

        result = correlator._correlate_incident(
            incident_time=datetime.now(UTC),
            service_name="payment-service",
        )

        assert result.correlation_type == CorrelationType.POSSIBLE_CORRELATION
        assert result.correlation_score >= 0.4

    def test_get_deployments_for_postmortem(self, mock_adapter, sample_deployment):
        """Postmortem용 배포 컨텍스트 수집."""
        mock_adapter.set_mock_deployments([sample_deployment])

        correlator = DeploymentCorrelator(adapter=mock_adapter)

        result = correlator.get_deployments_for_postmortem(
            incident_time=datetime.now(UTC),
            service_name="payment-service",
        )

        assert result["status"] == "collected"
        assert "recent_deployments" in result
        assert "deployment_correlation" in result

    def test_get_deployment_timeline_events(
        self, mock_adapter, sample_deployment, sample_config_change
    ):
        """타임라인 이벤트 수집."""
        mock_adapter.set_mock_deployments([sample_deployment])
        mock_adapter.set_mock_config_changes([sample_config_change])

        correlator = DeploymentCorrelator(adapter=mock_adapter)

        result = correlator.get_deployment_timeline_events(
            incident_time=datetime.now(UTC),
            service_name="payment-service",
        )

        assert len(result) == 2  # 배포 + 설정 변경
        assert all("timestamp" in event for event in result)
        assert all("event_type" in event for event in result)


# =============================================================================
# Fallback 동작 테스트
# =============================================================================


class TestFallbackBehavior:
    """Fallback 동작 테스트."""

    def test_adapter_not_available_returns_empty(self, mock_adapter, sample_deployment):
        """어댑터 비가용 시 빈 목록 반환."""
        mock_adapter.set_mock_deployments([sample_deployment])
        mock_adapter.set_availability(False)

        correlator = DeploymentCorrelator(adapter=mock_adapter)

        result = correlator._correlate_incident(
            incident_time=datetime.now(UTC),
            service_name="payment-service",
        )

        # 배포 정보 없음 → UNLIKELY
        assert result.correlation_type == CorrelationType.UNLIKELY
        assert len(result.deployments) == 0

    def test_result_to_dict(self, mock_adapter, sample_deployment):
        """결과 딕셔너리 변환 테스트."""
        mock_adapter.set_mock_deployments([sample_deployment])

        correlator = DeploymentCorrelator(adapter=mock_adapter)

        result = correlator._correlate_incident(
            incident_time=datetime.now(UTC),
            service_name="payment-service",
        )

        dict_result = result.to_dict()

        assert "deployments" in dict_result
        assert "config_changes" in dict_result
        assert "correlation_score" in dict_result
        assert "correlation_type" in dict_result
        assert "analysis_summary" in dict_result
