"""Compliance URL patterns (345 — Compliance Service Redesign).

Conditionally loaded — compliance views may live in optional packages,
and the entire group is gated by ``compliance.enabled`` per impl 527 D8
(Dormant tier; no marketing surface for v1.0 launch).
"""

from __future__ import annotations

from django.urls import path

from baldur.settings.compliance import get_compliance_settings

if not get_compliance_settings().enabled:
    urlpatterns: list = []
else:
    try:
        from baldur.api.django.views.compliance import (
            ComplianceChecksView,
            ComplianceEvidenceReviewView,
            CompliancePendingEvidenceView,
            ComplianceReportDetailView,
            ComplianceReportsView,
            ComplianceRunView,
            ComplianceStandardsView,
        )

        urlpatterns = [
            path(
                "compliance/standards/",
                ComplianceStandardsView.as_view(),
                name="compliance-standards",
            ),
            path(
                "compliance/checks/",
                ComplianceChecksView.as_view(),
                name="compliance-checks",
            ),
            path(
                "compliance/checks/<str:standard>/",
                ComplianceChecksView.as_view(),
                name="compliance-checks-by-standard",
            ),
            path(
                "compliance/run/",
                ComplianceRunView.as_view(),
                name="compliance-run",
            ),
            path(
                "compliance/run/<str:standard>/",
                ComplianceRunView.as_view(),
                name="compliance-run-standard",
            ),
            path(
                "compliance/reports/",
                ComplianceReportsView.as_view(),
                name="compliance-reports",
            ),
            path(
                "compliance/reports/<str:report_id>/",
                ComplianceReportDetailView.as_view(),
                name="compliance-report-detail",
            ),
            path(
                "compliance/reports/<str:report_id>/evidence/pending/",
                CompliancePendingEvidenceView.as_view(),
                name="compliance-pending-evidence",
            ),
            path(
                "compliance/reports/<str:report_id>/checks/<str:check_id>/review/",
                ComplianceEvidenceReviewView.as_view(),
                name="compliance-evidence-review",
            ),
        ]
    except ImportError:
        urlpatterns = []
