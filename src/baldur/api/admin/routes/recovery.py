"""Recovery + canary + rollback admin routes."""

from __future__ import annotations

from baldur.api.admin.registry import AdminRegistry, AdminRoute
from baldur.api.admin.routes._import_policy import handle_route_import_failure
from baldur.interfaces.web_framework import HttpMethod, PermissionLevel


def _register_recovery_routes(registry: AdminRegistry) -> None:
    # --- recovery ---
    # Removed endpoints (no current handler — deferred to a later admin
    # surface expansion): /recovery/plan, /recovery/execute/{plan_id},
    # /recovery/approvals/{approval_id} (GET detail + POST submit),
    # /recovery/regional-policy.
    try:
        from baldur.api.handlers.recovery import (
            recovery_dashboard_widget,
            recovery_pending_approvals,
            recovery_status,
        )
    except Exception as exc:
        handle_route_import_failure("admin.recovery_proper_routes_unavailable", exc)
    else:
        for route in (
            AdminRoute(
                HttpMethod.GET,
                "/recovery/status",
                recovery_status,
                PermissionLevel.VIEWER,
            ),
            AdminRoute(
                HttpMethod.GET,
                "/recovery/dashboard",
                recovery_dashboard_widget,
                PermissionLevel.VIEWER,
            ),
            AdminRoute(
                HttpMethod.GET,
                "/recovery/approvals",
                recovery_pending_approvals,
                PermissionLevel.VIEWER,
            ),
        ):
            registry.register(route)

    # --- canary (imports OK) ---
    try:
        from baldur.api.handlers.canary import (
            canary_panic_rollback,
            canary_rollout_action,
            canary_rollout_create,
            canary_rollout_detail,
            canary_rollout_history,
            canary_rollout_list,
            canary_rollout_metrics,
        )
    except Exception as exc:
        handle_route_import_failure("admin.canary_routes_unavailable", exc)
        return

    for route in (
        AdminRoute(
            HttpMethod.GET,
            "/canary/rollouts",
            canary_rollout_list,
            PermissionLevel.VIEWER,
        ),
        AdminRoute(
            HttpMethod.POST,
            "/canary/rollouts",
            canary_rollout_create,
            PermissionLevel.ADMIN,
        ),
        AdminRoute(
            HttpMethod.GET,
            "/canary/rollouts/{rollout_id}",
            canary_rollout_detail,
            PermissionLevel.VIEWER,
        ),
        AdminRoute(
            HttpMethod.POST,
            "/canary/rollouts/{rollout_id}/{action}",
            canary_rollout_action,
            PermissionLevel.ADMIN,
        ),
        AdminRoute(
            HttpMethod.GET,
            "/canary/rollouts/{rollout_id}/metrics",
            canary_rollout_metrics,
            PermissionLevel.VIEWER,
        ),
        AdminRoute(
            HttpMethod.POST,
            "/canary/panic-rollback",
            canary_panic_rollback,
            PermissionLevel.ADMIN,
        ),
        AdminRoute(
            HttpMethod.GET,
            "/canary/history",
            canary_rollout_history,
            PermissionLevel.VIEWER,
        ),
    ):
        registry.register(route)
