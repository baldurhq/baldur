"""
Compliance REST API Endpoints.

Endpoints for compliance check execution, report retrieval,
and evidence review.

Handlers extracted to api/handlers/compliance.py (Phase 2b — 432).
"""

from baldur.api.django.base import HandlerAPIView
from baldur.api.handlers.compliance import (
    compliance_checks,
    compliance_evidence_review,
    compliance_pending_evidence,
    compliance_report_detail,
    compliance_reports,
    compliance_run,
    compliance_standards,
)
from baldur.interfaces.web_framework import PermissionLevel

__all__ = [
    "ComplianceChecksView",
    "ComplianceEvidenceReviewView",
    "CompliancePendingEvidenceView",
    "ComplianceReportDetailView",
    "ComplianceReportsView",
    "ComplianceRunView",
    "ComplianceStandardsView",
]


class ComplianceStandardsView(HandlerAPIView):
    """List active compliance standards."""

    permission_level = PermissionLevel.VIEWER
    handler = compliance_standards


class ComplianceChecksView(HandlerAPIView):
    """List check definitions (all or by standard)."""

    permission_level = PermissionLevel.VIEWER
    handler = compliance_checks


class ComplianceRunView(HandlerAPIView):
    """Run compliance checks (all or by standard)."""

    permission_level = PermissionLevel.OPERATOR
    handler = compliance_run


class ComplianceReportsView(HandlerAPIView):
    """List compliance reports from audit log."""

    permission_level = PermissionLevel.VIEWER
    handler = compliance_reports


class ComplianceReportDetailView(HandlerAPIView):
    """Get detailed compliance report."""

    permission_level = PermissionLevel.VIEWER
    handler = compliance_report_detail


class CompliancePendingEvidenceView(HandlerAPIView):
    """List EVIDENCE checks with pending review status."""

    permission_level = PermissionLevel.VIEWER
    handler = compliance_pending_evidence


class ComplianceEvidenceReviewView(HandlerAPIView):
    """Review an EVIDENCE check result (approve/reject)."""

    permission_level = PermissionLevel.OPERATOR
    handler = compliance_evidence_review
