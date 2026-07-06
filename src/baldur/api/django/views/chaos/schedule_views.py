"""
Chaos Engineering Schedule Views.

Thin HandlerAPIView wrappers delegating to framework-agnostic handlers.
Handlers extracted to api/handlers/chaos_schedule.py (Phase 2b — 432).
"""

from baldur.api.django.base import HandlerAPIView
from baldur.api.handlers.chaos_schedule import (
    chaos_pending_approvals,
    chaos_schedule_approval,
    chaos_schedule_create,
    chaos_schedule_delete,
    chaos_schedule_detail,
    chaos_schedule_execute,
    chaos_schedule_list,
    chaos_schedule_update,
)
from baldur.interfaces.web_framework import HttpMethod, PermissionLevel

__all__ = [
    "ScheduleListView",
    "ScheduleDetailView",
    "ScheduleApprovalView",
    "ScheduleExecuteView",
    "PendingApprovalsView",
]


class ScheduleListView(HandlerAPIView):
    """List and create scheduled experiments (GET viewer, POST admin)."""

    handler_map = {
        HttpMethod.GET: chaos_schedule_list,
        HttpMethod.POST: chaos_schedule_create,
    }
    permission_map = {
        HttpMethod.GET: PermissionLevel.VIEWER,
        HttpMethod.POST: PermissionLevel.ADMIN,
    }


class ScheduleDetailView(HandlerAPIView):
    """Individual schedule operations (GET viewer, PATCH/DELETE admin)."""

    handler_map = {
        HttpMethod.GET: chaos_schedule_detail,
        HttpMethod.PATCH: chaos_schedule_update,
        HttpMethod.DELETE: chaos_schedule_delete,
    }
    permission_map = {
        HttpMethod.GET: PermissionLevel.VIEWER,
        HttpMethod.PATCH: PermissionLevel.ADMIN,
        HttpMethod.DELETE: PermissionLevel.ADMIN,
    }


class ScheduleApprovalView(HandlerAPIView):
    """Approve or deny a scheduled experiment."""

    permission_level = PermissionLevel.ADMIN
    handler = chaos_schedule_approval


class ScheduleExecuteView(HandlerAPIView):
    """Execute a schedule immediately."""

    permission_level = PermissionLevel.ADMIN
    handler = chaos_schedule_execute


class PendingApprovalsView(HandlerAPIView):
    """List pending approvals."""

    permission_level = PermissionLevel.VIEWER
    handler = chaos_pending_approvals
