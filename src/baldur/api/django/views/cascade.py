"""
Cascade Event Audit API Views.

Thin HandlerAPIView wrappers. Business logic extracted to
api/handlers/cascade.py (Phase 2b -- 432).
"""

from baldur.api.django.base import HandlerAPIView
from baldur.api.handlers.cascade import (
    cascade_chain_verify,
    cascade_checkpoint_create,
    cascade_checkpoint_get,
    cascade_event_detail,
    cascade_event_list,
    cascade_load_shedding_status,
    causation_trace,
)
from baldur.interfaces.web_framework import HttpMethod, PermissionLevel

__all__ = [
    "CascadeEventListView",
    "CascadeEventDetailView",
    "CascadeChainVerifyView",
    "CausationTraceView",
    "CascadeCheckpointView",
    "CascadeLoadSheddingStatusView",
]


class CascadeEventListView(HandlerAPIView):
    """Cascade event list endpoint."""

    permission_level = PermissionLevel.VIEWER
    handler = cascade_event_list


class CascadeEventDetailView(HandlerAPIView):
    """Cascade event detail endpoint."""

    permission_level = PermissionLevel.VIEWER
    handler = cascade_event_detail


class CascadeChainVerifyView(HandlerAPIView):
    """Hash chain integrity verification endpoint."""

    permission_level = PermissionLevel.ADMIN
    handler = cascade_chain_verify


class CausationTraceView(HandlerAPIView):
    """Causation trace for a specific event."""

    permission_level = PermissionLevel.VIEWER
    handler = causation_trace


class CascadeCheckpointView(HandlerAPIView):
    """Checkpoint query and creation endpoint."""

    handler_map = {
        HttpMethod.GET: cascade_checkpoint_get,
        HttpMethod.POST: cascade_checkpoint_create,
    }
    permission_map = {
        HttpMethod.GET: PermissionLevel.ADMIN,
        HttpMethod.POST: PermissionLevel.ADMIN,
    }


class CascadeLoadSheddingStatusView(HandlerAPIView):
    """Load shedding status endpoint."""

    permission_level = PermissionLevel.VIEWER
    handler = cascade_load_shedding_status
