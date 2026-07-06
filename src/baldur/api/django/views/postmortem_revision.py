"""
Postmortem Revision API Views.

Thin HandlerAPIView wrappers. Business logic extracted to
api/handlers/postmortem_revision.py (Phase 2b -- 432).
"""

from baldur.api.django.base import HandlerAPIView
from baldur.api.handlers.postmortem_revision import (
    postmortem_revision_compare,
    postmortem_revision_create,
    postmortem_revision_detail,
    postmortem_revision_list,
    postmortem_seal,
    postmortem_unseal,
)
from baldur.interfaces.web_framework import HttpMethod, PermissionLevel

__all__ = [
    "PostmortemRevisionListView",
    "PostmortemRevisionDetailView",
    "PostmortemRevisionCompareView",
    "PostmortemSealView",
]


class PostmortemRevisionListView(HandlerAPIView):
    """Postmortem revision list and creation endpoint."""

    handler_map = {
        HttpMethod.GET: postmortem_revision_list,
        HttpMethod.POST: postmortem_revision_create,
    }
    permission_map = {
        HttpMethod.GET: PermissionLevel.VIEWER,
        HttpMethod.POST: PermissionLevel.OPERATOR,
    }


class PostmortemRevisionDetailView(HandlerAPIView):
    """Specific revision detail endpoint."""

    permission_level = PermissionLevel.VIEWER
    handler = postmortem_revision_detail


class PostmortemRevisionCompareView(HandlerAPIView):
    """Revision comparison endpoint."""

    permission_level = PermissionLevel.VIEWER
    handler = postmortem_revision_compare


class PostmortemSealView(HandlerAPIView):
    """Postmortem seal and unseal endpoint."""

    handler_map = {
        HttpMethod.POST: postmortem_seal,
        HttpMethod.DELETE: postmortem_unseal,
    }
    permission_map = {
        HttpMethod.POST: PermissionLevel.ADMIN,
        HttpMethod.DELETE: PermissionLevel.ADMIN,
    }
