"""
Error Budget Status API Endpoints.

Provides REST API for Error Budget status, history, error recording,
exhaustion simulation, and simulation reset.

Handlers extracted to api/handlers/error_budget_status.py (Phase 2b — 432).
"""

from baldur.api.django.base import HandlerAPIView
from baldur.api.handlers.error_budget_status import (
    budget_exhaust,
    budget_history,
    budget_record,
    budget_reset_simulation,
    budget_status,
)
from baldur.interfaces.web_framework import PermissionLevel

__all__ = [
    "ErrorBudgetStatusView",
    "ErrorBudgetHistoryView",
    "ErrorBudgetRecordView",
    "ErrorBudgetExhaustView",
    "ErrorBudgetResetSimulationView",
]


class ErrorBudgetStatusView(HandlerAPIView):
    """Error Budget status query endpoint."""

    permission_level = PermissionLevel.VIEWER
    handler = budget_status


class ErrorBudgetHistoryView(HandlerAPIView):
    """Error Budget decision history endpoint."""

    permission_level = PermissionLevel.VIEWER
    handler = budget_history


class ErrorBudgetRecordView(HandlerAPIView):
    """Record errors for budget consumption (Chaos/Test only)."""

    permission_level = PermissionLevel.ADMIN
    handler = budget_record


class ErrorBudgetExhaustView(HandlerAPIView):
    """Simulate budget exhaustion (Chaos/Test only)."""

    permission_level = PermissionLevel.ADMIN
    handler = budget_exhaust


class ErrorBudgetResetSimulationView(HandlerAPIView):
    """Reset simulation statistics."""

    permission_level = PermissionLevel.ADMIN
    handler = budget_reset_simulation
