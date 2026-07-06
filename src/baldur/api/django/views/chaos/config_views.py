"""
Chaos Engineering Configuration Views.

Thin HandlerAPIView wrappers delegating to framework-agnostic handlers.
Handlers extracted to api/handlers/chaos_config.py (Phase 2b — 432).
"""

from baldur.api.django.base import HandlerAPIView
from baldur.api.handlers.chaos_config import (
    chaos_blast_radius_policy_get,
    chaos_blast_radius_policy_update,
    report_config_get,
    report_config_update,
    safety_guard_config_get,
    safety_guard_config_update,
    scheduler_config_get,
    scheduler_config_update,
)
from baldur.interfaces.web_framework import HttpMethod, PermissionLevel

__all__ = [
    "SafetyGuardConfigView",
    "ChaosBlastRadiusPolicyView",
    "SchedulerConfigView",
    "ReportConfigView",
]


class SafetyGuardConfigView(HandlerAPIView):
    """SafetyGuard configuration (GET viewer, PATCH admin)."""

    handler_map = {
        HttpMethod.GET: safety_guard_config_get,
        HttpMethod.PATCH: safety_guard_config_update,
    }
    permission_map = {
        HttpMethod.GET: PermissionLevel.VIEWER,
        HttpMethod.PATCH: PermissionLevel.ADMIN,
    }


class ChaosBlastRadiusPolicyView(HandlerAPIView):
    """Chaos module BlastRadius policy configuration (GET viewer, PATCH admin)."""

    handler_map = {
        HttpMethod.GET: chaos_blast_radius_policy_get,
        HttpMethod.PATCH: chaos_blast_radius_policy_update,
    }
    permission_map = {
        HttpMethod.GET: PermissionLevel.VIEWER,
        HttpMethod.PATCH: PermissionLevel.ADMIN,
    }


class SchedulerConfigView(HandlerAPIView):
    """ChaosScheduler configuration (GET viewer, PATCH admin)."""

    handler_map = {
        HttpMethod.GET: scheduler_config_get,
        HttpMethod.PATCH: scheduler_config_update,
    }
    permission_map = {
        HttpMethod.GET: PermissionLevel.VIEWER,
        HttpMethod.PATCH: PermissionLevel.ADMIN,
    }


class ReportConfigView(HandlerAPIView):
    """ResilienceReport configuration (GET viewer, PATCH admin)."""

    handler_map = {
        HttpMethod.GET: report_config_get,
        HttpMethod.PATCH: report_config_update,
    }
    permission_map = {
        HttpMethod.GET: PermissionLevel.VIEWER,
        HttpMethod.PATCH: PermissionLevel.ADMIN,
    }
