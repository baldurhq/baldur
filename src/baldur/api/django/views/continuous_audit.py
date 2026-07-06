"""
Continuous Audit API Endpoints.

Provides REST API for continuous audit system:
- Audit log query/filter/export
- Integrity verification
- Raw data access (no report formatting)

Relocated from audit/continuous_audit_api.py (369 — Audit API Relocation).
Handlers extracted to api/handlers/continuous_audit.py (Phase 2b — 432).
"""

from baldur.api.django.base import HandlerAPIView
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
from baldur.interfaces.web_framework import PermissionLevel

__all__ = [
    "ContinuousAuditQueryView",
    "ContinuousAuditDetailView",
    "ContinuousAuditAutoTuningView",
    "DriftHistoryView",
    "ComplianceHistoryView",
    "IntegrityVerifyView",
    "ChainStateView",
    "ExportJSONLView",
    "ExportCSVView",
    "ConfigView",
]


class ContinuousAuditQueryView(HandlerAPIView):
    """Audit log query endpoint."""

    permission_level = PermissionLevel.VIEWER
    handler = continuous_audit_query


class ContinuousAuditDetailView(HandlerAPIView):
    """Single audit log detail view."""

    permission_level = PermissionLevel.VIEWER
    handler = continuous_audit_detail


class ContinuousAuditAutoTuningView(HandlerAPIView):
    """Auto tuning history view (authentication required)."""

    permission_level = PermissionLevel.VIEWER
    handler = continuous_audit_auto_tuning


class DriftHistoryView(HandlerAPIView):
    """DNA drift history view."""

    permission_level = PermissionLevel.VIEWER
    handler = continuous_audit_drift_history


class ComplianceHistoryView(HandlerAPIView):
    """Compliance check history view."""

    permission_level = PermissionLevel.VIEWER
    handler = continuous_audit_compliance_history


class IntegrityVerifyView(HandlerAPIView):
    """Audit log integrity verification."""

    permission_level = PermissionLevel.VIEWER
    handler = continuous_audit_integrity_verify


class ChainStateView(HandlerAPIView):
    """Hash chain state view."""

    permission_level = PermissionLevel.VIEWER
    handler = continuous_audit_chain_state


class ExportJSONLView(HandlerAPIView):
    """JSON Lines streaming export."""

    permission_level = PermissionLevel.VIEWER
    handler = continuous_audit_export_jsonl


class ExportCSVView(HandlerAPIView):
    """CSV streaming export."""

    permission_level = PermissionLevel.VIEWER
    handler = continuous_audit_export_csv


class ConfigView(HandlerAPIView):
    """Audit configuration view."""

    permission_level = PermissionLevel.VIEWER
    handler = continuous_audit_config
