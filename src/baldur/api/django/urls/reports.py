"""Daily report + dashboard URL patterns."""

from __future__ import annotations

from django.urls import path

from baldur.api.django.views.daily_report import (
    DailyReportDetailView,
    DailyReportListView,
    DailyReportTrendView,
)
from baldur.api.django.views.dashboard import (
    DashboardSummaryView,
)

urlpatterns = [
    # Trend route must precede the detail route so "/reports/daily/trend/" is
    # not matched by "<str:date>".
    path(
        "reports/daily/",
        DailyReportListView.as_view(),
        name="daily-report-list",
    ),
    path(
        "reports/daily/trend/",
        DailyReportTrendView.as_view(),
        name="daily-report-trend",
    ),
    path(
        "reports/daily/<str:date>/",
        DailyReportDetailView.as_view(),
        name="daily-report-detail",
    ),
    # Dashboard
    path(
        "dashboard/summary/", DashboardSummaryView.as_view(), name="dashboard-summary"
    ),
]
