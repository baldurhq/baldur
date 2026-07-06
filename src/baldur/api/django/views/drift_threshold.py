"""
Drift Threshold Configuration API Endpoints.

REST API for drift threshold management via RuntimeConfigManager.

Handlers extracted to api/handlers/drift_threshold.py (Phase 2b — 432).
"""

from baldur.api.django.base import HandlerAPIView
from baldur.api.handlers.drift_threshold import (
    drift_threshold_config_get,
    drift_threshold_config_update,
    drift_threshold_reset,
)
from baldur.interfaces.web_framework import HttpMethod, PermissionLevel

__all__ = [
    "DriftThresholdConfigView",
    "DriftThresholdResetView",
]


class DriftThresholdConfigView(HandlerAPIView):
    """Drift threshold configuration (read and update)."""

    handler_map = {
        HttpMethod.GET: drift_threshold_config_get,
        HttpMethod.PUT: drift_threshold_config_update,
    }
    permission_map = {
        HttpMethod.GET: PermissionLevel.VIEWER,
        HttpMethod.PUT: PermissionLevel.ADMIN,
    }


class DriftThresholdResetView(HandlerAPIView):
    """Reset drift thresholds to defaults."""

    permission_level = PermissionLevel.ADMIN
    handler = drift_threshold_reset
