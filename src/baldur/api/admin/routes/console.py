"""Web console route — serves the built-in operator UI at ``GET /`` (536).

Single PUBLIC route (D5): the HTML shell carries no sensitive data and a
browser top-level navigation cannot send the ``X-Baldur-Admin-Key`` header, so
the page must load without auth. The data/control ``fetch()`` calls the JS
makes carry the key and enforce their own VIEWER/OPERATOR/ADMIN levels through
the unchanged auth path. The handler itself returns 404 when the console is
disabled (D4).
"""

from __future__ import annotations

import structlog

from baldur.api.admin.registry import AdminRegistry, AdminRoute
from baldur.interfaces.web_framework import HttpMethod, PermissionLevel

logger = structlog.get_logger()

__all__ = ["_register_console_routes"]


def _register_console_routes(registry: AdminRegistry) -> None:
    """Wire ``GET /`` to the web console handler.

    The route is always registered; the handler returns 404 at runtime when
    ``console_enabled`` is false, so the console is runtime-toggleable without
    re-wiring routes.
    """
    try:
        from baldur.api.admin.console.handler import console_page
    except Exception as exc:  # noqa: BLE001
        # The console is a convenience surface, not the operator-critical health
        # surface — a missing asset/import must not block the JSON API. DEBUG
        # (not WARNING) because JSON endpoints keep serving fully.
        logger.debug("admin.console_routes_unavailable", error=exc)
        return

    registry.register(
        AdminRoute(HttpMethod.GET, "/", console_page, PermissionLevel.PUBLIC)
    )
