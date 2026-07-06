"""Daily report admin routes (list / trend / per-date detail)."""

from __future__ import annotations

import structlog

from baldur.api.admin.registry import AdminRegistry, AdminRoute
from baldur.interfaces.web_framework import HttpMethod, PermissionLevel

logger = structlog.get_logger()


def _register_daily_report_routes(registry: AdminRegistry) -> None:
    try:
        from baldur.api.handlers.daily_report import (
            daily_report_detail,
            daily_report_list,
            daily_report_trend,
        )
    except Exception as exc:
        logger.debug("admin.daily_report_routes_unavailable", error=exc)
        return

    routes = (
        AdminRoute(
            HttpMethod.GET,
            "/reports/daily",
            daily_report_list,
            PermissionLevel.VIEWER,
        ),
        AdminRoute(
            HttpMethod.GET,
            "/reports/daily/trend",
            daily_report_trend,
            PermissionLevel.VIEWER,
        ),
        AdminRoute(
            HttpMethod.GET,
            "/reports/daily/{date}",
            daily_report_detail,
            PermissionLevel.VIEWER,
        ),
    )
    for route in routes:
        registry.register(route)
