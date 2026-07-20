"""
Governance API Views Package - the unified governance hub.

API structure:
- GET /api/baldur/metrics/status/ - unified status query (Observability)
- POST /api/baldur/governance/reconcile/ - manual reconciliation (Control)
- POST /api/baldur/governance/mode/ - force an operating mode change (Control)
- GET /api/baldur/governance/status/ - RBAC status query
- GET/PUT /api/baldur/config/governance/ - governance settings

Design Philosophy:
- Separate observability from control
- Prevent endpoint fragmentation
"""

# Approval Views (4-Eyes)
from baldur.api.django.views.governance.approval_views import (
    ApprovalRequestApproveView,
    ApprovalRequestListView,
    ApprovalRequestRejectView,
)

# Config Views
from baldur.api.django.views.governance.config_views import (
    GovernanceConfigView,
    L2StorageConfigManagedView,
)

# Control Views
from baldur.api.django.views.governance.control_views import (
    GovernanceModeView,
    GovernanceReconcileView,
)

# Status Views (Observability)
from baldur.api.django.views.governance.status_views import (
    GovernanceRBACStatusView,
    MetricStatusView,
)

__all__ = [
    # API Views
    "MetricStatusView",
    "GovernanceReconcileView",
    "GovernanceModeView",
    # RBAC API Views
    "GovernanceRBACStatusView",
    "GovernanceConfigView",
    # 4-Eyes Approval API Views
    "ApprovalRequestListView",
    "ApprovalRequestApproveView",
    "ApprovalRequestRejectView",
    "L2StorageConfigManagedView",
]
