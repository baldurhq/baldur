"""Audit-pipeline resilience admin routes (CB ops, degraded mode, metrics)."""

from __future__ import annotations

import structlog

from baldur.api.admin.registry import AdminRegistry, AdminRoute
from baldur.interfaces.web_framework import HttpMethod, PermissionLevel

logger = structlog.get_logger()


def _register_audit_resilience_routes(registry: AdminRegistry) -> None:
    try:
        from baldur.api.handlers.audit_resilience import (
            audit_metrics,
            circuit_breaker_force_open,
            circuit_breaker_reset,
            circuit_breaker_reset_all,
            degraded_mode_force,
            degraded_mode_status,
            metrics_reset,
        )
    except Exception as exc:
        logger.debug("admin.audit_resilience_routes_unavailable", error=exc)
        return

    for route in (
        AdminRoute(
            HttpMethod.POST,
            "/resilience/cb/reset/{name}",
            circuit_breaker_reset,
            PermissionLevel.ADMIN,
        ),
        AdminRoute(
            HttpMethod.POST,
            "/resilience/cb/force-open/{name}",
            circuit_breaker_force_open,
            PermissionLevel.ADMIN,
        ),
        AdminRoute(
            HttpMethod.POST,
            "/resilience/cb/reset-all",
            circuit_breaker_reset_all,
            PermissionLevel.ADMIN,
        ),
        AdminRoute(
            HttpMethod.GET,
            "/resilience/audit-metrics",
            audit_metrics,
            PermissionLevel.VIEWER,
        ),
        AdminRoute(
            HttpMethod.GET,
            "/resilience/degraded-mode",
            degraded_mode_status,
            PermissionLevel.VIEWER,
        ),
        AdminRoute(
            HttpMethod.POST,
            "/resilience/degraded-mode/{action}",
            degraded_mode_force,
            PermissionLevel.ADMIN,
        ),
        AdminRoute(
            HttpMethod.POST,
            "/resilience/metrics/reset",
            metrics_reset,
            PermissionLevel.OPERATOR,
        ),
    ):
        registry.register(route)
