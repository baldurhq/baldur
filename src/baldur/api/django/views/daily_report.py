"""
Daily Report REST API views (Phase 4, D8) — thin HandlerAPIView wrappers.

Business logic lives in api/handlers/daily_report.py as framework-agnostic
functions; views below adapt them to the Django/DRF dispatch pipeline.

Endpoints:
    GET /api/reports/daily/               List (recent N days, summary)
    GET /api/reports/daily/trend/         Trend data (key metrics over time)
    GET /api/reports/daily/{date}/        Single date (summary or detail)

Data source:
    Per-date state backend keys "baldur:daily_reports:{YYYY-MM-DD}" — written
    by DailyReportService._persist_report() on each daily report generation
    (430 D1). Query logic lives in the framework-agnostic handlers.
"""

from __future__ import annotations

from baldur.api.django.base import HandlerAPIView
from baldur.api.handlers.daily_report import (
    daily_report_detail,
    daily_report_list,
    daily_report_trend,
)
from baldur.interfaces.web_framework import PermissionLevel


class DailyReportListView(HandlerAPIView):
    """List recent persisted daily reports (summary view)."""

    permission_level = PermissionLevel.VIEWER
    handler = daily_report_list


class DailyReportDetailView(HandlerAPIView):
    """Return a single persisted report by date."""

    permission_level = PermissionLevel.VIEWER
    handler = daily_report_detail


class DailyReportTrendView(HandlerAPIView):
    """Return metric trend data over a date range."""

    permission_level = PermissionLevel.VIEWER
    handler = daily_report_trend


__all__ = [
    "DailyReportListView",
    "DailyReportDetailView",
    "DailyReportTrendView",
]
