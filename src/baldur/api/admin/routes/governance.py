"""Governance admin routes (status, config, control, approvals, escalation)."""

from __future__ import annotations

from baldur.api.admin.registry import AdminRegistry, AdminRoute
from baldur.api.admin.routes._import_policy import handle_route_import_failure
from baldur.interfaces.web_framework import HttpMethod, PermissionLevel


def _register_governance_routes(registry: AdminRegistry) -> None:
    # Removed endpoints (no current handler — deferred to a later admin
    # surface expansion): GET /governance/status, GET
    # /governance/status/{service_name}, GET /governance/status/channels,
    # GET /governance/approvals/{approval_id}, POST
    # /governance/approvals/{approval_id}, POST
    # /governance/emergency-escalation.
    try:
        from baldur.api.handlers.governance import (
            approval_request_list,
            governance_config_get,
            governance_config_update,
            governance_rbac_status,
        )
    except Exception as exc:
        handle_route_import_failure("admin.governance_routes_unavailable", exc)
        return

    for route in (
        AdminRoute(
            HttpMethod.GET,
            "/governance/config",
            governance_config_get,
            PermissionLevel.VIEWER,
        ),
        AdminRoute(
            HttpMethod.PUT,
            "/governance/config",
            governance_config_update,
            PermissionLevel.ADMIN,
        ),
        AdminRoute(
            HttpMethod.GET,
            "/governance/control",
            governance_rbac_status,
            PermissionLevel.VIEWER,
        ),
        AdminRoute(
            HttpMethod.PATCH,
            "/governance/control",
            governance_config_update,
            PermissionLevel.ADMIN,
        ),
        AdminRoute(
            HttpMethod.GET,
            "/governance/approvals",
            approval_request_list,
            PermissionLevel.VIEWER,
        ),
    ):
        registry.register(route)
