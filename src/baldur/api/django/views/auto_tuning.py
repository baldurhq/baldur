"""
Auto-Tuning API Endpoints.

Provides REST API for auto-tuning system control:
status, enable/disable, module control, bounds, history, override, metrics.

Handlers extracted to api/handlers/auto_tuning.py (Phase 2b — 432).
"""

from baldur.api.django.base import HandlerAPIView
from baldur.api.handlers.auto_tuning import (
    auto_tuning_bounds_get,
    auto_tuning_bounds_update,
    auto_tuning_disable,
    auto_tuning_enable,
    auto_tuning_history,
    auto_tuning_metrics,
    auto_tuning_module_disable,
    auto_tuning_module_enable,
    auto_tuning_override_clear,
    auto_tuning_override_set,
    auto_tuning_status,
)
from baldur.interfaces.web_framework import HttpMethod, PermissionLevel

__all__ = [
    "AutoTuningStatusView",
    "AutoTuningEnableView",
    "AutoTuningDisableView",
    "AutoTuningModuleEnableView",
    "AutoTuningModuleDisableView",
    "AutoTuningBoundsView",
    "AutoTuningHistoryView",
    "AutoTuningOverrideView",
    "AutoTuningMetricsView",
]


class AutoTuningStatusView(HandlerAPIView):
    """Auto-tuning system status endpoint."""

    permission_level = PermissionLevel.VIEWER
    handler = auto_tuning_status


class AutoTuningEnableView(HandlerAPIView):
    """Enable auto-tuning."""

    permission_level = PermissionLevel.ADMIN
    handler = auto_tuning_enable


class AutoTuningDisableView(HandlerAPIView):
    """Disable auto-tuning."""

    permission_level = PermissionLevel.ADMIN
    handler = auto_tuning_disable


class AutoTuningModuleEnableView(HandlerAPIView):
    """Enable auto-tuning for a specific module."""

    permission_level = PermissionLevel.ADMIN
    handler = auto_tuning_module_enable


class AutoTuningModuleDisableView(HandlerAPIView):
    """Disable auto-tuning for a specific module."""

    permission_level = PermissionLevel.ADMIN
    handler = auto_tuning_module_disable


class AutoTuningBoundsView(HandlerAPIView):
    """Safety bounds configuration (read and update)."""

    handler_map = {
        HttpMethod.GET: auto_tuning_bounds_get,
        HttpMethod.PUT: auto_tuning_bounds_update,
    }
    permission_map = {
        HttpMethod.GET: PermissionLevel.VIEWER,
        HttpMethod.PUT: PermissionLevel.ADMIN,
    }


class AutoTuningHistoryView(HandlerAPIView):
    """Adjustment history list and detail."""

    permission_level = PermissionLevel.VIEWER
    handler = auto_tuning_history


class AutoTuningOverrideView(HandlerAPIView):
    """Manual override (set and clear)."""

    permission_level = PermissionLevel.ADMIN
    handler_map = {
        HttpMethod.POST: auto_tuning_override_set,
        HttpMethod.DELETE: auto_tuning_override_clear,
    }


class AutoTuningMetricsView(HandlerAPIView):
    """Current auto-tuning metrics."""

    permission_level = PermissionLevel.VIEWER
    handler = auto_tuning_metrics
