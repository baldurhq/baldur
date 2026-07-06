"""Error-budget admin routes (status, deployment gating, reconciliation)."""

from __future__ import annotations

from baldur.api.admin.registry import AdminRegistry, AdminRoute
from baldur.api.admin.routes._import_policy import handle_route_import_failure
from baldur.interfaces.web_framework import HttpMethod, PermissionLevel


def _register_error_budget_routes(registry: AdminRegistry) -> None:
    # error_budget_status + deployment endpoints removed — the handler-module
    # surface (budget_status / deployment_verdict / ...) no longer matches the
    # pre-existing route layer.  Tracked in OOS_INDEX as v1.1 admin surface
    # expansion candidate.

    # --- reconciliation (imports OK) ---
    try:
        from baldur.api.handlers.error_budget_reconciliation import (
            reconciliation_config_get,
            reconciliation_config_update,
            reconciliation_excluded_period_delete,
            reconciliation_excluded_periods_create,
            reconciliation_excluded_periods_list,
            reconciliation_failsafe_periods,
            reconciliation_shadow_budget_approve,
            reconciliation_shadow_budget_detail,
            reconciliation_shadow_budget_reject,
            reconciliation_shadow_budgets_calculate,
            reconciliation_shadow_budgets_list,
            reconciliation_status,
        )
    except Exception as exc:
        handle_route_import_failure("admin.reconciliation_routes_unavailable", exc)
        return

    for route in (
        AdminRoute(
            HttpMethod.GET,
            "/reconciliation/status",
            reconciliation_status,
            PermissionLevel.VIEWER,
        ),
        AdminRoute(
            HttpMethod.GET,
            "/reconciliation/failsafe-periods",
            reconciliation_failsafe_periods,
            PermissionLevel.VIEWER,
        ),
        AdminRoute(
            HttpMethod.GET,
            "/reconciliation/shadow-budgets",
            reconciliation_shadow_budgets_list,
            PermissionLevel.VIEWER,
        ),
        AdminRoute(
            HttpMethod.POST,
            "/reconciliation/shadow-budgets",
            reconciliation_shadow_budgets_calculate,
            PermissionLevel.OPERATOR,
        ),
        AdminRoute(
            HttpMethod.GET,
            "/reconciliation/shadow-budgets/{calculation_id}",
            reconciliation_shadow_budget_detail,
            PermissionLevel.VIEWER,
        ),
        AdminRoute(
            HttpMethod.POST,
            "/reconciliation/shadow-budgets/{calculation_id}/approve",
            reconciliation_shadow_budget_approve,
            PermissionLevel.ADMIN,
        ),
        AdminRoute(
            HttpMethod.POST,
            "/reconciliation/shadow-budgets/{calculation_id}/reject",
            reconciliation_shadow_budget_reject,
            PermissionLevel.ADMIN,
        ),
        AdminRoute(
            HttpMethod.GET,
            "/reconciliation/excluded-periods",
            reconciliation_excluded_periods_list,
            PermissionLevel.VIEWER,
        ),
        AdminRoute(
            HttpMethod.POST,
            "/reconciliation/excluded-periods",
            reconciliation_excluded_periods_create,
            PermissionLevel.OPERATOR,
        ),
        AdminRoute(
            HttpMethod.DELETE,
            "/reconciliation/excluded-periods/{exclusion_id}",
            reconciliation_excluded_period_delete,
            PermissionLevel.ADMIN,
        ),
        AdminRoute(
            HttpMethod.GET,
            "/reconciliation/config",
            reconciliation_config_get,
            PermissionLevel.ADMIN,
        ),
        AdminRoute(
            HttpMethod.PUT,
            "/reconciliation/config",
            reconciliation_config_update,
            PermissionLevel.ADMIN,
        ),
    ):
        registry.register(route)
