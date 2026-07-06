"""Security review admin route."""

from __future__ import annotations

import structlog

from baldur.api.admin.registry import AdminRegistry, AdminRoute
from baldur.interfaces.web_framework import HttpMethod, PermissionLevel

logger = structlog.get_logger()


def _register_security_review_routes(registry: AdminRegistry) -> None:
    try:
        from baldur.api.handlers.security_review import security_review_run

        registry.register(
            AdminRoute(
                HttpMethod.GET,
                "/security-review",
                security_review_run,
                PermissionLevel.ADMIN,
            )
        )
    except Exception:
        logger.debug("admin.security_review_routes_skipped")
