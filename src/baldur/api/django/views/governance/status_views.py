"""
Governance Status Views.

Thin HandlerAPIView wrappers for metric status and RBAC status.
Handlers extracted to api/handlers/governance.py (Phase 2b — 432).
"""

from baldur.api.django.base import HandlerAPIView
from baldur.api.handlers.governance import (
    governance_rbac_status,
    metric_status,
)
from baldur.interfaces.web_framework import PermissionLevel

__all__ = [
    "MetricStatusView",
    "GovernanceRBACStatusView",
]


class MetricStatusView(HandlerAPIView):
    """Integrated metric status overview endpoint."""

    handler = metric_status
    permission_level = PermissionLevel.ADMIN


class GovernanceRBACStatusView(HandlerAPIView):
    """Governance RBAC status endpoint."""

    handler = governance_rbac_status
    permission_level = PermissionLevel.VIEWER
