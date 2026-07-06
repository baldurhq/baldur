"""Recovery coordinator URL patterns (status, actions, approvals, history)."""

from __future__ import annotations

from django.urls import path

from baldur.api.django.views.recovery import (
    RecoveryAbortView,
    RecoveryApproveView,
    RecoveryDashboardWidgetView,
    RecoveryHistoryView,
    RecoveryPendingApprovalsView,
    RecoveryRejectView,
    RecoveryStartView,
    RecoveryStatusView,
)

urlpatterns = [
    # Recovery Status
    path("recovery/status/", RecoveryStatusView.as_view(), name="recovery-status"),
    # Recovery Actions
    path("recovery/start/", RecoveryStartView.as_view(), name="recovery-start"),
    path("recovery/abort/", RecoveryAbortView.as_view(), name="recovery-abort"),
    # Pending Approvals
    path(
        "recovery/pending-approvals/",
        RecoveryPendingApprovalsView.as_view(),
        name="recovery-pending-approvals",
    ),
    path("recovery/approve/", RecoveryApproveView.as_view(), name="recovery-approve"),
    path("recovery/reject/", RecoveryRejectView.as_view(), name="recovery-reject"),
    # Recovery History
    path("recovery/history/", RecoveryHistoryView.as_view(), name="recovery-history"),
    # Dashboard Widget
    path(
        "recovery/widget/",
        RecoveryDashboardWidgetView.as_view(),
        name="recovery-widget",
    ),
]
