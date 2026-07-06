"""
API Tiering Views for Criticality-Based Load Shedding.

Thin HandlerAPIView wrappers. Business logic extracted to
api/handlers/tiering.py (Phase 2b -- 432).
"""

from baldur.api.django.base import HandlerAPIView
from baldur.api.handlers.tiering import (
    tier_definitions_get,
    tier_definitions_update,
    tier_dry_run,
    tier_export,
    tier_import,
    tier_mappings_get,
    tier_mappings_update,
    tier_overrides_get,
    tier_overrides_update,
    tier_reset,
    tier_resolve_lookup,
)
from baldur.interfaces.web_framework import HttpMethod, PermissionLevel

__all__ = [
    "TierDefinitionsView",
    "TierMappingsView",
    "TierOverridesView",
    "TierDryRunView",
    "TierResetView",
    "TierExportView",
    "TierImportView",
    "TierResolveLookupView",
]


class TierDefinitionsView(HandlerAPIView):
    """Tier definitions query and update endpoint."""

    handler_map = {
        HttpMethod.GET: tier_definitions_get,
        HttpMethod.PUT: tier_definitions_update,
    }
    permission_map = {
        HttpMethod.GET: PermissionLevel.VIEWER,
        HttpMethod.PUT: PermissionLevel.ADMIN,
    }


class TierMappingsView(HandlerAPIView):
    """Tier mappings query and update endpoint."""

    handler_map = {
        HttpMethod.GET: tier_mappings_get,
        HttpMethod.PUT: tier_mappings_update,
    }
    permission_map = {
        HttpMethod.GET: PermissionLevel.VIEWER,
        HttpMethod.PUT: PermissionLevel.ADMIN,
    }


class TierOverridesView(HandlerAPIView):
    """Tier overrides query and update endpoint."""

    handler_map = {
        HttpMethod.GET: tier_overrides_get,
        HttpMethod.PUT: tier_overrides_update,
    }
    permission_map = {
        HttpMethod.GET: PermissionLevel.VIEWER,
        HttpMethod.PUT: PermissionLevel.ADMIN,
    }


class TierDryRunView(HandlerAPIView):
    """Tier change simulation endpoint."""

    permission_level = PermissionLevel.OPERATOR
    handler = tier_dry_run


class TierResetView(HandlerAPIView):
    """Reset tier configuration to defaults endpoint."""

    permission_level = PermissionLevel.ADMIN
    handler = tier_reset


class TierExportView(HandlerAPIView):
    """Tier configuration export endpoint."""

    permission_level = PermissionLevel.VIEWER
    handler = tier_export


class TierImportView(HandlerAPIView):
    """Tier configuration import endpoint."""

    permission_level = PermissionLevel.ADMIN
    handler = tier_import


class TierResolveLookupView(HandlerAPIView):
    """Tier resolution lookup for debugging endpoint."""

    permission_level = PermissionLevel.VIEWER
    handler = tier_resolve_lookup
