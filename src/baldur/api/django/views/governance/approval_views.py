"""
Governance Approval Views.

Thin HandlerAPIView wrappers for 4-Eyes Approval workflows.
Handlers extracted to api/handlers/governance.py (Phase 2b — 432).
"""

from baldur.api.django.base import HandlerAPIView
from baldur.api.handlers.governance import (
    approval_request_approve,
    approval_request_create,
    approval_request_list,
    approval_request_reject,
)
from baldur.interfaces.web_framework import HttpMethod, PermissionLevel

__all__ = [
    "ApprovalRequestListView",
    "ApprovalRequestApproveView",
    "ApprovalRequestRejectView",
]


class ApprovalRequestListView(HandlerAPIView):
    """4-Eyes approval request list and create endpoint."""

    handler_map = {
        HttpMethod.GET: approval_request_list,
        HttpMethod.POST: approval_request_create,
    }
    permission_level = PermissionLevel.ADMIN


class ApprovalRequestApproveView(HandlerAPIView):
    """Approve a pending 4-Eyes approval request."""

    handler = approval_request_approve
    permission_level = PermissionLevel.ADMIN


class ApprovalRequestRejectView(HandlerAPIView):
    """Reject a pending 4-Eyes approval request."""

    handler = approval_request_reject
    permission_level = PermissionLevel.ADMIN
