"""
ProviderRegistry traffic routing 관련 단위 테스트.

테스트 대상 (237 약점 3):
- ProviderRegistry.register_traffic_routing()
- ProviderRegistry.get_traffic_routing()
- _auto_register_traffic_routing_adapters()
"""

from __future__ import annotations

import pytest

from baldur.adapters.traffic_routing.logging_adapter import (
    LoggingTrafficRoutingAdapter,
)
from baldur.factory import ProviderRegistry
from baldur.interfaces.traffic_routing import (
    RoutingChange,
    TrafficRoutingAdapter,
)


class TestProviderRegistryTrafficRoutingBehavior:
    """ProviderRegistry traffic routing 동작 검증."""

    def setup_method(self) -> None:
        ProviderRegistry.reset()

    def teardown_method(self) -> None:
        ProviderRegistry.reset()

    def test_get_default_traffic_routing(self) -> None:
        """기본 어댑터는 LoggingTrafficRoutingAdapter이다."""
        adapter = ProviderRegistry.get_traffic_routing()
        assert isinstance(adapter, LoggingTrafficRoutingAdapter)

    def test_register_custom_adapter(self) -> None:
        """커스텀 어댑터를 등록하고 조회할 수 있다."""

        class DummyRouter(TrafficRoutingAdapter):
            def switch_primary(self, from_region: str, to_region: str) -> RoutingChange:
                return RoutingChange(
                    success=True, from_region=from_region, to_region=to_region
                )

            def rollback(self, routing_change: RoutingChange) -> bool:
                return True

            def get_current_routing(self) -> dict:
                return {"provider": "dummy"}

        ProviderRegistry.register_traffic_routing("dummy", DummyRouter)
        adapter = ProviderRegistry.get_traffic_routing(name="dummy")

        assert isinstance(adapter, DummyRouter)
        assert adapter.get_current_routing()["provider"] == "dummy"

    def test_get_unknown_adapter_raises_adapter_not_found_error(self) -> None:
        """등록되지 않은 어댑터 조회 시 AdapterNotFoundError."""
        from baldur.core.exceptions import AdapterNotFoundError

        with pytest.raises(AdapterNotFoundError, match="traffic_routing"):
            ProviderRegistry.get_traffic_routing(name="nonexistent")

    def test_singleton_behavior(self) -> None:
        """singleton=True(기본) 시 같은 인스턴스를 반환한다."""
        adapter1 = ProviderRegistry.get_traffic_routing()
        adapter2 = ProviderRegistry.get_traffic_routing()
        assert adapter1 is adapter2

    def test_non_singleton_behavior(self) -> None:
        """singleton=False 시 새 인스턴스를 반환한다."""
        adapter1 = ProviderRegistry.get_traffic_routing(singleton=False)
        adapter2 = ProviderRegistry.get_traffic_routing(singleton=False)
        assert adapter1 is not adapter2

    def test_reset_clears_traffic_routing(self) -> None:
        """reset() 후 기본 어댑터로 복원된다."""

        class DummyRouter(TrafficRoutingAdapter):
            def switch_primary(self, fr: str, to: str) -> RoutingChange:
                return RoutingChange(success=True, from_region=fr, to_region=to)

            def rollback(self, rc: RoutingChange) -> bool:
                return True

            def get_current_routing(self) -> dict:
                return {}

        ProviderRegistry.register_traffic_routing("dummy", DummyRouter)
        ProviderRegistry.reset()

        # auto-register로 logging이 다시 등록됨
        adapter = ProviderRegistry.get_traffic_routing()
        assert isinstance(adapter, LoggingTrafficRoutingAdapter)

    def test_auto_register_default_adapter(self) -> None:
        """auto_discover registers logging adapter on demand."""
        # reset 후 traffic_routing._providers 비어있음
        assert ProviderRegistry.traffic_routing.list_providers() == []
        # get_traffic_routing 호출 시 자동 등록
        adapter = ProviderRegistry.get_traffic_routing()
        assert ProviderRegistry.traffic_routing.has_provider("logging")
        assert isinstance(adapter, LoggingTrafficRoutingAdapter)
