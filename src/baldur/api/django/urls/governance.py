"""Governance URL patterns (unified hub + RBAC + 4-eyes approval + L2 storage config)."""

from __future__ import annotations

from django.urls import path

from baldur.api.django.views.governance import (
    ApprovalRequestApproveView,
    ApprovalRequestListView,
    ApprovalRequestRejectView,
    GovernanceConfigView,
    GovernanceModeView,
    GovernanceRBACStatusView,
    GovernanceReconcileView,
    L2StorageConfigManagedView,
    MetricStatusView,
)

urlpatterns = [
    # Observability — unified status query
    path("metrics/status/", MetricStatusView.as_view(), name="metrics-status"),
    # Control — reconciliation + mode switch
    path(
        "governance/reconcile/",
        GovernanceReconcileView.as_view(),
        name="governance-reconcile",
    ),
    path("governance/mode/", GovernanceModeView.as_view(), name="governance-mode"),
    # RBAC status & config
    path(
        "governance/status/",
        GovernanceRBACStatusView.as_view(),
        name="governance-status",
    ),
    path(
        "config/governance/", GovernanceConfigView.as_view(), name="config-governance"
    ),
    # 4-eyes approval workflow
    path(
        "governance/approval-requests/",
        ApprovalRequestListView.as_view(),
        name="approval-requests-list",
    ),
    path(
        "governance/approval-requests/<str:request_id>/approve/",
        ApprovalRequestApproveView.as_view(),
        name="approval-request-approve",
    ),
    path(
        "governance/approval-requests/<str:request_id>/reject/",
        ApprovalRequestRejectView.as_view(),
        name="approval-request-reject",
    ),
    # L2 storage config (RuntimeConfigManager integration)
    path(
        "config/l2-storage/",
        L2StorageConfigManagedView.as_view(),
        name="config-l2-storage",
    ),
]
