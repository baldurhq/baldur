"""Manual circuit-breaker control routes (action / status / audit / quick ops)."""

from __future__ import annotations

import structlog

from baldur.api.admin.registry import AdminRegistry, AdminRoute
from baldur.interfaces.web_framework import HttpMethod, PermissionLevel

logger = structlog.get_logger()


def _register_circuit_breaker_control_routes(registry: AdminRegistry) -> None:
    try:
        from baldur.api.handlers.circuit_breaker import (
            control_action,
            control_audit,
            control_status,
            quick_allow,
            quick_block,
            quick_reset,
            service_status,
        )
    except Exception as exc:
        logger.debug("admin.cb_control_routes_unavailable", error=exc)
        return

    routes = (
        AdminRoute(HttpMethod.POST, "/control", control_action, PermissionLevel.ADMIN),
        AdminRoute(
            HttpMethod.GET, "/control/status", control_status, PermissionLevel.VIEWER
        ),
        AdminRoute(
            HttpMethod.GET,
            "/control/status/{service_name}",
            service_status,
            PermissionLevel.VIEWER,
        ),
        AdminRoute(
            HttpMethod.GET,
            "/control/audit",
            control_audit,
            PermissionLevel.VIEWER,
        ),
        AdminRoute(
            HttpMethod.POST,
            "/control/allow/{service_name}",
            quick_allow,
            PermissionLevel.ADMIN,
        ),
        AdminRoute(
            HttpMethod.POST,
            "/control/block/{service_name}",
            quick_block,
            PermissionLevel.ADMIN,
        ),
        AdminRoute(
            HttpMethod.POST,
            "/control/reset/{service_name}",
            quick_reset,
            PermissionLevel.ADMIN,
        ),
    )
    for route in routes:
        registry.register(route)
