"""Chaos engineering admin routes (config, reports, safety, schedules)."""

from __future__ import annotations

import structlog

from baldur.api.admin.registry import AdminRegistry, AdminRoute
from baldur.interfaces.web_framework import HttpMethod, PermissionLevel

logger = structlog.get_logger()


def _register_chaos_routes(registry: AdminRegistry) -> None:
    try:
        from baldur.api.handlers.chaos_config import (
            chaos_blast_radius_policy_get,
            chaos_blast_radius_policy_update,
            report_config_get,
            report_config_update,
            safety_guard_config_get,
            safety_guard_config_update,
            scheduler_config_get,
            scheduler_config_update,
        )
        from baldur.api.handlers.chaos_report import (
            chaos_dry_run_analysis,
            chaos_grade_history,
            chaos_report_detail,
            chaos_report_generate,
            chaos_report_list,
        )
        from baldur.api.handlers.chaos_safety import (
            dry_run_config_get,
            dry_run_config_update,
            kill_all,
            kill_switch_action,
            kill_switch_status,
            safety_check,
            stop_conditions_config_get,
            stop_conditions_config_update,
            ttl_config_get,
            ttl_config_update,
        )
        from baldur.api.handlers.chaos_schedule import (
            chaos_pending_approvals,
            chaos_schedule_approval,
            chaos_schedule_create,
            chaos_schedule_delete,
            chaos_schedule_detail,
            chaos_schedule_execute,
            chaos_schedule_list,
            chaos_schedule_update,
        )
    except Exception as exc:
        logger.debug("admin.chaos_routes_unavailable", error=exc)
        return

    for route in (
        AdminRoute(
            HttpMethod.GET,
            "/chaos/config/safety-guard",
            safety_guard_config_get,
            PermissionLevel.VIEWER,
        ),
        AdminRoute(
            HttpMethod.PATCH,
            "/chaos/config/safety-guard",
            safety_guard_config_update,
            PermissionLevel.ADMIN,
        ),
        AdminRoute(
            HttpMethod.GET,
            "/chaos/config/blast-radius",
            chaos_blast_radius_policy_get,
            PermissionLevel.VIEWER,
        ),
        AdminRoute(
            HttpMethod.PATCH,
            "/chaos/config/blast-radius",
            chaos_blast_radius_policy_update,
            PermissionLevel.ADMIN,
        ),
        AdminRoute(
            HttpMethod.GET,
            "/chaos/config/scheduler",
            scheduler_config_get,
            PermissionLevel.VIEWER,
        ),
        AdminRoute(
            HttpMethod.PATCH,
            "/chaos/config/scheduler",
            scheduler_config_update,
            PermissionLevel.ADMIN,
        ),
        AdminRoute(
            HttpMethod.GET,
            "/chaos/config/report",
            report_config_get,
            PermissionLevel.VIEWER,
        ),
        AdminRoute(
            HttpMethod.PATCH,
            "/chaos/config/report",
            report_config_update,
            PermissionLevel.ADMIN,
        ),
        AdminRoute(
            HttpMethod.GET, "/chaos/reports", chaos_report_list, PermissionLevel.VIEWER
        ),
        AdminRoute(
            HttpMethod.GET,
            "/chaos/reports/{report_id}",
            chaos_report_detail,
            PermissionLevel.VIEWER,
        ),
        AdminRoute(
            HttpMethod.POST,
            "/chaos/reports/generate",
            chaos_report_generate,
            PermissionLevel.OPERATOR,
        ),
        AdminRoute(
            HttpMethod.GET,
            "/chaos/grade-history",
            chaos_grade_history,
            PermissionLevel.VIEWER,
        ),
        AdminRoute(
            HttpMethod.POST,
            "/chaos/dry-run/analysis",
            chaos_dry_run_analysis,
            PermissionLevel.VIEWER,
        ),
        AdminRoute(
            HttpMethod.GET,
            "/chaos/kill-switch",
            kill_switch_status,
            PermissionLevel.VIEWER,
        ),
        AdminRoute(
            HttpMethod.POST,
            "/chaos/kill-switch",
            kill_switch_action,
            PermissionLevel.ADMIN,
        ),
        AdminRoute(
            HttpMethod.POST,
            "/chaos/safety-check",
            safety_check,
            PermissionLevel.VIEWER,
        ),
        AdminRoute(
            HttpMethod.GET,
            "/chaos/stop-conditions",
            stop_conditions_config_get,
            PermissionLevel.VIEWER,
        ),
        AdminRoute(
            HttpMethod.PUT,
            "/chaos/stop-conditions",
            stop_conditions_config_update,
            PermissionLevel.ADMIN,
        ),
        AdminRoute(
            HttpMethod.GET,
            "/chaos/ttl-config",
            ttl_config_get,
            PermissionLevel.VIEWER,
        ),
        AdminRoute(
            HttpMethod.PUT,
            "/chaos/ttl-config",
            ttl_config_update,
            PermissionLevel.ADMIN,
        ),
        AdminRoute(
            HttpMethod.GET,
            "/chaos/dry-run-config",
            dry_run_config_get,
            PermissionLevel.VIEWER,
        ),
        AdminRoute(
            HttpMethod.PUT,
            "/chaos/dry-run-config",
            dry_run_config_update,
            PermissionLevel.ADMIN,
        ),
        AdminRoute(HttpMethod.POST, "/chaos/kill-all", kill_all, PermissionLevel.ADMIN),
        AdminRoute(
            HttpMethod.GET,
            "/chaos/schedules",
            chaos_schedule_list,
            PermissionLevel.VIEWER,
        ),
        AdminRoute(
            HttpMethod.POST,
            "/chaos/schedules",
            chaos_schedule_create,
            PermissionLevel.ADMIN,
        ),
        AdminRoute(
            HttpMethod.GET,
            "/chaos/schedules/{schedule_id}",
            chaos_schedule_detail,
            PermissionLevel.VIEWER,
        ),
        AdminRoute(
            HttpMethod.PATCH,
            "/chaos/schedules/{schedule_id}",
            chaos_schedule_update,
            PermissionLevel.ADMIN,
        ),
        AdminRoute(
            HttpMethod.DELETE,
            "/chaos/schedules/{schedule_id}",
            chaos_schedule_delete,
            PermissionLevel.ADMIN,
        ),
        AdminRoute(
            HttpMethod.POST,
            "/chaos/schedules/{schedule_id}/approval",
            chaos_schedule_approval,
            PermissionLevel.ADMIN,
        ),
        AdminRoute(
            HttpMethod.POST,
            "/chaos/schedules/{schedule_id}/execute",
            chaos_schedule_execute,
            PermissionLevel.ADMIN,
        ),
        AdminRoute(
            HttpMethod.GET,
            "/chaos/pending-approvals",
            chaos_pending_approvals,
            PermissionLevel.VIEWER,
        ),
    ):
        registry.register(route)
