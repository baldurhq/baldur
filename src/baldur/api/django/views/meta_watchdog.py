"""
Meta-Watchdog API Endpoints.

Kubernetes liveness probe and status query endpoints.

Handlers extracted to api/handlers/meta_watchdog.py (Phase 2b — 432).
"""

from baldur.api.django.base import HandlerAPIView
from baldur.api.handlers.meta_watchdog import (
    meta_watchdog_force_check,
    meta_watchdog_liveness,
    meta_watchdog_status,
)
from baldur.interfaces.web_framework import PermissionLevel


class MetaWatchdogLivenessView(HandlerAPIView):
    """K8s liveness probe for Meta-Watchdog."""

    permission_level = PermissionLevel.PUBLIC
    handler = meta_watchdog_liveness


class MetaWatchdogStatusView(HandlerAPIView):
    """Meta-Watchdog system status and component health."""

    permission_level = PermissionLevel.PUBLIC
    handler = meta_watchdog_status


class MetaWatchdogForceCheckView(HandlerAPIView):
    """Trigger immediate health check."""

    permission_level = PermissionLevel.PUBLIC
    handler = meta_watchdog_force_check
