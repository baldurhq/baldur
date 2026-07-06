"""
Traffic Routing Adapters.

Implementations of the TrafficRoutingAdapter interface.

OSS-tier (always available):
- LoggingTrafficRoutingAdapter: logging + app-level event emission only

Dormant-tier (relocated to ``baldur_dormant.adapters.traffic_routing``
per doc 528 D10-v2 / D16):
- K8sIngressTrafficRoutingAdapter: single-cluster Ingress-based service
  switching. Access via ``ProviderRegistry.traffic_routing.get("k8s_ingress")``
  when ``baldur_dormant`` is installed, or import directly from
  ``baldur_dormant.adapters.traffic_routing.k8s_ingress_adapter``.

Production implementations (Route53, GCP LB, etc.) are owned by the host
app and registered via ``ProviderRegistry.traffic_routing.register(...)``.
"""

from baldur.adapters.traffic_routing.logging_adapter import (
    LoggingTrafficRoutingAdapter,
)

__all__ = [
    "LoggingTrafficRoutingAdapter",
]
