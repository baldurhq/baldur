"""Tiering + config-history + finops + dlq-compressed admin routes."""

from __future__ import annotations

from baldur.api.admin.registry import AdminRegistry, AdminRoute
from baldur.api.admin.routes._import_policy import handle_route_import_failure
from baldur.interfaces.web_framework import HttpMethod, PermissionLevel


def _register_config_data_routes(registry: AdminRegistry) -> None:
    # --- tiering ---
    # Removed endpoints (no current handler — tracked in OOS_INDEX for v1.1
    # admin surface expansion): GET /tiering, GET /tiering/defaults, DELETE
    # /tiering/mapping/{service_name}.
    try:
        from baldur.api.handlers.tiering import (
            tier_definitions_get,
            tier_definitions_update,
            tier_dry_run,
            tier_export,
            tier_import,
            tier_mappings_get,
            tier_mappings_update,
            tier_overrides_update,
        )
    except Exception as exc:
        handle_route_import_failure("admin.tiering_routes_unavailable", exc)
    else:
        for route in (
            AdminRoute(
                HttpMethod.GET,
                "/tiering/config",
                tier_definitions_get,
                PermissionLevel.VIEWER,
            ),
            AdminRoute(
                HttpMethod.PUT,
                "/tiering/config",
                tier_definitions_update,
                PermissionLevel.ADMIN,
            ),
            AdminRoute(
                HttpMethod.GET,
                "/tiering/mapping/{service_name}",
                tier_mappings_get,
                PermissionLevel.VIEWER,
            ),
            AdminRoute(
                HttpMethod.POST,
                "/tiering/mapping/{service_name}",
                tier_mappings_update,
                PermissionLevel.ADMIN,
            ),
            AdminRoute(
                HttpMethod.POST,
                "/tiering/override/{service_name}",
                tier_overrides_update,
                PermissionLevel.ADMIN,
            ),
            AdminRoute(
                HttpMethod.POST,
                "/tiering/dry-run",
                tier_dry_run,
                PermissionLevel.VIEWER,
            ),
            AdminRoute(
                HttpMethod.GET,
                "/tiering/export",
                tier_export,
                PermissionLevel.VIEWER,
            ),
            AdminRoute(
                HttpMethod.POST,
                "/tiering/import",
                tier_import,
                PermissionLevel.ADMIN,
            ),
        ):
            registry.register(route)

    # --- config_history + finops + dlq_compressed (imports OK) ---
    try:
        from baldur.api.handlers.config_history import (
            config_compare,
            config_history_list,
            config_rollback,
            config_version_detail,
        )
        from baldur.api.handlers.dlq_compressed import (
            dlq_compressed_detail,
            dlq_compressed_list,
            dlq_compressed_summary,
        )
        from baldur.api.handlers.finops import (
            finops_alert_acknowledge,
            finops_alerts_list,
            finops_budget_get,
            finops_budget_reset,
            finops_budget_set,
            finops_cost_record,
            finops_report,
        )
    except Exception as exc:
        handle_route_import_failure(
            "admin.config_history_finops_routes_unavailable", exc
        )
        return

    for route in (
        AdminRoute(
            HttpMethod.GET,
            "/config-history/{config_type}/history",
            config_history_list,
            PermissionLevel.VIEWER,
        ),
        AdminRoute(
            HttpMethod.GET,
            "/config-history/{config_type}/history/{version}",
            config_version_detail,
            PermissionLevel.VIEWER,
        ),
        AdminRoute(
            HttpMethod.POST,
            "/config-history/{config_type}/rollback",
            config_rollback,
            PermissionLevel.ADMIN,
        ),
        AdminRoute(
            HttpMethod.GET,
            "/config-history/{config_type}/compare",
            config_compare,
            PermissionLevel.VIEWER,
        ),
        AdminRoute(
            HttpMethod.GET, "/finops/budget", finops_budget_get, PermissionLevel.VIEWER
        ),
        AdminRoute(
            HttpMethod.GET,
            "/finops/budget/{service_name}",
            finops_budget_get,
            PermissionLevel.VIEWER,
        ),
        AdminRoute(
            HttpMethod.POST,
            "/finops/budget/{service_name}",
            finops_budget_set,
            PermissionLevel.ADMIN,
        ),
        AdminRoute(
            HttpMethod.DELETE,
            "/finops/budget/{service_name}",
            finops_budget_reset,
            PermissionLevel.ADMIN,
        ),
        AdminRoute(
            HttpMethod.POST,
            "/finops/cost",
            finops_cost_record,
            PermissionLevel.OPERATOR,
        ),
        AdminRoute(
            HttpMethod.GET, "/finops/report", finops_report, PermissionLevel.VIEWER
        ),
        AdminRoute(
            HttpMethod.GET, "/finops/alerts", finops_alerts_list, PermissionLevel.VIEWER
        ),
        AdminRoute(
            HttpMethod.POST,
            "/finops/alerts/{alert_index}",
            finops_alert_acknowledge,
            PermissionLevel.OPERATOR,
        ),
        AdminRoute(
            HttpMethod.GET,
            "/dlq-compressed",
            dlq_compressed_list,
            PermissionLevel.VIEWER,
        ),
        AdminRoute(
            HttpMethod.GET,
            "/dlq-compressed/summary",
            dlq_compressed_summary,
            PermissionLevel.VIEWER,
        ),
        AdminRoute(
            HttpMethod.GET,
            "/dlq-compressed/{entry_id}",
            dlq_compressed_detail,
            PermissionLevel.VIEWER,
        ),
    ):
        registry.register(route)
