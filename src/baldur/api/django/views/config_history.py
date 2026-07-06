"""
Config History and Rollback API Views.

Thin HandlerAPIView wrappers. Business logic extracted to
api/handlers/config_history.py (Phase 2b -- 432).
"""

from baldur.api.django.base import HandlerAPIView
from baldur.api.handlers.config_history import (
    config_compare,
    config_history_list,
    config_rollback,
    config_version_detail,
)
from baldur.interfaces.web_framework import PermissionLevel

__all__ = [
    "ConfigHistoryView",
    "ConfigVersionDetailView",
    "ConfigRollbackView",
    "ConfigCompareView",
]


class ConfigHistoryView(HandlerAPIView):
    """Config change history list endpoint."""

    permission_level = PermissionLevel.VIEWER
    handler = config_history_list


class ConfigVersionDetailView(HandlerAPIView):
    """Specific config version detail endpoint."""

    permission_level = PermissionLevel.VIEWER
    handler = config_version_detail


class ConfigRollbackView(HandlerAPIView):
    """Config rollback to a specific version endpoint."""

    permission_level = PermissionLevel.ADMIN
    handler = config_rollback


class ConfigCompareView(HandlerAPIView):
    """Config version comparison endpoint."""

    permission_level = PermissionLevel.VIEWER
    handler = config_compare
