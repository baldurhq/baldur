"""Chaos engineering URL patterns (config, schedules, kill-switch, reports)."""

from __future__ import annotations

from django.urls import path

from baldur.api.django.views.chaos import (
    BlastRadiusCheckView,
    ChaosBlastRadiusPolicyView,
    DryRunAnalysisView,
    DryRunConfigView,
    GradeHistoryView,
    KillAllView,
    KillSwitchView,
    PendingApprovalsView,
    ReportConfigView,
    ReportDetailView,
    ReportGenerateView,
    ReportListView,
    SafetyCheckView,
    SafetyGuardConfigView,
    ScheduleApprovalView,
    ScheduleDetailView,
    ScheduleExecuteView,
    ScheduleListView,
    SchedulerConfigView,
    StopConditionsConfigView,
    TTLConfigView,
)

urlpatterns = [
    # Configuration
    path(
        "chaos/config/safety-guard/",
        SafetyGuardConfigView.as_view(),
        name="chaos-config-safety-guard",
    ),
    path(
        "chaos/config/blast-radius/",
        ChaosBlastRadiusPolicyView.as_view(),
        name="chaos-config-blast-radius",
    ),
    path(
        "chaos/config/scheduler/",
        SchedulerConfigView.as_view(),
        name="chaos-config-scheduler",
    ),
    path(
        "chaos/config/reports/", ReportConfigView.as_view(), name="chaos-config-reports"
    ),
    # Safety Mechanism Configuration
    path(
        "chaos/config/stop-conditions/",
        StopConditionsConfigView.as_view(),
        name="chaos-config-stop-conditions",
    ),
    path("chaos/config/ttl/", TTLConfigView.as_view(), name="chaos-config-ttl"),
    path(
        "chaos/config/dry-run/", DryRunConfigView.as_view(), name="chaos-config-dry-run"
    ),
    # Dry Run Analysis with Impact Prediction
    path(
        "chaos/dry-run/analyze/",
        DryRunAnalysisView.as_view(),
        name="chaos-dry-run-analyze",
    ),
    # Scheduled Experiments CRUD
    path("chaos/schedules/", ScheduleListView.as_view(), name="chaos-schedules-list"),
    path(
        "chaos/schedules/<str:schedule_id>/",
        ScheduleDetailView.as_view(),
        name="chaos-schedule-detail",
    ),
    path(
        "chaos/schedules/<str:schedule_id>/approve/",
        ScheduleApprovalView.as_view(),
        name="chaos-schedule-approve",
    ),
    path(
        "chaos/schedules/<str:schedule_id>/execute/",
        ScheduleExecuteView.as_view(),
        name="chaos-schedule-execute",
    ),
    # Kill Switch
    path("chaos/kill-switch/", KillSwitchView.as_view(), name="chaos-kill-switch"),
    # Kill All Control
    path(
        "chaos/control/kill-all/", KillAllView.as_view(), name="chaos-control-kill-all"
    ),
    # Safety & Blast Radius Checks
    path("chaos/safety-check/", SafetyCheckView.as_view(), name="chaos-safety-check"),
    path(
        "chaos/blast-radius/check/",
        BlastRadiusCheckView.as_view(),
        name="chaos-blast-radius-check",
    ),
    # Reports
    path("chaos/reports/", ReportListView.as_view(), name="chaos-reports"),
    path(
        "chaos/reports/<str:report_id>/",
        ReportDetailView.as_view(),
        name="chaos-report-detail",
    ),
    path(
        "chaos/reports/generate/",
        ReportGenerateView.as_view(),
        name="chaos-reports-generate",
    ),
    path(
        "chaos/reports/grades/", GradeHistoryView.as_view(), name="chaos-grade-history"
    ),
    # Pending Approvals
    path(
        "chaos/pending-approvals/",
        PendingApprovalsView.as_view(),
        name="chaos-pending-approvals",
    ),
]
