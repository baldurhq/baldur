"""
Error Budget API Views Package.

REST API endpoints for Error Budget management and Deployment Policy.

REFACTORED: this package splits the former error_budget.py (1179 lines) into
three modules.

Modules:
- status.py: Error Budget status query/recording (5 views)
- deployment.py: deployment policy decision recording (5 views)
- reconciliation.py: Shadow Budget management (9 views)

Endpoints:
- GET  /api/baldur/error-budget/status/
- GET  /api/baldur/error-budget/history/
- POST /api/baldur/error-budget/record/
- POST /api/baldur/error-budget/exhaust/
- POST /api/baldur/error-budget/reset-simulation/
- GET  /api/baldur/deployment-policy/verdict/
- POST /api/baldur/deployment-policy/acknowledge/
- POST /api/baldur/deployment-policy/override/
- POST /api/baldur/deployment-policy/lift/
- GET  /api/baldur/deployment-policy/active-override/
- GET  /api/baldur/reconciliation/status/
- GET  /api/baldur/reconciliation/failsafe-periods/
- GET/POST /api/baldur/reconciliation/shadow-budgets/
- POST /api/baldur/reconciliation/shadow-budgets/{id}/approve/
- POST /api/baldur/reconciliation/shadow-budgets/{id}/reject/
- GET/POST /api/baldur/reconciliation/excluded-periods/
- DELETE /api/baldur/reconciliation/excluded-periods/{id}/
- GET/PUT /api/baldur/reconciliation/config/

Core Principle: "the system advises, humans decide."
FAIL-SAFE DESIGN: on system failure -> default to PROCEED (fail-open)
"""

# Deployment policy views
from .deployment import (
    ActiveOverrideView,
    DeploymentFreezeAcknowledgeView,
    DeploymentFreezeLiftView,
    DeploymentOverrideView,
    DeploymentVerdictView,
)

# Reconciliation views
from .reconciliation import (
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

# Status views
from .status import (
    ErrorBudgetExhaustView,
    ErrorBudgetHistoryView,
    ErrorBudgetRecordView,
    ErrorBudgetResetSimulationView,
    ErrorBudgetStatusView,
)

__all__ = [
    # Status views
    "ErrorBudgetStatusView",
    "ErrorBudgetHistoryView",
    "ErrorBudgetRecordView",
    "ErrorBudgetExhaustView",
    "ErrorBudgetResetSimulationView",
    # Deployment policy views
    "DeploymentVerdictView",
    "DeploymentFreezeAcknowledgeView",
    "DeploymentOverrideView",
    "DeploymentFreezeLiftView",
    "ActiveOverrideView",
    # Reconciliation views
    "ReconciliationStatusView",
    "FailSafePeriodsView",
    "ShadowBudgetsView",
    "ShadowBudgetDetailView",
    "ShadowBudgetApproveView",
    "ShadowBudgetRejectView",
    "ExcludedPeriodsView",
    "ExcludedPeriodDetailView",
    "ReconciliationConfigView",
]
