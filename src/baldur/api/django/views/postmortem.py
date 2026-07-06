"""
Post-mortem API Views.

Thin HandlerAPIView wrappers. Business logic extracted to
api/handlers/postmortem.py (Phase 2b -- 432).
"""

from baldur.api.django.base import HandlerAPIView
from baldur.api.handlers.postmortem import (
    postmortem_generate,
    postmortem_incident_detail,
    postmortem_incidents_list,
)
from baldur.interfaces.web_framework import PermissionLevel

__all__ = [
    "PostmortemGeneratorView",
    "GetHealingIncidentsView",
    "PostmortemDetailView",
]


class PostmortemGeneratorView(HandlerAPIView):
    """Post-mortem report generation endpoint."""

    permission_level = PermissionLevel.AUTHENTICATED
    handler = postmortem_generate


class GetHealingIncidentsView(HandlerAPIView):
    """Post-mortem incident list endpoint."""

    permission_level = PermissionLevel.AUTHENTICATED
    handler = postmortem_incidents_list


class PostmortemDetailView(HandlerAPIView):
    """Single post-mortem incident detail endpoint."""

    permission_level = PermissionLevel.AUTHENTICATED
    handler = postmortem_incident_detail
