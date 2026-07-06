"""
Governance Control Views.

Thin HandlerAPIView wrappers for reconciliation and mode switching.
Handlers extracted to api/handlers/governance.py (Phase 2b — 432).
"""

from baldur.api.django.base import HandlerAPIView
from baldur.api.handlers.governance import (
    governance_mode_get,
    governance_mode_set,
    governance_reconcile,
)
from baldur.interfaces.web_framework import HttpMethod, PermissionLevel

__all__ = [
    "GovernanceReconcileView",
    "GovernanceModeView",
]


class GovernanceReconcileView(HandlerAPIView):
    """Manual reconciliation endpoint."""

    handler = governance_reconcile
    permission_level = PermissionLevel.ADMIN


class GovernanceModeView(HandlerAPIView):
    """Operating mode read and switch endpoint."""

    handler_map = {
        HttpMethod.GET: governance_mode_get,
        HttpMethod.POST: governance_mode_set,
    }
    permission_level = PermissionLevel.ADMIN
