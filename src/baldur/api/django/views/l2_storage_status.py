"""
L2 Storage Status and Health API Views.

Thin HandlerAPIView wrappers delegating to framework-agnostic handlers.
Handlers extracted to api/handlers/l2_storage.py (Phase 2b — 432).
"""

from baldur.api.django.base import HandlerAPIView
from baldur.api.handlers.l2_storage import (
    l2_storage_health,
    l2_storage_health_reset,
    l2_storage_metrics,
    l2_storage_status,
    l2_storage_sync_from_l2,
    l2_storage_sync_to_l2,
)
from baldur.interfaces.web_framework import PermissionLevel

__all__ = [
    "L2StorageStatusView",
    "L2StorageHealthView",
    "L2StorageHealthResetView",
    "L2StorageSyncFromL2View",
    "L2StorageSyncToL2View",
    "L2StorageMetricsView",
]


class L2StorageStatusView(HandlerAPIView):
    """L2 storage status endpoint."""

    permission_level = PermissionLevel.VIEWER
    handler = l2_storage_status


class L2StorageHealthView(HandlerAPIView):
    """L2 storage health status endpoint."""

    permission_level = PermissionLevel.VIEWER
    handler = l2_storage_health


class L2StorageHealthResetView(HandlerAPIView):
    """Reset L2 storage health status."""

    permission_level = PermissionLevel.ADMIN
    handler = l2_storage_health_reset


class L2StorageSyncFromL2View(HandlerAPIView):
    """Force sync from L2 to L1."""

    permission_level = PermissionLevel.ADMIN
    handler = l2_storage_sync_from_l2


class L2StorageSyncToL2View(HandlerAPIView):
    """Force sync from L1 to L2."""

    permission_level = PermissionLevel.ADMIN
    handler = l2_storage_sync_to_l2


class L2StorageMetricsView(HandlerAPIView):
    """L2 storage metrics endpoint."""

    permission_level = PermissionLevel.VIEWER
    handler = l2_storage_metrics
