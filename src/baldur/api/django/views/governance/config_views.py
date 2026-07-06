"""
Governance Config Views.

Thin HandlerAPIView wrappers for governance and L2 storage configuration.
Handlers extracted to api/handlers/governance.py (Phase 2b — 432).
"""

from baldur.api.django.base import HandlerAPIView
from baldur.api.handlers.governance import (
    governance_config_get,
    governance_config_update,
    l2_storage_config_get,
    l2_storage_config_update,
)
from baldur.interfaces.web_framework import HttpMethod, PermissionLevel

__all__ = [
    "GovernanceConfigView",
    "L2StorageConfigManagedView",
]


class GovernanceConfigView(HandlerAPIView):
    """Governance configuration read and update endpoint."""

    handler_map = {
        HttpMethod.GET: governance_config_get,
        HttpMethod.PUT: governance_config_update,
    }
    permission_map = {
        HttpMethod.GET: PermissionLevel.VIEWER,
        HttpMethod.PUT: PermissionLevel.ADMIN,
    }


class L2StorageConfigManagedView(HandlerAPIView):
    """L2 storage configuration read and update endpoint."""

    handler_map = {
        HttpMethod.GET: l2_storage_config_get,
        HttpMethod.PUT: l2_storage_config_update,
    }
    permission_level = PermissionLevel.ADMIN
