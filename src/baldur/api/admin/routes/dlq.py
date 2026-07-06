"""Dead-letter queue admin routes (replay / cleanup / list / detail / retry)."""

from __future__ import annotations

import structlog

from baldur.api.admin.registry import AdminRegistry, AdminRoute
from baldur.interfaces.web_framework import HttpMethod, PermissionLevel

logger = structlog.get_logger()


def _register_dlq_routes(registry: AdminRegistry) -> None:
    try:
        from baldur.api.handlers.dlq import (
            dlq_cleanup_archive,
            dlq_cleanup_purge,
            dlq_cleanup_stats,
            dlq_detail,
            dlq_facets,
            dlq_force_redrive,
            dlq_list,
            dlq_replay,
            dlq_resolve,
            dlq_retry,
            dlq_test_create,
        )
    except Exception as exc:
        logger.debug("admin.dlq_routes_unavailable", error=exc)
        return

    routes = (
        AdminRoute(
            HttpMethod.POST, "/dlq/replay", dlq_replay, PermissionLevel.OPERATOR
        ),
        AdminRoute(
            HttpMethod.GET,
            "/dlq/cleanup/stats",
            dlq_cleanup_stats,
            PermissionLevel.VIEWER,
        ),
        AdminRoute(
            HttpMethod.POST,
            "/dlq/cleanup/archive",
            dlq_cleanup_archive,
            PermissionLevel.OPERATOR,
        ),
        AdminRoute(
            HttpMethod.POST,
            "/dlq/cleanup/purge",
            dlq_cleanup_purge,
            PermissionLevel.ADMIN,
        ),
        AdminRoute(HttpMethod.GET, "/dlq/list", dlq_list, PermissionLevel.VIEWER),
        # `/dlq/facets` MUST be registered before `/dlq/{pk}` — AdminRegistry.resolve
        # returns the first matching route and `/dlq/{pk}` compiles to
        # `^/dlq/([^/]+)$`, which matches the single-segment `/dlq/facets`.
        # Registered after, the request would resolve to dlq_detail with
        # pk="facets" (542 D1; precedent: `/dlq/list`).
        AdminRoute(HttpMethod.GET, "/dlq/facets", dlq_facets, PermissionLevel.VIEWER),
        AdminRoute(HttpMethod.GET, "/dlq/{pk}", dlq_detail, PermissionLevel.VIEWER),
        AdminRoute(
            HttpMethod.POST, "/dlq/{pk}/retry", dlq_retry, PermissionLevel.OPERATOR
        ),
        AdminRoute(
            HttpMethod.POST,
            "/dlq/{pk}/resolve",
            dlq_resolve,
            PermissionLevel.OPERATOR,
        ),
        # Force-redrive is a privileged cap-override — bound at ADMIN (strictly
        # above the OPERATOR normal retry), mirroring the destructive-purge
        # precedent. `/dlq/{pk}/force-redrive` is two-segment, so it does not
        # collide with the single-segment `/dlq/{pk}` (`^/dlq/([^/]+)$`).
        AdminRoute(
            HttpMethod.POST,
            "/dlq/{pk}/force-redrive",
            dlq_force_redrive,
            PermissionLevel.ADMIN,
        ),
        AdminRoute(
            HttpMethod.POST,
            "/dlq/test/create",
            dlq_test_create,
            PermissionLevel.ADMIN,
        ),
    )
    for route in routes:
        registry.register(route)
