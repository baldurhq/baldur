"""
TrafficRoutingAdapter / LoggingTrafficRoutingAdapter 단위 테스트.

테스트 대상 (237 약점 3):
- RoutingChange: 라우팅 변경 결과 dataclass
- TrafficRoutingAdapter: 트래픽 라우팅 추상 인터페이스
- LoggingTrafficRoutingAdapter: 기본 구현 (로깅 + 앱 레벨)
"""

from __future__ import annotations

from abc import ABC
from unittest.mock import MagicMock, patch

import pytest

from baldur.adapters.traffic_routing.logging_adapter import (
    LoggingTrafficRoutingAdapter,
)
from baldur.interfaces.traffic_routing import (
    RoutingChange,
    TrafficRoutingAdapter,
)

# =============================================================================
# RoutingChange 계약 검증 (Contract)
# =============================================================================


class TestRoutingChangeContract:
    """RoutingChange dataclass 계약값 검증."""

    def test_fields_exist(self) -> None:
        """필수 필드가 존재한다."""
        rc = RoutingChange(
            success=True,
            from_region="us-east-1",
            to_region="eu-west-1",
        )
        assert rc.success is True
        assert rc.from_region == "us-east-1"
        assert rc.to_region == "eu-west-1"

    def test_default_details_empty_dict(self) -> None:
        """details 기본값은 빈 딕셔너리."""
        rc = RoutingChange(success=True, from_region="a", to_region="b")
        assert rc.details == {}

    def test_default_rollback_info_none(self) -> None:
        """rollback_info 기본값은 None."""
        rc = RoutingChange(success=True, from_region="a", to_region="b")
        assert rc.rollback_info is None


# =============================================================================
# TrafficRoutingAdapter 계약 검증 (Contract)
# =============================================================================


class TestTrafficRoutingAdapterContract:
    """TrafficRoutingAdapter ABC 계약 검증."""

    def test_is_abstract(self) -> None:
        """TrafficRoutingAdapter는 ABC이다."""
        assert issubclass(TrafficRoutingAdapter, ABC)

    def test_cannot_instantiate(self) -> None:
        """추상 클래스는 직접 인스턴스화할 수 없다."""
        with pytest.raises(TypeError):
            TrafficRoutingAdapter()

    def test_abstract_methods(self) -> None:
        """3개 추상 메서드가 정의되어 있다."""
        abstract_methods = TrafficRoutingAdapter.__abstractmethods__
        assert "switch_primary" in abstract_methods
        assert "rollback" in abstract_methods
        assert "get_current_routing" in abstract_methods


# =============================================================================
# LoggingTrafficRoutingAdapter 동작 검증 (Behavior)
# =============================================================================


class TestLoggingTrafficRoutingAdapterBehavior:
    """LoggingTrafficRoutingAdapter 동작 검증."""

    def test_is_subclass_of_adapter(self) -> None:
        """TrafficRoutingAdapter의 하위 클래스이다."""
        assert issubclass(LoggingTrafficRoutingAdapter, TrafficRoutingAdapter)

    def test_switch_primary_returns_routing_change(self) -> None:
        """switch_primary()는 RoutingChange를 반환한다."""
        adapter = LoggingTrafficRoutingAdapter()
        with patch("baldur.services.event_bus.bus.get_event_bus"):
            result = adapter.switch_primary("us-east-1", "eu-west-1")

        assert isinstance(result, RoutingChange)

    @patch("baldur.services.event_bus.bus.get_event_bus")
    def test_switch_primary_success(self, mock_get_bus: MagicMock) -> None:
        """switch_primary()는 항상 성공한다 (앱 레벨만)."""
        mock_bus = MagicMock()
        mock_get_bus.return_value = mock_bus

        adapter = LoggingTrafficRoutingAdapter()
        result = adapter.switch_primary("us-east-1", "eu-west-1")

        assert result.success is True
        assert result.from_region == "us-east-1"
        assert result.to_region == "eu-west-1"

    @patch("baldur.services.event_bus.bus.get_event_bus")
    def test_switch_primary_publishes_event(self, mock_get_bus: MagicMock) -> None:
        """switch_primary()는 REGION_PRIMARY_CHANGED 이벤트를 발행한다."""
        mock_bus = MagicMock()
        mock_get_bus.return_value = mock_bus

        adapter = LoggingTrafficRoutingAdapter()
        adapter.switch_primary("us-east-1", "eu-west-1")

        mock_bus.publish.assert_called_once()
        event = mock_bus.publish.call_args[0][0]
        from baldur.services.event_bus.bus import EventType

        assert event.event_type == EventType.REGION_PRIMARY_CHANGED
        assert event.data["value"] == "eu-west-1"
        assert event.data["previous"] == "us-east-1"
        assert event.source == "failover"

    @patch("baldur.services.event_bus.bus.get_event_bus")
    def test_switch_primary_details_app_only(self, mock_get_bus: MagicMock) -> None:
        """switch_primary() 결과의 details는 앱 레벨만임을 나타낸다."""
        mock_bus = MagicMock()
        mock_get_bus.return_value = mock_bus

        adapter = LoggingTrafficRoutingAdapter()
        result = adapter.switch_primary("a", "b")

        assert result.details["level"] == "app_only"
        assert result.details["dns_updated"] is False

    @patch("baldur.services.event_bus.bus.get_event_bus")
    def test_switch_primary_event_bus_failure_still_succeeds(
        self, mock_get_bus: MagicMock
    ) -> None:
        """이벤트 발행 실패해도 RoutingChange는 성공으로 반환한다."""
        mock_bus = MagicMock()
        mock_bus.publish.side_effect = Exception("Redis down")
        mock_get_bus.return_value = mock_bus

        adapter = LoggingTrafficRoutingAdapter()
        result = adapter.switch_primary("a", "b")

        assert result.success is True

    @patch("baldur.services.event_bus.bus.get_event_bus")
    def test_rollback_calls_switch_primary_reversed(
        self, mock_get_bus: MagicMock
    ) -> None:
        """rollback()은 switch_primary()를 역방향으로 호출한다."""
        mock_bus = MagicMock()
        mock_get_bus.return_value = mock_bus

        adapter = LoggingTrafficRoutingAdapter()
        change = RoutingChange(
            success=True, from_region="us-east-1", to_region="eu-west-1"
        )
        result = adapter.rollback(change)

        assert result is True
        # 2번 호출됨 (rollback → switch_primary)
        # switch_primary는 to→from 순서로 호출되어야 함
        calls = mock_bus.publish.call_args_list
        assert len(calls) == 1
        event = calls[0][0][0]
        # 롤백이므로 eu-west-1 → us-east-1
        assert event.data["value"] == "us-east-1"
        assert event.data["previous"] == "eu-west-1"

    def test_get_current_routing(self) -> None:
        """get_current_routing()은 어댑터 정보를 반환한다."""
        adapter = LoggingTrafficRoutingAdapter()
        result = adapter.get_current_routing()

        assert isinstance(result, dict)
        assert result["adapter"] == "logging"


# =============================================================================
# 커스텀 어댑터 주입 검증 (Behavior)
# =============================================================================


class TestCustomAdapterInjectionBehavior:
    """커스텀 TrafficRoutingAdapter 구현체 주입 검증."""

    def test_custom_adapter_implementation(self) -> None:
        """사용자 정의 어댑터를 구현하고 사용할 수 있다."""

        class MockRoute53Adapter(TrafficRoutingAdapter):
            def switch_primary(self, from_region: str, to_region: str) -> RoutingChange:
                return RoutingChange(
                    success=True,
                    from_region=from_region,
                    to_region=to_region,
                    details={"dns_updated": True, "provider": "route53"},
                )

            def rollback(self, routing_change: RoutingChange) -> bool:
                return True

            def get_current_routing(self) -> dict:
                return {"provider": "route53"}

        adapter = MockRoute53Adapter()
        result = adapter.switch_primary("us-east-1", "eu-west-1")

        assert result.success is True
        assert result.details["dns_updated"] is True
        assert adapter.get_current_routing()["provider"] == "route53"
