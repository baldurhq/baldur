"""System kill-switch and dry-run control routes."""

from __future__ import annotations

import structlog

from baldur.api.admin.registry import AdminRegistry, AdminRoute
from baldur.interfaces.web_framework import HttpMethod, PermissionLevel

logger = structlog.get_logger()


def _register_system_control_routes(registry: AdminRegistry) -> None:
    try:
        from baldur.api.handlers.system_control import (
            dry_run_disable,
            dry_run_enable,
            system_disable,
            system_enable,
            system_status,
        )
    except Exception as exc:
        logger.debug("admin.system_control_routes_unavailable", error=exc)
        return

    routes = (
        AdminRoute(
            HttpMethod.GET, "/system/status", system_status, PermissionLevel.VIEWER
        ),
        AdminRoute(
            HttpMethod.POST, "/system/enable", system_enable, PermissionLevel.ADMIN
        ),
        AdminRoute(
            HttpMethod.POST, "/system/disable", system_disable, PermissionLevel.ADMIN
        ),
        AdminRoute(
            HttpMethod.POST,
            "/system/dry-run/enable",
            dry_run_enable,
            PermissionLevel.ADMIN,
        ),
        AdminRoute(
            HttpMethod.POST,
            "/system/dry-run/disable",
            dry_run_disable,
            PermissionLevel.ADMIN,
        ),
    )
    for route in routes:
        registry.register(route)
