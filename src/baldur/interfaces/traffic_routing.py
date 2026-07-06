"""
Traffic Routing Adapter Interface.

Abstract interface for shifting traffic during a regional outage.

Example implementations:
- AWS Route53 (boto3)
- GCP Global Load Balancer (google-cloud-compute)
- Cloudflare DNS (cloudflare)
- Kubernetes Ingress (kubernetes)
- App-level routing (integrating with ServiceLocalityRouter)

The default implementation only logs (LoggingTrafficRoutingAdapter).
In production, users register an adapter with the ProviderRegistry.

Usage:
    from baldur.interfaces.traffic_routing import (
        TrafficRoutingAdapter,
        RoutingChange,
    )

    # Custom adapter implementation
    class Route53TrafficRouter(TrafficRoutingAdapter):
        def __init__(self, hosted_zone_id: str):
            self._client = boto3.client('route53')
            self._zone_id = hosted_zone_id

        def switch_primary(self, from_region, to_region) -> RoutingChange:
            self._client.change_resource_record_sets(...)
            return RoutingChange(success=True, ...)

        def rollback(self, routing_change) -> bool:
            ...

        def get_current_routing(self) -> dict:
            ...

    # Registration
    from baldur.factory import ProviderRegistry
    ProviderRegistry.register_traffic_routing("route53", Route53TrafficRouter)
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class RoutingChange:
    """
    Traffic-routing change result.

    Captures the result of a switch_primary() call and carries the
    information needed by rollback() to restore the previous state.

    Attributes:
        success: Whether the switch succeeded
        from_region: Previous Primary region
        to_region: New Primary region
        details: Switch details
        rollback_info: Previous-state info for rollback
    """

    success: bool
    """Whether the switch succeeded."""

    from_region: str
    """Previous Primary region."""

    to_region: str
    """New Primary region."""

    details: dict[str, Any] = field(default_factory=dict)
    """Switch details."""

    rollback_info: dict[str, Any] | None = None
    """Previous-state info for rollback."""


class TrafficRoutingAdapter(ABC):
    """
    Traffic routing adapter interface.

    Abstract interface for shifting traffic at the DNS/LB layer during a
    regional outage. The baldur package does not include any external
    cloud SDK, so production adapters are implemented in the host app and
    registered with the ProviderRegistry.

    Default implementation (LoggingTrafficRoutingAdapter):
        Operates at the application level only, without DNS/LB changes.
        Publishes a REGION_PRIMARY_CHANGED event via RedisEventBus so that
        ServiceLocalityRouter can refresh its routing table.

    Example (AWS Route53):
        class Route53TrafficRouter(TrafficRoutingAdapter):
            def __init__(self, hosted_zone_id: str):
                self._client = boto3.client('route53')
                self._zone_id = hosted_zone_id

            def switch_primary(self, from_region, to_region) -> RoutingChange:
                self._client.change_resource_record_sets(
                    HostedZoneId=self._zone_id,
                    ChangeBatch={...}
                )
                return RoutingChange(
                    success=True,
                    from_region=from_region,
                    to_region=to_region,
                    details={"dns_updated": True},
                )

            def rollback(self, routing_change) -> bool:
                return self.switch_primary(
                    routing_change.to_region,
                    routing_change.from_region,
                ).success

            def get_current_routing(self) -> dict:
                return {"hosted_zone": self._zone_id}

    Example (K8s Ingress):
        class K8sIngressTrafficRouter(TrafficRoutingAdapter):
            def switch_primary(self, from_region, to_region) -> RoutingChange:
                # kubectl patch ingress ...
                ...

    Registration:
        ProviderRegistry.register_traffic_routing("route53", Route53TrafficRouter)
    """

    @abstractmethod
    def switch_primary(self, from_region: str, to_region: str) -> RoutingChange:
        """
        Switch the Primary region.

        Shifts traffic to to_region at the DNS/LB layer.

        Args:
            from_region: Current Primary region
            to_region: New Primary region

        Returns:
            RoutingChange result (includes rollback info)
        """
        pass

    @abstractmethod
    def rollback(self, routing_change: RoutingChange) -> bool:
        """
        Roll back a routing change.

        Args:
            routing_change: The return value of switch_primary()

        Returns:
            True if the rollback succeeded
        """
        pass

    @abstractmethod
    def get_current_routing(self) -> dict[str, Any]:
        """Query the current routing state."""
        pass
