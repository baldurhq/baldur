"""
L2 Storage Shadow Log API Views.

Thin HandlerAPIView wrappers delegating to framework-agnostic handlers.
Handlers extracted to api/handlers/l2_storage_shadow_log.py (Phase 2b — 432).
"""

from baldur.api.django.base import HandlerAPIView
from baldur.api.handlers.l2_storage_shadow_log import (
    shadow_log_analyze,
    shadow_log_by_service,
    shadow_log_clear,
    shadow_log_list,
    shadow_log_replay,
    shadow_log_stats,
)
from baldur.interfaces.web_framework import PermissionLevel

__all__ = [
    "ShadowLogListView",
    "ShadowLogStatsView",
    "ShadowLogClearView",
    "ShadowLogAnalyzeView",
    "ShadowLogReplayView",
    "ShadowLogByServiceView",
]


class ShadowLogListView(HandlerAPIView):
    """Shadow log entries endpoint."""

    permission_level = PermissionLevel.VIEWER
    handler = shadow_log_list


class ShadowLogStatsView(HandlerAPIView):
    """Shadow log statistics endpoint."""

    permission_level = PermissionLevel.VIEWER
    handler = shadow_log_stats


class ShadowLogClearView(HandlerAPIView):
    """Clear shadow log entries."""

    permission_level = PermissionLevel.ADMIN
    handler = shadow_log_clear


class ShadowLogAnalyzeView(HandlerAPIView):
    """Analyze L2 failures for forensic investigation."""

    permission_level = PermissionLevel.VIEWER
    handler = shadow_log_analyze


class ShadowLogReplayView(HandlerAPIView):
    """Replay unsynced shadow log records to L2."""

    permission_level = PermissionLevel.OPERATOR
    handler = shadow_log_replay


class ShadowLogByServiceView(HandlerAPIView):
    """Shadow log entries by service."""

    permission_level = PermissionLevel.VIEWER
    handler = shadow_log_by_service
