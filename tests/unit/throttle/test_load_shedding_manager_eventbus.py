"""
LoadSheddingManager EventBus 발행 단위 테스트.

테스트 대상:
1. update_shedding_state()에서 LOAD_SHEDDING_LEVEL_CHANGED 이벤트 발행
2. _publish_shedding_event(): 활성화/비활성화/레벨변경 이벤트 데이터 정합성
3. EventBus import 실패 시 Fail-Open 처리
"""

from unittest.mock import MagicMock, patch

import pytest

from baldur.services.circuit_breaker.load_shedding.manager import (
    LoadSheddingManager,
)
from baldur.services.circuit_breaker.models import (
    LoadSheddingPolicy,
    ServiceConfig,
    SheddingLevel,
)
from baldur.services.event_bus.bus import (
    EventPriority,
    EventType,
)


@pytest.fixture(autouse=True)
def reset_singletons():
    """각 테스트 전후로 싱글톤 초기화."""
    LoadSheddingManager.reset_instance()
    yield
    LoadSheddingManager.reset_instance()


@pytest.fixture
def default_policy():
    """기본 Load Shedding 정책."""
    return LoadSheddingPolicy(
        enabled=True,
        levels=[
            SheddingLevel(
                error_rate=30.0,
                traffic_limit=50.0,
                shed_criticality=["low"],
                description="Level 1: low 50%",
            ),
            SheddingLevel(
                error_rate=50.0,
                traffic_limit=20.0,
                shed_criticality=["low", "medium"],
                description="Level 2: low+medium 20%",
            ),
        ],
    )


@pytest.fixture
def sample_services():
    """테스트용 서비스 설정."""
    return [
        ServiceConfig(
            service_id="payment-api", criticality="critical", shed_priority=0
        ),
        ServiceConfig(service_id="review-api", criticality="low", shed_priority=10),
        ServiceConfig(
            service_id="notification-api", criticality="medium", shed_priority=5
        ),
    ]


@pytest.fixture
def manager_with_services(default_policy, sample_services):
    """서비스가 등록된 Manager."""
    manager = LoadSheddingManager(policy=default_policy)
    for svc in sample_services:
        manager.register_service(svc)
    return manager


class TestUpdateSheddingStatePublishesEvent:
    """update_shedding_state()에서 EventBus 이벤트 발행 검증."""

    def test_publishes_event_on_shedding_activated(self, manager_with_services):
        """Shedding 활성화 시 LOAD_SHEDDING_LEVEL_CHANGED 이벤트 발행."""
        manager = manager_with_services

        mock_bus = MagicMock()
        with patch("baldur.services.event_bus.get_event_bus", return_value=mock_bus):
            # Critical 서비스 에러율 30% → Level 1 활성화
            manager.set_error_rate("payment-api", 35.0)
            manager.update_shedding_state()

            mock_bus.publish.assert_called_once()
            published_event = mock_bus.publish.call_args[0][0]
            assert published_event.event_type == EventType.LOAD_SHEDDING_LEVEL_CHANGED
            assert published_event.data["new_level"] == 0
            assert published_event.data["previous_level"] == -1
            assert published_event.data["traffic_limit"] == 50.0
            assert published_event.source == "load_shedding_manager"
            assert published_event.priority == EventPriority.HIGH

    def test_publishes_event_on_shedding_deactivated(self, manager_with_services):
        """Shedding 비활성화 시 이벤트 발행."""
        manager = manager_with_services

        # 먼저 활성화
        manager.set_error_rate("payment-api", 35.0)
        manager.update_shedding_state()

        mock_bus = MagicMock()
        with patch("baldur.services.event_bus.get_event_bus", return_value=mock_bus):
            # 에러율 0%로 비활성화
            manager.set_error_rate("payment-api", 0.0)
            manager.update_shedding_state()

            mock_bus.publish.assert_called_once()
            published_event = mock_bus.publish.call_args[0][0]
            assert published_event.data["new_level"] == -1
            assert published_event.data["traffic_limit"] == 100.0

    def test_publishes_event_with_affected_services(self, manager_with_services):
        """이벤트에 affected_services 포함 확인."""
        manager = manager_with_services

        mock_bus = MagicMock()
        with patch("baldur.services.event_bus.get_event_bus", return_value=mock_bus):
            manager.set_error_rate("payment-api", 35.0)
            manager.update_shedding_state()

            published_event = mock_bus.publish.call_args[0][0]
            assert "affected_services" in published_event.data
            assert isinstance(published_event.data["affected_services"], list)

    def test_no_event_when_level_unchanged(self, manager_with_services):
        """레벨 변경 없으면 이벤트 미발행."""
        manager = manager_with_services

        mock_bus = MagicMock()
        with patch("baldur.services.event_bus.get_event_bus", return_value=mock_bus):
            # 에러율 0% → 레벨 변경 없음
            result = manager.update_shedding_state()
            assert result is None
            mock_bus.publish.assert_not_called()

    def test_event_includes_critical_error_rate(self, manager_with_services):
        """이벤트에 critical_error_rate 포함 확인."""
        manager = manager_with_services

        mock_bus = MagicMock()
        with patch("baldur.services.event_bus.get_event_bus", return_value=mock_bus):
            manager.set_error_rate("payment-api", 35.0)
            manager.update_shedding_state()

            published_event = mock_bus.publish.call_args[0][0]
            assert "critical_error_rate" in published_event.data
            assert published_event.data["critical_error_rate"] > 0


class TestPublishSheddingEventFailOpen:
    """_publish_shedding_event() Fail-Open 검증."""

    def test_import_error_does_not_raise(self, manager_with_services):
        """EventBus import 실패 시 예외 미발생."""
        manager = manager_with_services

        with patch.dict("sys.modules", {"baldur.services.event_bus": None}):
            # 예외가 발생하지 않아야 함
            manager.set_error_rate("payment-api", 35.0)
            result = manager.update_shedding_state()
            assert result is not None  # audit_entry는 정상 반환

    def test_publish_error_does_not_raise(self, manager_with_services):
        """EventBus publish 실패 시 예외 미발생."""
        manager = manager_with_services

        mock_bus = MagicMock()
        mock_bus.publish.side_effect = RuntimeError("Bus error")
        with patch("baldur.services.event_bus.get_event_bus", return_value=mock_bus):
            # 예외가 발생하지 않아야 함
            manager.set_error_rate("payment-api", 35.0)
            result = manager.update_shedding_state()
            assert result is not None
