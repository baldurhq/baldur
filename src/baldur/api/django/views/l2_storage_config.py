"""
L2 Storage Configuration API Views.

Thin HandlerAPIView wrappers delegating to framework-agnostic handlers.
Handlers extracted to api/handlers/l2_storage.py (Phase 2b — 432).
"""

from baldur.api.django.base import HandlerAPIView
from baldur.api.handlers.l2_storage import (
    l2_storage_config_get,
    l2_storage_config_reset,
    l2_storage_config_update,
)
from baldur.interfaces.web_framework import HttpMethod, PermissionLevel

__all__ = [
    "L2StorageConfigView",
    "L2StorageConfigResetView",
]


class L2StorageConfigView(HandlerAPIView):
    """L2 storage configuration (GET viewer, PUT admin)."""

    handler_map = {
        HttpMethod.GET: l2_storage_config_get,
        HttpMethod.PUT: l2_storage_config_update,
    }
    permission_map = {
        HttpMethod.GET: PermissionLevel.VIEWER,
        HttpMethod.PUT: PermissionLevel.ADMIN,
    }


class L2StorageConfigResetView(HandlerAPIView):
    """Reset L2 storage configuration to defaults."""

    permission_level = PermissionLevel.ADMIN
    handler = l2_storage_config_reset
