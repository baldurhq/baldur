"""
L2 Storage Drift Reconciliation API Views.

Thin HandlerAPIView wrappers delegating to framework-agnostic handlers.
Handlers extracted to api/handlers/l2_storage.py (Phase 2b — 432).
"""

from baldur.api.django.base import HandlerAPIView
from baldur.api.handlers.l2_storage import (
    drift_reconciliation_history,
    drift_reconciliation_service,
    drift_reconciliation_stats,
    drift_reconciliation_trigger,
)
from baldur.interfaces.web_framework import PermissionLevel

__all__ = [
    "DriftReconciliationStatsView",
    "DriftReconciliationHistoryView",
    "DriftReconciliationTriggerView",
    "DriftReconciliationServiceView",
]


class DriftReconciliationStatsView(HandlerAPIView):
    """Drift reconciliation statistics endpoint."""

    permission_level = PermissionLevel.VIEWER
    handler = drift_reconciliation_stats


class DriftReconciliationHistoryView(HandlerAPIView):
    """Drift reconciliation history endpoint."""

    permission_level = PermissionLevel.VIEWER
    handler = drift_reconciliation_history


class DriftReconciliationTriggerView(HandlerAPIView):
    """Force drift reconciliation for all services."""

    permission_level = PermissionLevel.ADMIN
    handler = drift_reconciliation_trigger


class DriftReconciliationServiceView(HandlerAPIView):
    """Force drift reconciliation for a single service."""

    permission_level = PermissionLevel.ADMIN
    handler = drift_reconciliation_service
