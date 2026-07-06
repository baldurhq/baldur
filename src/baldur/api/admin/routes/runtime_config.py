"""Runtime configuration admin routes (per-section GET/PUT, logging, SLO)."""

from __future__ import annotations

import structlog

from baldur.api.admin.registry import AdminRegistry, AdminRoute
from baldur.interfaces.web_framework import HttpMethod, PermissionLevel

logger = structlog.get_logger()


def _register_config_routes(registry: AdminRegistry) -> None:
    try:
        from functools import partial

        from baldur.api.handlers.config import (
            all_config_get,
            cancel_pending_change,
            config_get,
            config_reset,
            config_update,
            editable_config_get,
            logging_config_update,
            pending_changes_get,
            slo_config_delete,
            slo_config_update,
        )
    except Exception as exc:
        logger.debug("admin.runtime_config_routes_unavailable", error=exc)
        return

    routes: list[AdminRoute] = [
        AdminRoute(HttpMethod.GET, "/config", all_config_get, PermissionLevel.VIEWER),
        # Console Runtime Config editor projection (662 D6). Literal path —
        # "editable" is not a config section slug, so no per-section collision.
        AdminRoute(
            HttpMethod.GET,
            "/config/editable",
            editable_config_get,
            PermissionLevel.VIEWER,
        ),
        AdminRoute(
            HttpMethod.POST, "/config/reset", config_reset, PermissionLevel.ADMIN
        ),
        AdminRoute(
            HttpMethod.GET,
            "/config/pending",
            pending_changes_get,
            PermissionLevel.VIEWER,
        ),
        AdminRoute(
            HttpMethod.POST,
            "/config/pending/{pending_id}/cancel",
            cancel_pending_change,
            PermissionLevel.ADMIN,
        ),
    ]

    # Per-section routes (GET viewer+, PUT admin)
    sections = (
        ("circuit-breaker", "circuit_breaker"),
        ("dlq", "dlq"),
        ("retry", "retry"),
        ("sla", "sla"),
        ("rate-limit", "rate_limit"),
        ("security", "security"),
        ("idempotency", "idempotency"),
        ("notification", "notification"),
        ("forensic", "forensic"),
        ("metrics", "metrics"),
        ("error-budget", "error_budget"),
        ("replay-automation", "replay_automation"),
    )
    for url_slug, config_name in sections:
        routes.append(
            AdminRoute(
                HttpMethod.GET,
                f"/config/{url_slug}",
                partial(config_get, config_name=config_name),
                PermissionLevel.VIEWER,
            )
        )
        routes.append(
            AdminRoute(
                HttpMethod.PUT,
                f"/config/{url_slug}",
                partial(config_update, config_name=config_name),
                PermissionLevel.ADMIN,
            )
        )

    # Logging section — custom PUT applies runtime logger hot reload
    routes.append(
        AdminRoute(
            HttpMethod.GET,
            "/config/logging",
            partial(config_get, config_name="logging"),
            PermissionLevel.VIEWER,
        )
    )
    routes.append(
        AdminRoute(
            HttpMethod.PUT,
            "/config/logging",
            logging_config_update,
            PermissionLevel.ADMIN,
        )
    )

    # SLO section — GET via generic, PUT/DELETE custom
    routes.append(
        AdminRoute(
            HttpMethod.GET,
            "/config/slo",
            partial(config_get, config_name="slo"),
            PermissionLevel.VIEWER,
        )
    )
    routes.append(
        AdminRoute(
            HttpMethod.PUT, "/config/slo", slo_config_update, PermissionLevel.ADMIN
        )
    )
    routes.append(
        AdminRoute(
            HttpMethod.DELETE, "/config/slo", slo_config_delete, PermissionLevel.ADMIN
        )
    )

    for route in routes:
        registry.register(route)
