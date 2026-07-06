"""
Logging Traffic Routing Adapter.

DNS/LB 레벨 전환 없이 앱 레벨에서만 동작하는 기본 어댑터입니다.

동작 방식:
1. RedisEventBus로 REGION_PRIMARY_CHANGED 이벤트 발행
2. ServiceLocalityRouter가 이벤트 수신 후 라우팅 테이블 갱신
3. 새 Primary로 요청 라우팅

DNS/LB 레벨 전환이 필요한 경우 TrafficRoutingAdapter를 구현하여
ProviderRegistry에 등록하세요.
"""

from __future__ import annotations

from typing import Any

import structlog

from baldur.interfaces.traffic_routing import (
    RoutingChange,
    TrafficRoutingAdapter,
)

logger = structlog.get_logger()


class LoggingTrafficRoutingAdapter(TrafficRoutingAdapter):
    """
    기본 어댑터 — 로깅 + 앱 레벨 이벤트 발행.

    DNS/LB 레벨 전환 없이 앱 레벨에서만 동작합니다:
    1. RedisEventBus로 REGION_PRIMARY_CHANGED 이벤트 발행
    2. ServiceLocalityRouter가 이벤트 수신 후 라우팅 테이블 갱신

    DNS TTL 전파(최대 60초) 없이 즉시 앱 레벨에서 라우팅이 전환됩니다.
    """

    def switch_primary(self, from_region: str, to_region: str) -> RoutingChange:
        """
        앱 레벨 Primary 리전 전환.

        DNS/LB는 변경하지 않고, RedisEventBus를 통해
        모든 인스턴스에 Primary 변경 이벤트를 발행합니다.

        Args:
            from_region: 현재 Primary 리전
            to_region: 새 Primary 리전

        Returns:
            RoutingChange 결과
        """
        logger.warning(
            "traffic_routing.app_level_routing_update",
            from_region=from_region,
            to_region=to_region,
        )

        # 앱 레벨 라우팅 전파
        try:
            from baldur.services.event_bus.bus import (
                BaldurEvent,
                EventType,
                get_event_bus,
            )

            bus = get_event_bus()
            bus.publish(
                BaldurEvent(
                    event_type=EventType.REGION_PRIMARY_CHANGED,
                    data={
                        "key": "region_primary",
                        "value": to_region,
                        "previous": from_region,
                    },
                    source="failover",
                )
            )
        except Exception as e:
            logger.exception(
                "traffic_routing.event_publish_failed",
                error=e,
            )

        return RoutingChange(
            success=True,
            from_region=from_region,
            to_region=to_region,
            details={"level": "app_only", "dns_updated": False},
        )

    def rollback(self, routing_change: RoutingChange) -> bool:
        """
        앱 레벨 라우팅 롤백.

        switch_primary()를 역방향으로 호출합니다.

        Args:
            routing_change: switch_primary() 반환값

        Returns:
            True if 롤백 성공
        """
        return self.switch_primary(
            routing_change.to_region,
            routing_change.from_region,
        ).success

    def get_current_routing(self) -> dict[str, Any]:
        """현재 라우팅 상태 조회."""
        return {"adapter": "logging", "note": "app-level only"}
