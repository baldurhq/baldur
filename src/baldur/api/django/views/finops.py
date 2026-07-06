"""
FinOps DNA API Views.

Thin HandlerAPIView wrappers. Business logic extracted to
api/handlers/finops.py (Phase 2b -- 432).
"""

from baldur.api.django.base import HandlerAPIView
from baldur.api.handlers.finops import (
    finops_alert_acknowledge,
    finops_alerts_list,
    finops_budget_get,
    finops_budget_reset,
    finops_budget_set,
    finops_cost_record,
    finops_report,
)
from baldur.interfaces.web_framework import HttpMethod, PermissionLevel

__all__ = [
    "FinOpsBudgetView",
    "FinOpsCostView",
    "FinOpsReportView",
    "FinOpsAlertsView",
]


class FinOpsBudgetView(HandlerAPIView):
    """FinOps budget management endpoint."""

    handler_map = {
        HttpMethod.GET: finops_budget_get,
        HttpMethod.POST: finops_budget_set,
        HttpMethod.DELETE: finops_budget_reset,
    }
    permission_map = {
        HttpMethod.GET: PermissionLevel.VIEWER,
        HttpMethod.POST: PermissionLevel.ADMIN,
        HttpMethod.DELETE: PermissionLevel.ADMIN,
    }


class FinOpsCostView(HandlerAPIView):
    """FinOps cost recording endpoint."""

    permission_level = PermissionLevel.OPERATOR
    handler = finops_cost_record


class FinOpsReportView(HandlerAPIView):
    """FinOps report generation endpoint."""

    permission_level = PermissionLevel.VIEWER
    handler = finops_report


class FinOpsAlertsView(HandlerAPIView):
    """FinOps alerts query and acknowledgment endpoint."""

    handler_map = {
        HttpMethod.GET: finops_alerts_list,
        HttpMethod.POST: finops_alert_acknowledge,
    }
    permission_map = {
        HttpMethod.GET: PermissionLevel.VIEWER,
        HttpMethod.POST: PermissionLevel.OPERATOR,
    }
