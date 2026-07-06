"""
DLQ Compressed Entries API Views.

Thin HandlerAPIView wrappers. Business logic extracted to
api/handlers/dlq_compressed.py (Phase 2b -- 432).
"""

from baldur.api.django.base import HandlerAPIView
from baldur.api.handlers.dlq_compressed import (
    dlq_compressed_detail,
    dlq_compressed_list,
    dlq_compressed_summary,
)
from baldur.interfaces.web_framework import PermissionLevel

__all__ = [
    "DLQCompressedListView",
    "DLQCompressedDetailView",
    "DLQCompressedSummaryView",
]


class DLQCompressedListView(HandlerAPIView):
    """List compressed DLQ entries with optional filtering."""

    permission_level = PermissionLevel.VIEWER
    handler = dlq_compressed_list


class DLQCompressedDetailView(HandlerAPIView):
    """Get a specific compressed entry by ID."""

    permission_level = PermissionLevel.VIEWER
    handler = dlq_compressed_detail


class DLQCompressedSummaryView(HandlerAPIView):
    """Aggregate statistics of compressed entries."""

    permission_level = PermissionLevel.VIEWER
    handler = dlq_compressed_summary
