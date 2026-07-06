"""
Chaos Engineering Safety Views.

Thin HandlerAPIView wrappers delegating to framework-agnostic handlers.
Handlers extracted to api/handlers/chaos_safety.py (Phase 2b — 432).
"""

from baldur.api.django.base import HandlerAPIView
from baldur.api.handlers.chaos_safety import (
    blast_radius_check,
    dry_run_config_get,
    dry_run_config_update,
    kill_all,
    kill_switch_action,
    kill_switch_status,
    safety_check,
    stop_conditions_config_get,
    stop_conditions_config_update,
    ttl_config_get,
    ttl_config_update,
)
from baldur.interfaces.web_framework import HttpMethod, PermissionLevel

__all__ = [
    "KillSwitchView",
    "SafetyCheckView",
    "BlastRadiusCheckView",
    "StopConditionsConfigView",
    "TTLConfigView",
    "DryRunConfigView",
    "KillAllView",
]


class KillSwitchView(HandlerAPIView):
    """Kill switch controls (GET viewer, POST admin)."""

    handler_map = {
        HttpMethod.GET: kill_switch_status,
        HttpMethod.POST: kill_switch_action,
    }
    permission_map = {
        HttpMethod.GET: PermissionLevel.VIEWER,
        HttpMethod.POST: PermissionLevel.ADMIN,
    }


class SafetyCheckView(HandlerAPIView):
    """Run safety check (read-only analysis)."""

    permission_level = PermissionLevel.VIEWER
    handler = safety_check


class BlastRadiusCheckView(HandlerAPIView):
    """Check blast radius policies (read-only analysis)."""

    permission_level = PermissionLevel.VIEWER
    handler = blast_radius_check


class StopConditionsConfigView(HandlerAPIView):
    """Stop conditions configuration (GET viewer, PATCH admin)."""

    handler_map = {
        HttpMethod.GET: stop_conditions_config_get,
        HttpMethod.PATCH: stop_conditions_config_update,
    }
    permission_map = {
        HttpMethod.GET: PermissionLevel.VIEWER,
        HttpMethod.PATCH: PermissionLevel.ADMIN,
    }


class TTLConfigView(HandlerAPIView):
    """TTL (self-expiration) configuration (GET viewer, PATCH admin)."""

    handler_map = {
        HttpMethod.GET: ttl_config_get,
        HttpMethod.PATCH: ttl_config_update,
    }
    permission_map = {
        HttpMethod.GET: PermissionLevel.VIEWER,
        HttpMethod.PATCH: PermissionLevel.ADMIN,
    }


class DryRunConfigView(HandlerAPIView):
    """Dry-run configuration (GET viewer, PATCH admin)."""

    handler_map = {
        HttpMethod.GET: dry_run_config_get,
        HttpMethod.PATCH: dry_run_config_update,
    }
    permission_map = {
        HttpMethod.GET: PermissionLevel.VIEWER,
        HttpMethod.PATCH: PermissionLevel.ADMIN,
    }


class KillAllView(HandlerAPIView):
    """Kill all running experiments (emergency control)."""

    permission_level = PermissionLevel.ADMIN
    handler = kill_all
