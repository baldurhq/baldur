"""
Logging Traffic Routing Adapter.

Default adapter that operates purely at the app level, without DNS/LB-level
switching.

How it works:
1. Publish a REGION_PRIMARY_CHANGED event on the RedisEventBus
2. ServiceLocalityRouter receives the event and refreshes its routing table
3. Requests are routed to the new primary

If DNS/LB-level switching is required, implement TrafficRoutingAdapter and
register it with the ProviderRegistry.
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
    Default adapter — logging plus app-level event publication.

    Operates purely at the app level, without DNS/LB-level switching:
    1. Publish a REGION_PRIMARY_CHANGED event on the RedisEventBus
    2. ServiceLocalityRouter receives the event and refreshes its routing table

    Routing switches immediately at the app level, without waiting for DNS TTL
    propagation (up to 60 seconds).
    """

    def switch_primary(self, from_region: str, to_region: str) -> RoutingChange:
        """
        Switch the primary region at the app level.

        Leaves DNS/LB untouched and publishes a primary-change event to every
        instance over the RedisEventBus.

        Args:
            from_region: Current primary region
            to_region: New primary region

        Returns:
            RoutingChange result
        """
        logger.warning(
            "traffic_routing.app_level_routing_update",
            from_region=from_region,
            to_region=to_region,
        )

        # Propagate app-level routing
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
        Roll back app-level routing.

        Calls switch_primary() in the reverse direction.

        Args:
            routing_change: Return value of switch_primary()

        Returns:
            True if the rollback succeeded
        """
        return self.switch_primary(
            routing_change.to_region,
            routing_change.from_region,
        ).success

    def get_current_routing(self) -> dict[str, Any]:
        """Return the current routing state."""
        return {"adapter": "logging", "note": "app-level only"}
