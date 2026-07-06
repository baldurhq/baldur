"""Core admin routes — K8s probes, dashboard, audit health, bulkhead status.

Always-loaded base routes. Failure to import these handlers logs a WARNING
because they cover the operator-facing health surface (admin server starts
with a reduced route set).
"""

from __future__ import annotations

import structlog

from baldur.api.admin.registry import AdminRegistry, AdminRoute
from baldur.interfaces.web_framework import HttpMethod, PermissionLevel

logger = structlog.get_logger()


def _register_core_routes(registry: AdminRegistry) -> None:
    """Wire the always-loaded base handlers (health/dashboard/audit/bulkhead).

    Extraction source: ``api/handlers/{health,dashboard,audit,bulkhead}.py``.
    Paths mirror the Django URL conf so CLI / admin / Django share a single
    URL contract.
    """
    try:
        from baldur.api.handlers.audit import (
            audit_health,
            circuit_breaker_status,
        )
        from baldur.api.handlers.bulkhead import bulkhead_status
        from baldur.api.handlers.dashboard import dashboard_summary
        from baldur.api.handlers.health import (
            health_check,
            liveness_check,
            readiness_check,
        )
        from baldur.api.handlers.throttle import throttle_status
    except Exception as exc:
        # Core routes are the operator-facing health surface (K8s probes live
        # here). Silent drop would hide that /liveness, /readiness, /health
        # are missing until the operator notices in production — WARN.
        logger.warning(
            "admin.core_handlers_unavailable",
            error=exc,
            hint="admin server will start with reduced route set",
        )
        return

    routes: tuple[AdminRoute, ...] = (
        AdminRoute(HttpMethod.GET, "/liveness", liveness_check, PermissionLevel.PUBLIC),
        AdminRoute(
            HttpMethod.GET, "/readiness", readiness_check, PermissionLevel.PUBLIC
        ),
        AdminRoute(HttpMethod.GET, "/health", health_check, PermissionLevel.VIEWER),
        AdminRoute(
            HttpMethod.GET,
            "/dashboard/summary",
            dashboard_summary,
            PermissionLevel.VIEWER,
        ),
        AdminRoute(
            HttpMethod.GET, "/audit/health", audit_health, PermissionLevel.VIEWER
        ),
        AdminRoute(
            HttpMethod.GET,
            "/audit/circuit-breakers",
            circuit_breaker_status,
            PermissionLevel.VIEWER,
        ),
        AdminRoute(
            HttpMethod.GET,
            "/audit/circuit-breakers/{name}",
            circuit_breaker_status,
            PermissionLevel.VIEWER,
        ),
        AdminRoute(
            HttpMethod.GET,
            "/bulkheads",
            bulkhead_status,
            PermissionLevel.VIEWER,
        ),
        AdminRoute(
            HttpMethod.GET,
            "/throttle/status",
            throttle_status,
            PermissionLevel.VIEWER,
        ),
    )

    for route in routes:
        registry.register(route)
