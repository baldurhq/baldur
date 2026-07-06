"""Continuous audit + compliance admin routes."""

from __future__ import annotations

import structlog

from baldur.api.admin.registry import AdminRegistry, AdminRoute
from baldur.interfaces.web_framework import HttpMethod, PermissionLevel

logger = structlog.get_logger()


def _register_continuous_audit_routes(registry: AdminRegistry) -> None:
    try:
        from baldur.api.handlers.compliance import (
            compliance_checks,
            compliance_evidence_review,
            compliance_pending_evidence,
            compliance_report_detail,
            compliance_reports,
            compliance_run,
            compliance_standards,
        )
        from baldur.api.handlers.continuous_audit import (
            continuous_audit_auto_tuning,
            continuous_audit_chain_state,
            continuous_audit_compliance_history,
            continuous_audit_config,
            continuous_audit_detail,
            continuous_audit_drift_history,
            continuous_audit_export_csv,
            continuous_audit_export_jsonl,
            continuous_audit_integrity_verify,
            continuous_audit_query,
        )
    except Exception as exc:
        logger.debug("admin.continuous_audit_routes_unavailable", error=exc)
        return

    for route in (
        AdminRoute(
            HttpMethod.GET,
            "/audit/logs",
            continuous_audit_query,
            PermissionLevel.VIEWER,
        ),
        AdminRoute(
            HttpMethod.GET,
            "/audit/logs/{log_id}",
            continuous_audit_detail,
            PermissionLevel.VIEWER,
        ),
        AdminRoute(
            HttpMethod.GET,
            "/audit/auto-tuning",
            continuous_audit_auto_tuning,
            PermissionLevel.VIEWER,
        ),
        AdminRoute(
            HttpMethod.GET,
            "/audit/drift",
            continuous_audit_drift_history,
            PermissionLevel.VIEWER,
        ),
        AdminRoute(
            HttpMethod.GET,
            "/audit/compliance",
            continuous_audit_compliance_history,
            PermissionLevel.VIEWER,
        ),
        AdminRoute(
            HttpMethod.GET,
            "/audit/integrity/verify",
            continuous_audit_integrity_verify,
            PermissionLevel.VIEWER,
        ),
        AdminRoute(
            HttpMethod.GET,
            "/audit/integrity/state",
            continuous_audit_chain_state,
            PermissionLevel.VIEWER,
        ),
        AdminRoute(
            HttpMethod.GET,
            "/audit/export/jsonl",
            continuous_audit_export_jsonl,
            PermissionLevel.VIEWER,
        ),
        AdminRoute(
            HttpMethod.GET,
            "/audit/export/csv",
            continuous_audit_export_csv,
            PermissionLevel.VIEWER,
        ),
        AdminRoute(
            HttpMethod.GET,
            "/audit/config",
            continuous_audit_config,
            PermissionLevel.VIEWER,
        ),
        AdminRoute(
            HttpMethod.GET,
            "/compliance/standards",
            compliance_standards,
            PermissionLevel.VIEWER,
        ),
        AdminRoute(
            HttpMethod.GET,
            "/compliance/checks",
            compliance_checks,
            PermissionLevel.VIEWER,
        ),
        AdminRoute(
            HttpMethod.GET,
            "/compliance/checks/{standard}",
            compliance_checks,
            PermissionLevel.VIEWER,
        ),
        AdminRoute(
            HttpMethod.POST, "/compliance/run", compliance_run, PermissionLevel.OPERATOR
        ),
        AdminRoute(
            HttpMethod.POST,
            "/compliance/run/{standard}",
            compliance_run,
            PermissionLevel.OPERATOR,
        ),
        AdminRoute(
            HttpMethod.GET,
            "/compliance/reports",
            compliance_reports,
            PermissionLevel.VIEWER,
        ),
        AdminRoute(
            HttpMethod.GET,
            "/compliance/reports/{report_id}",
            compliance_report_detail,
            PermissionLevel.VIEWER,
        ),
        AdminRoute(
            HttpMethod.GET,
            "/compliance/reports/{report_id}/evidence/pending",
            compliance_pending_evidence,
            PermissionLevel.VIEWER,
        ),
        AdminRoute(
            HttpMethod.PATCH,
            "/compliance/reports/{report_id}/checks/{check_id}/review",
            compliance_evidence_review,
            PermissionLevel.OPERATOR,
        ),
    ):
        registry.register(route)
