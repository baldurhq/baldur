"""Extended health, metrics, and gate-config admin routes."""

from __future__ import annotations

import structlog

from baldur.api.admin.registry import AdminRegistry, AdminRoute
from baldur.interfaces.web_framework import HttpMethod, PermissionLevel

logger = structlog.get_logger()


def _register_health_routes(registry: AdminRegistry) -> None:
    try:
        from baldur.api.handlers.error_budget_gate import (
            gate_config_get,
            gate_config_update,
            gate_health,
            gate_reset,
        )
        from baldur.api.handlers.health import (
            pool_health_check,
            simple_health_ping,
        )
        from baldur.api.handlers.metrics import (
            baldur_metrics,
            prometheus_text_metrics,
        )
    except Exception as exc:
        logger.debug("admin.health_routes_unavailable", error=exc)
        return

    routes = (
        AdminRoute(
            HttpMethod.GET, "/health/pool", pool_health_check, PermissionLevel.PUBLIC
        ),
        AdminRoute(
            HttpMethod.GET, "/health/ping", simple_health_ping, PermissionLevel.PUBLIC
        ),
        AdminRoute(
            HttpMethod.GET, "/metrics", baldur_metrics, PermissionLevel.AUTHENTICATED
        ),
        AdminRoute(
            HttpMethod.GET,
            "/prometheus",
            prometheus_text_metrics,
            PermissionLevel.PUBLIC,
        ),
        AdminRoute(HttpMethod.GET, "/health/gate", gate_health, PermissionLevel.PUBLIC),
        AdminRoute(
            HttpMethod.GET,
            "/config/gate",
            gate_config_get,
            PermissionLevel.AUTHENTICATED,
        ),
        AdminRoute(
            HttpMethod.PUT,
            "/config/gate",
            gate_config_update,
            PermissionLevel.AUTHENTICATED,
        ),
        AdminRoute(
            HttpMethod.POST,
            "/gate/reset",
            gate_reset,
            PermissionLevel.AUTHENTICATED,
        ),
    )
    for route in routes:
        registry.register(route)
