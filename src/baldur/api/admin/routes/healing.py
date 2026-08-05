"""Healing-summary admin route.

A single read-only route serving the console's healing counters and latency
tokens. Deliberately fixed-shape and parameterless — this is the console's own
data source, not a general metrics-query API; PromQL-shaped analysis stays with
Prometheus and Grafana.
"""

from __future__ import annotations

import structlog

from baldur.api.admin.registry import AdminRegistry, AdminRoute
from baldur.interfaces.web_framework import HttpMethod, PermissionLevel

logger = structlog.get_logger()

__all__ = ["_register_healing_routes"]


def _register_healing_routes(registry: AdminRegistry) -> None:
    try:
        from baldur.api.handlers.healing import healing_summary
    except Exception as exc:
        logger.debug("admin.healing_routes_unavailable", error=exc)
        return

    registry.register(
        AdminRoute(
            HttpMethod.GET,
            "/healing/summary",
            healing_summary,
            PermissionLevel.VIEWER,
        )
    )
