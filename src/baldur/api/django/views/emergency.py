"""
Emergency Mode API Views — thin HandlerAPIView wrappers.

Business logic lives in api/handlers/emergency.py.

Endpoints:
- GET  /api/baldur/emergency/status/             - Current state
- POST /api/baldur/emergency/trigger/            - Manual activation (admin)
- POST /api/baldur/emergency/release/            - Deactivate (admin)
- POST /api/baldur/emergency/gradual-recovery/   - Start recovery (admin)
- POST /api/baldur/emergency/stop-recovery/      - Stop recovery (admin)
- GET  /api/baldur/emergency/history/            - Change history
- GET/PUT /api/baldur/emergency/config/          - Recovery gate config
- GET  /api/baldur/emergency/levels/             - Level definitions
"""

from __future__ import annotations

from baldur.api.django.base import HandlerAPIView
from baldur.api.handlers.emergency import (
    emergency_config_get,
    emergency_config_update,
    emergency_history,
    emergency_levels,
    emergency_release,
    emergency_status,
    emergency_trigger,
    gradual_recovery_start,
    gradual_recovery_stop,
)
from baldur.interfaces.web_framework import HttpMethod, PermissionLevel

__all__ = [
    "EmergencyStatusView",
    "EmergencyTriggerView",
    "EmergencyReleaseView",
    "GradualRecoveryStartView",
    "GradualRecoveryStopView",
    "EmergencyHistoryView",
    "EmergencyConfigView",
    "EmergencyLevelsView",
]


class EmergencyStatusView(HandlerAPIView):
    """GET /api/baldur/emergency/status/ — current state (viewer+)."""

    permission_level = PermissionLevel.VIEWER
    handler = emergency_status


class EmergencyTriggerView(HandlerAPIView):
    """POST /api/baldur/emergency/trigger/ — manual activation (admin)."""

    permission_level = PermissionLevel.ADMIN
    handler = emergency_trigger


class EmergencyReleaseView(HandlerAPIView):
    """POST /api/baldur/emergency/release/ — deactivate (admin)."""

    permission_level = PermissionLevel.ADMIN
    handler = emergency_release


class GradualRecoveryStartView(HandlerAPIView):
    """POST /api/baldur/emergency/gradual-recovery/ — start recovery (admin)."""

    permission_level = PermissionLevel.ADMIN
    handler = gradual_recovery_start


class GradualRecoveryStopView(HandlerAPIView):
    """POST /api/baldur/emergency/stop-recovery/ — stop recovery (admin)."""

    permission_level = PermissionLevel.ADMIN
    handler = gradual_recovery_stop


class EmergencyHistoryView(HandlerAPIView):
    """GET /api/baldur/emergency/history/ — change history (viewer+)."""

    permission_level = PermissionLevel.VIEWER
    handler = emergency_history


class EmergencyConfigView(HandlerAPIView):
    """GET/PUT /api/baldur/emergency/config/ — recovery gate config."""

    handler_map = {
        HttpMethod.GET: emergency_config_get,
        HttpMethod.PUT: emergency_config_update,
    }
    permission_map = {
        HttpMethod.GET: PermissionLevel.ADMIN,
        HttpMethod.PUT: PermissionLevel.ADMIN,
    }
    permission_level = PermissionLevel.ADMIN


class EmergencyLevelsView(HandlerAPIView):
    """GET /api/baldur/emergency/levels/ — level definitions (viewer+)."""

    permission_level = PermissionLevel.VIEWER
    handler = emergency_levels
