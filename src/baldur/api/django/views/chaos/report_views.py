"""
Chaos Engineering Report Views.

Thin HandlerAPIView wrappers delegating to framework-agnostic handlers.
Handlers extracted to api/handlers/chaos_report.py (Phase 2b — 432).
"""

from baldur.api.django.base import HandlerAPIView
from baldur.api.handlers.chaos_report import (
    chaos_dry_run_analysis,
    chaos_grade_history,
    chaos_report_detail,
    chaos_report_generate,
    chaos_report_list,
)
from baldur.interfaces.web_framework import PermissionLevel

__all__ = [
    "ReportListView",
    "ReportDetailView",
    "ReportGenerateView",
    "GradeHistoryView",
    "DryRunAnalysisView",
]


class ReportListView(HandlerAPIView):
    """List resilience reports."""

    permission_level = PermissionLevel.VIEWER
    handler = chaos_report_list


class ReportDetailView(HandlerAPIView):
    """Get report by ID or date."""

    permission_level = PermissionLevel.VIEWER
    handler = chaos_report_detail


class ReportGenerateView(HandlerAPIView):
    """Generate report on demand."""

    permission_level = PermissionLevel.ADMIN
    handler = chaos_report_generate


class GradeHistoryView(HandlerAPIView):
    """Grade history for trending."""

    permission_level = PermissionLevel.VIEWER
    handler = chaos_grade_history


class DryRunAnalysisView(HandlerAPIView):
    """Dry-run analysis with impact prediction."""

    permission_level = PermissionLevel.VIEWER
    handler = chaos_dry_run_analysis
