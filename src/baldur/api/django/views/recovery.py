"""
Recovery REST API Views.

Thin HandlerAPIView wrappers for recovery process management.
Handlers extracted to api/handlers/recovery.py (Phase 2b — 432).
"""

from baldur.api.django.base import HandlerAPIView
from baldur.api.handlers.recovery import (
    recovery_abort,
    recovery_approve,
    recovery_dashboard_widget,
    recovery_history,
    recovery_pending_approvals,
    recovery_reject,
    recovery_start,
    recovery_status,
)
from baldur.interfaces.web_framework import PermissionLevel


class RecoveryStatusView(HandlerAPIView):
    """Current recovery status endpoint."""

    handler = recovery_status
    permission_level = PermissionLevel.AUTHENTICATED


class RecoveryStartView(HandlerAPIView):
    """Start recovery process endpoint."""

    handler = recovery_start
    permission_level = PermissionLevel.AUTHENTICATED


class RecoveryAbortView(HandlerAPIView):
    """Abort recovery process endpoint."""

    handler = recovery_abort
    permission_level = PermissionLevel.AUTHENTICATED


class RecoveryPendingApprovalsView(HandlerAPIView):
    """Pending recovery approvals list endpoint."""

    handler = recovery_pending_approvals
    permission_level = PermissionLevel.AUTHENTICATED


class RecoveryApproveView(HandlerAPIView):
    """Approve a recovery request endpoint."""

    handler = recovery_approve
    permission_level = PermissionLevel.AUTHENTICATED


class RecoveryRejectView(HandlerAPIView):
    """Reject a recovery request endpoint."""

    handler = recovery_reject
    permission_level = PermissionLevel.AUTHENTICATED


class RecoveryHistoryView(HandlerAPIView):
    """Recovery history endpoint."""

    handler = recovery_history
    permission_level = PermissionLevel.AUTHENTICATED


class RecoveryDashboardWidgetView(HandlerAPIView):
    """Recovery dashboard widget data endpoint."""

    handler = recovery_dashboard_widget
    permission_level = PermissionLevel.AUTHENTICATED
