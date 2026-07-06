"""L2 storage admin routes (status, sync, drift, config, shadow log)."""

from __future__ import annotations

import structlog

from baldur.api.admin.registry import AdminRegistry, AdminRoute
from baldur.interfaces.web_framework import HttpMethod, PermissionLevel

logger = structlog.get_logger()


def _register_l2_storage_routes(registry: AdminRegistry) -> None:
    try:
        from baldur.api.handlers.l2_storage import (
            drift_reconciliation_history,
            drift_reconciliation_service,
            drift_reconciliation_stats,
            drift_reconciliation_trigger,
            l2_storage_config_get,
            l2_storage_config_reset,
            l2_storage_config_update,
            l2_storage_health,
            l2_storage_health_reset,
            l2_storage_metrics,
            l2_storage_status,
            l2_storage_sync_from_l2,
            l2_storage_sync_to_l2,
        )
        from baldur.api.handlers.l2_storage_shadow_log import (
            shadow_log_analyze,
            shadow_log_by_service,
            shadow_log_clear,
            shadow_log_list,
            shadow_log_replay,
            shadow_log_stats,
        )
    except Exception as exc:
        logger.debug("admin.l2_storage_routes_unavailable", error=exc)
        return

    for route in (
        AdminRoute(
            HttpMethod.GET,
            "/l2-storage/status",
            l2_storage_status,
            PermissionLevel.VIEWER,
        ),
        AdminRoute(
            HttpMethod.GET,
            "/l2-storage/health",
            l2_storage_health,
            PermissionLevel.VIEWER,
        ),
        AdminRoute(
            HttpMethod.POST,
            "/l2-storage/health/reset",
            l2_storage_health_reset,
            PermissionLevel.ADMIN,
        ),
        AdminRoute(
            HttpMethod.POST,
            "/l2-storage/sync/from-l2",
            l2_storage_sync_from_l2,
            PermissionLevel.ADMIN,
        ),
        AdminRoute(
            HttpMethod.POST,
            "/l2-storage/sync/to-l2",
            l2_storage_sync_to_l2,
            PermissionLevel.ADMIN,
        ),
        AdminRoute(
            HttpMethod.GET,
            "/l2-storage/metrics",
            l2_storage_metrics,
            PermissionLevel.VIEWER,
        ),
        AdminRoute(
            HttpMethod.GET,
            "/l2-storage/drift/stats",
            drift_reconciliation_stats,
            PermissionLevel.VIEWER,
        ),
        AdminRoute(
            HttpMethod.GET,
            "/l2-storage/drift/history",
            drift_reconciliation_history,
            PermissionLevel.VIEWER,
        ),
        AdminRoute(
            HttpMethod.POST,
            "/l2-storage/drift/reconcile",
            drift_reconciliation_trigger,
            PermissionLevel.ADMIN,
        ),
        AdminRoute(
            HttpMethod.POST,
            "/l2-storage/drift/reconcile/{service_name}",
            drift_reconciliation_service,
            PermissionLevel.ADMIN,
        ),
        AdminRoute(
            HttpMethod.GET,
            "/l2-storage/config",
            l2_storage_config_get,
            PermissionLevel.VIEWER,
        ),
        AdminRoute(
            HttpMethod.PUT,
            "/l2-storage/config",
            l2_storage_config_update,
            PermissionLevel.ADMIN,
        ),
        AdminRoute(
            HttpMethod.POST,
            "/l2-storage/config/reset",
            l2_storage_config_reset,
            PermissionLevel.ADMIN,
        ),
        AdminRoute(
            HttpMethod.GET,
            "/l2-storage/shadow-log",
            shadow_log_list,
            PermissionLevel.VIEWER,
        ),
        AdminRoute(
            HttpMethod.GET,
            "/l2-storage/shadow-log/stats",
            shadow_log_stats,
            PermissionLevel.VIEWER,
        ),
        AdminRoute(
            HttpMethod.POST,
            "/l2-storage/shadow-log/clear",
            shadow_log_clear,
            PermissionLevel.ADMIN,
        ),
        AdminRoute(
            HttpMethod.GET,
            "/l2-storage/shadow-log/analyze",
            shadow_log_analyze,
            PermissionLevel.VIEWER,
        ),
        AdminRoute(
            HttpMethod.POST,
            "/l2-storage/shadow-log/replay",
            shadow_log_replay,
            PermissionLevel.OPERATOR,
        ),
        AdminRoute(
            HttpMethod.GET,
            "/l2-storage/shadow-log/service/{service_name}",
            shadow_log_by_service,
            PermissionLevel.VIEWER,
        ),
    ):
        registry.register(route)
