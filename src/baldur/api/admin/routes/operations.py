"""Operations admin routes (auto-tuning, meta-watchdog, metric-sync,
drift-threshold, grafana webhook)."""

from __future__ import annotations

import structlog

from baldur.api.admin.registry import AdminRegistry, AdminRoute
from baldur.interfaces.web_framework import HttpMethod, PermissionLevel

logger = structlog.get_logger()


def _register_operations_routes(registry: AdminRegistry) -> None:
    try:
        from baldur.api.handlers.auto_tuning import (
            auto_tuning_bounds_get,
            auto_tuning_bounds_update,
            auto_tuning_disable,
            auto_tuning_enable,
            auto_tuning_history,
            auto_tuning_metrics,
            auto_tuning_module_disable,
            auto_tuning_module_enable,
            auto_tuning_override_clear,
            auto_tuning_override_set,
            auto_tuning_status,
        )
        from baldur.api.handlers.drift_threshold import (
            drift_threshold_config_get,
            drift_threshold_config_update,
            drift_threshold_reset,
        )
        from baldur.api.handlers.grafana_webhook import (
            grafana_alert_webhook,
            grafana_webhook_test_get,
            grafana_webhook_test_post,
        )
        from baldur.api.handlers.meta_watchdog import (
            meta_watchdog_force_check,
            meta_watchdog_liveness,
            meta_watchdog_send_test,
            meta_watchdog_status,
        )
        from baldur.api.handlers.metric_sync import (
            drift_report,
            metric_sync,
        )
    except Exception as exc:
        logger.debug("admin.operations_routes_unavailable", error=exc)
        return

    for route in (
        AdminRoute(
            HttpMethod.GET,
            "/auto-tuning/status",
            auto_tuning_status,
            PermissionLevel.VIEWER,
        ),
        AdminRoute(
            HttpMethod.POST,
            "/auto-tuning/enable",
            auto_tuning_enable,
            PermissionLevel.ADMIN,
        ),
        AdminRoute(
            HttpMethod.POST,
            "/auto-tuning/disable",
            auto_tuning_disable,
            PermissionLevel.ADMIN,
        ),
        AdminRoute(
            HttpMethod.POST,
            "/auto-tuning/module/{module_name}/enable",
            auto_tuning_module_enable,
            PermissionLevel.ADMIN,
        ),
        AdminRoute(
            HttpMethod.POST,
            "/auto-tuning/module/{module_name}/disable",
            auto_tuning_module_disable,
            PermissionLevel.ADMIN,
        ),
        AdminRoute(
            HttpMethod.GET,
            "/auto-tuning/bounds",
            auto_tuning_bounds_get,
            PermissionLevel.VIEWER,
        ),
        AdminRoute(
            HttpMethod.PUT,
            "/auto-tuning/bounds",
            auto_tuning_bounds_update,
            PermissionLevel.ADMIN,
        ),
        AdminRoute(
            HttpMethod.GET,
            "/auto-tuning/history",
            auto_tuning_history,
            PermissionLevel.VIEWER,
        ),
        AdminRoute(
            HttpMethod.POST,
            "/auto-tuning/override",
            auto_tuning_override_set,
            PermissionLevel.ADMIN,
        ),
        AdminRoute(
            HttpMethod.DELETE,
            "/auto-tuning/override/{parameter}",
            auto_tuning_override_clear,
            PermissionLevel.ADMIN,
        ),
        AdminRoute(
            HttpMethod.GET,
            "/auto-tuning/metrics",
            auto_tuning_metrics,
            PermissionLevel.VIEWER,
        ),
        AdminRoute(
            HttpMethod.GET,
            "/meta-watchdog/liveness",
            meta_watchdog_liveness,
            PermissionLevel.PUBLIC,
        ),
        AdminRoute(
            HttpMethod.GET,
            "/meta-watchdog/status",
            meta_watchdog_status,
            PermissionLevel.VIEWER,
        ),
        AdminRoute(
            HttpMethod.POST,
            "/meta-watchdog/force-check",
            meta_watchdog_force_check,
            PermissionLevel.OPERATOR,
        ),
        AdminRoute(
            HttpMethod.POST,
            "/meta-watchdog/escalation-test",
            meta_watchdog_send_test,
            PermissionLevel.OPERATOR,
        ),
        AdminRoute(
            HttpMethod.POST, "/metric-sync", metric_sync, PermissionLevel.OPERATOR
        ),
        AdminRoute(
            HttpMethod.GET,
            "/metric-sync/drift-report",
            drift_report,
            PermissionLevel.VIEWER,
        ),
        AdminRoute(
            HttpMethod.GET,
            "/drift-threshold/config",
            drift_threshold_config_get,
            PermissionLevel.VIEWER,
        ),
        AdminRoute(
            HttpMethod.PUT,
            "/drift-threshold/config",
            drift_threshold_config_update,
            PermissionLevel.ADMIN,
        ),
        AdminRoute(
            HttpMethod.POST,
            "/drift-threshold/reset",
            drift_threshold_reset,
            PermissionLevel.ADMIN,
        ),
        AdminRoute(
            HttpMethod.POST,
            "/grafana/webhook",
            grafana_alert_webhook,
            PermissionLevel.OPERATOR,
        ),
        AdminRoute(
            HttpMethod.GET,
            "/grafana/webhook/test",
            grafana_webhook_test_get,
            PermissionLevel.VIEWER,
        ),
        AdminRoute(
            HttpMethod.POST,
            "/grafana/webhook/test",
            grafana_webhook_test_post,
            PermissionLevel.OPERATOR,
        ),
    ):
        registry.register(route)
