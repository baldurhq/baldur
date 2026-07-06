"""
Reconciliation (Shadow Budget) API Endpoints.

Provides REST API for shadow budget calculation, fail-safe period management,
excluded period management, and reconciliation configuration.

Handlers extracted to api/handlers/error_budget_reconciliation.py (Phase 2b — 432).
"""

from baldur.api.django.base import HandlerAPIView
from baldur.api.handlers.error_budget_reconciliation import (
    reconciliation_config_get,
    reconciliation_config_update,
    reconciliation_excluded_period_delete,
    reconciliation_excluded_periods_create,
    reconciliation_excluded_periods_list,
    reconciliation_failsafe_periods,
    reconciliation_shadow_budget_approve,
    reconciliation_shadow_budget_detail,
    reconciliation_shadow_budget_reject,
    reconciliation_shadow_budgets_calculate,
    reconciliation_shadow_budgets_list,
    reconciliation_status,
)
from baldur.interfaces.web_framework import HttpMethod, PermissionLevel

__all__ = [
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


class ReconciliationStatusView(HandlerAPIView):
    """Reconciliation status endpoint."""

    permission_level = PermissionLevel.VIEWER
    handler = reconciliation_status


class FailSafePeriodsView(HandlerAPIView):
    """Fail-safe periods list endpoint."""

    permission_level = PermissionLevel.VIEWER
    handler = reconciliation_failsafe_periods


class ShadowBudgetsView(HandlerAPIView):
    """Shadow budget list and calculation trigger."""

    handler_map = {
        HttpMethod.GET: reconciliation_shadow_budgets_list,
        HttpMethod.POST: reconciliation_shadow_budgets_calculate,
    }
    permission_map = {
        HttpMethod.GET: PermissionLevel.VIEWER,
        HttpMethod.POST: PermissionLevel.VIEWER,
    }


class ShadowBudgetDetailView(HandlerAPIView):
    """Shadow budget detail endpoint."""

    permission_level = PermissionLevel.VIEWER
    handler = reconciliation_shadow_budget_detail


class ShadowBudgetApproveView(HandlerAPIView):
    """Approve shadow budget and apply to primary budget."""

    permission_level = PermissionLevel.ADMIN
    handler = reconciliation_shadow_budget_approve


class ShadowBudgetRejectView(HandlerAPIView):
    """Reject shadow budget (mark period as excluded)."""

    permission_level = PermissionLevel.ADMIN
    handler = reconciliation_shadow_budget_reject


class ExcludedPeriodsView(HandlerAPIView):
    """Excluded period list and creation."""

    handler_map = {
        HttpMethod.GET: reconciliation_excluded_periods_list,
        HttpMethod.POST: reconciliation_excluded_periods_create,
    }
    permission_map = {
        HttpMethod.GET: PermissionLevel.VIEWER,
        HttpMethod.POST: PermissionLevel.VIEWER,
    }


class ExcludedPeriodDetailView(HandlerAPIView):
    """Remove excluded period (re-include in calculation)."""

    permission_level = PermissionLevel.ADMIN
    handler = reconciliation_excluded_period_delete


class ReconciliationConfigView(HandlerAPIView):
    """Reconciliation configuration view and update."""

    handler_map = {
        HttpMethod.GET: reconciliation_config_get,
        HttpMethod.PUT: reconciliation_config_update,
    }
    permission_map = {
        HttpMethod.GET: PermissionLevel.ADMIN,
        HttpMethod.PUT: PermissionLevel.ADMIN,
    }
