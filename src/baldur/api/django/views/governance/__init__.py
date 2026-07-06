"""
Governance API Views Package - 통합 거버넌스 허브.

API 구조:
- GET /api/baldur/metrics/status/ - 통합 상태 조회 (Observability)
- POST /api/baldur/governance/reconcile/ - 수동 정합성 조정 (Control)
- POST /api/baldur/governance/mode/ - 운영 모드 강제 전환 (Control)
- GET /api/baldur/governance/status/ - RBAC 상태 조회
- GET/PUT /api/baldur/config/governance/ - 거버넌스 설정

Design Philosophy:
- 관찰(Observability)과 제어(Control) 분리
- 엔드포인트 파편화 방지
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
