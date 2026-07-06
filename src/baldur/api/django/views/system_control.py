"""
System Control API - Global Kill Switch (thin HandlerAPIView wrappers).

Business logic lives in api/handlers/system_control.py. Service-layer
re-exports are retained for backward compatibility with callers that
import SystemControlManager / helpers from this module.

Endpoints:
- GET  /api/baldur/system/status/        - Get system status
- POST /api/baldur/system/enable/        - Enable baldur
- POST /api/baldur/system/disable/       - Disable baldur (Kill Switch)
- POST /api/baldur/system/dry-run/enable/  - Enable dry run mode
- POST /api/baldur/system/dry-run/disable/ - Disable dry run mode
"""

from __future__ import annotations

from baldur.api.django.base import HandlerAPIView
from baldur.api.handlers.system_control import (
    dry_run_disable,
    dry_run_enable,
    system_disable,
    system_enable,
    system_status,
)
from baldur.interfaces.web_framework import PermissionLevel
from baldur.services.system_control import (
    SystemControlManager,
    SystemState,
    get_system_control,
    is_baldur_enabled,
    is_dry_run,
)

__all__ = [
    # Service re-exports (backward compatibility)
    "SystemControlManager",
    "SystemState",
    "get_system_control",
    "is_baldur_enabled",
    "is_dry_run",
    # Views
    "SystemStatusView",
    "SystemEnableView",
    "SystemDisableView",
    "DryRunEnableView",
    "DryRunDisableView",
]


class SystemStatusView(HandlerAPIView):
    """GET /api/baldur/system/status/ — system status (viewer+)."""

    permission_level = PermissionLevel.VIEWER
    handler = system_status


class SystemEnableView(HandlerAPIView):
    """POST /api/baldur/system/enable/ — re-enable baldur (admin)."""

    permission_level = PermissionLevel.ADMIN
    handler = system_enable


class SystemDisableView(HandlerAPIView):
    """POST /api/baldur/system/disable/ — kill switch (admin)."""

    permission_level = PermissionLevel.ADMIN
    handler = system_disable


class DryRunEnableView(HandlerAPIView):
    """POST /api/baldur/system/dry-run/enable/ — dry run on (admin)."""

    permission_level = PermissionLevel.ADMIN
    handler = dry_run_enable


class DryRunDisableView(HandlerAPIView):
    """POST /api/baldur/system/dry-run/disable/ — dry run off (admin)."""

    permission_level = PermissionLevel.ADMIN
    handler = dry_run_disable
