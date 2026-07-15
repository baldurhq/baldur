"""Dead-letter queue admin routes.

Split into two tiers: the OSS-set (read + single-entry actions — list / detail /
facets / cleanup stats / retry / resolve / force-redrive) always registers; the
PRO-set (batch replay + archive/purge/test-create management) registers only when
the ``baldur_pro`` package is installed. In a pure-OSS deployment the four PRO-set
routes are absent, so a request to them resolves to 404 (invisible) rather than
reaching the handler-layer ``RuntimeError`` (HTTP 500) — matching the console's
per-action gating of those same operations.
"""

from __future__ import annotations

import importlib.util

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

    # OSS-set: read + single-entry actions. Always registered (backed by the
    # OSS DLQReadService when PRO is absent).
    oss_routes = (
        AdminRoute(
            HttpMethod.GET,
            "/dlq/cleanup/stats",
            dlq_cleanup_stats,
            PermissionLevel.VIEWER,
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
    )
    for route in oss_routes:
        registry.register(route)

    # PRO-set: batch replay + management lifecycle. Registered only when the PRO
    # package is installed — a static, import-ordering-independent presence probe
    # (does not import a PRO symbol). Pure OSS → these four routes are absent → a
    # request resolves to 404 rather than the handler-layer RuntimeError (500).
    if importlib.util.find_spec("baldur_pro") is None:
        return

    pro_routes = (
        AdminRoute(
            HttpMethod.POST, "/dlq/replay", dlq_replay, PermissionLevel.OPERATOR
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
        AdminRoute(
            HttpMethod.POST,
            "/dlq/test/create",
            dlq_test_create,
            PermissionLevel.ADMIN,
        ),
    )
    for route in pro_routes:
        registry.register(route)
