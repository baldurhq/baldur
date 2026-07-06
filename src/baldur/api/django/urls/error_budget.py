"""Error-budget + deployment-policy + reconciliation URL patterns."""

from __future__ import annotations

from django.urls import path

from baldur.api.django.views.error_budget.deployment import (
    ActiveOverrideView,
    DeploymentFreezeAcknowledgeView,
    DeploymentFreezeLiftView,
    DeploymentOverrideView,
    DeploymentVerdictView,
)
from baldur.api.django.views.error_budget.reconciliation import (
    ExcludedPeriodDetailView,
    ExcludedPeriodsView,
    FailSafePeriodsView,
    ReconciliationConfigView,
    ReconciliationStatusView,
    ShadowBudgetApproveView,
    ShadowBudgetDetailView,
    ShadowBudgetRejectView,
    ShadowBudgetsView,
)
from baldur.api.django.views.error_budget.status import (
    ErrorBudgetExhaustView,
    ErrorBudgetHistoryView,
    ErrorBudgetRecordView,
    ErrorBudgetResetSimulationView,
    ErrorBudgetStatusView,
)

urlpatterns = [
    # Error Budget API
    path(
        "error-budget/status/",
        ErrorBudgetStatusView.as_view(),
        name="error-budget-status",
    ),
    path(
        "error-budget/history/",
        ErrorBudgetHistoryView.as_view(),
        name="error-budget-history",
    ),
    # Chaos Engineering / Test APIs for Error Budget
    path(
        "error-budget/record/",
        ErrorBudgetRecordView.as_view(),
        name="error-budget-record",
    ),
    path(
        "error-budget/exhaust/",
        ErrorBudgetExhaustView.as_view(),
        name="error-budget-exhaust",
    ),
    path(
        "error-budget/reset-simulation/",
        ErrorBudgetResetSimulationView.as_view(),
        name="error-budget-reset-simulation",
    ),
    # Deployment Policy API
    path(
        "deployment-policy/verdict/",
        DeploymentVerdictView.as_view(),
        name="deployment-verdict",
    ),
    path(
        "deployment-policy/acknowledge/",
        DeploymentFreezeAcknowledgeView.as_view(),
        name="deployment-acknowledge",
    ),
    path(
        "deployment-policy/override/",
        DeploymentOverrideView.as_view(),
        name="deployment-override",
    ),
    path(
        "deployment-policy/lift/",
        DeploymentFreezeLiftView.as_view(),
        name="deployment-lift",
    ),
    path(
        "deployment-policy/active-override/",
        ActiveOverrideView.as_view(),
        name="deployment-active-override",
    ),
    # Reconciliation API (Shadow Budget) — "system calculates, human approves"
    path(
        "reconciliation/status/",
        ReconciliationStatusView.as_view(),
        name="reconciliation-status",
    ),
    path(
        "reconciliation/failsafe-periods/",
        FailSafePeriodsView.as_view(),
        name="reconciliation-failsafe-periods",
    ),
    path(
        "reconciliation/shadow-budgets/",
        ShadowBudgetsView.as_view(),
        name="reconciliation-shadow-budgets",
    ),
    path(
        "reconciliation/shadow-budgets/<str:calculation_id>/",
        ShadowBudgetDetailView.as_view(),
        name="reconciliation-shadow-budget-detail",
    ),
    path(
        "reconciliation/shadow-budgets/<str:calculation_id>/approve/",
        ShadowBudgetApproveView.as_view(),
        name="reconciliation-shadow-budget-approve",
    ),
    path(
        "reconciliation/shadow-budgets/<str:calculation_id>/reject/",
        ShadowBudgetRejectView.as_view(),
        name="reconciliation-shadow-budget-reject",
    ),
    path(
        "reconciliation/excluded-periods/",
        ExcludedPeriodsView.as_view(),
        name="reconciliation-excluded-periods",
    ),
    path(
        "reconciliation/excluded-periods/<str:exclusion_id>/",
        ExcludedPeriodDetailView.as_view(),
        name="reconciliation-excluded-period-detail",
    ),
    path(
        "reconciliation/config/",
        ReconciliationConfigView.as_view(),
        name="reconciliation-config",
    ),
]
