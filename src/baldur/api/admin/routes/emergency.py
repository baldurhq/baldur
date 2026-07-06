"""Emergency mode admin routes (status / trigger / release / recovery / config)."""

from __future__ import annotations

import structlog

from baldur.api.admin.registry import AdminRegistry, AdminRoute
from baldur.interfaces.web_framework import HttpMethod, PermissionLevel

logger = structlog.get_logger()


def _register_emergency_routes(registry: AdminRegistry) -> None:
    try:
        from baldur.api.handlers.emergency import (
            emergency_config_get,
            emergency_config_update,
            emergency_history,
            emergency_levels,
            emergency_release,
            emergency_status,
            emergency_trigger,
            gradual_recovery_start,
            gradual_recovery_stop,
        )
    except Exception as exc:
        logger.debug("admin.emergency_routes_unavailable", error=exc)
        return

    routes = (
        AdminRoute(
            HttpMethod.GET,
            "/emergency/status",
            emergency_status,
            PermissionLevel.VIEWER,
        ),
        AdminRoute(
            HttpMethod.POST,
            "/emergency/trigger",
            emergency_trigger,
            PermissionLevel.ADMIN,
        ),
        AdminRoute(
            HttpMethod.POST,
            "/emergency/release",
            emergency_release,
            PermissionLevel.ADMIN,
        ),
        AdminRoute(
            HttpMethod.POST,
            "/emergency/gradual-recovery",
            gradual_recovery_start,
            PermissionLevel.ADMIN,
        ),
        AdminRoute(
            HttpMethod.POST,
            "/emergency/stop-recovery",
            gradual_recovery_stop,
            PermissionLevel.ADMIN,
        ),
        AdminRoute(
            HttpMethod.GET,
            "/emergency/history",
            emergency_history,
            PermissionLevel.VIEWER,
        ),
        AdminRoute(
            HttpMethod.GET,
            "/emergency/config",
            emergency_config_get,
            PermissionLevel.ADMIN,
        ),
        AdminRoute(
            HttpMethod.PUT,
            "/emergency/config",
            emergency_config_update,
            PermissionLevel.ADMIN,
        ),
        AdminRoute(
            HttpMethod.GET,
            "/emergency/levels",
            emergency_levels,
            PermissionLevel.VIEWER,
        ),
    )
    for route in routes:
        registry.register(route)
