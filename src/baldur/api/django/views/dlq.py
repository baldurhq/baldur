"""
Baldur DLQ (Dead Letter Queue) Views — thin HandlerAPIView wrappers.

Business logic lives in api/handlers/dlq.py. Views here only declare
permission level + bind the handler so Django's DRF pipeline continues
to provide auth/throttling/content-negotiation.

Endpoints:
- POST /api/baldur/dlq/replay/              - Trigger DLQ replay
- GET  /api/baldur/dlq/cleanup/stats/       - Cleanup statistics
- POST /api/baldur/dlq/cleanup/archive/     - Archive old resolved entries
- POST /api/baldur/dlq/cleanup/purge/       - Destructive purge (admin)
- GET  /api/baldur/dlq/list/                - Paginated list
- GET  /api/baldur/dlq/<pk>/                - Single entry detail
- POST /api/baldur/dlq/<pk>/retry/          - Retry a single entry
- POST /api/baldur/dlq/<pk>/resolve/        - Manual resolve
- POST /api/baldur/dlq/<pk>/force-redrive/  - Force-redrive an at-cap entry (admin)
- POST /api/baldur/dlq/test/create/         - Test entry (admin; debug-only)
"""

from __future__ import annotations

from baldur.api.django.base import HandlerAPIView
from baldur.api.handlers.dlq import (
    dlq_cleanup_archive,
    dlq_cleanup_purge,
    dlq_cleanup_stats,
    dlq_detail,
    dlq_force_redrive,
    dlq_list,
    dlq_replay,
    dlq_resolve,
    dlq_retry,
    dlq_test_create,
)
from baldur.interfaces.web_framework import PermissionLevel

__all__ = [
    "DLQReplayView",
    "DLQCleanupStatsView",
    "DLQArchiveView",
    "DLQPurgeView",
    "DLQListView",
    "DLQDetailView",
    "DLQRetryView",
    "DLQResolveView",
    "DLQForceRedriveView",
    "DLQTestCreateView",
]


class DLQReplayView(HandlerAPIView):
    """POST /api/baldur/dlq/replay/ — trigger DLQ replay (operator)."""

    permission_level = PermissionLevel.OPERATOR
    handler = dlq_replay


class DLQCleanupStatsView(HandlerAPIView):
    """GET /api/baldur/dlq/cleanup/stats/ — cleanup statistics (viewer+)."""

    permission_level = PermissionLevel.VIEWER
    handler = dlq_cleanup_stats


class DLQArchiveView(HandlerAPIView):
    """POST /api/baldur/dlq/cleanup/archive/ — archive old resolved (operator)."""

    permission_level = PermissionLevel.OPERATOR
    handler = dlq_cleanup_archive


class DLQPurgeView(HandlerAPIView):
    """POST /api/baldur/dlq/cleanup/purge/ — destructive purge (admin)."""

    permission_level = PermissionLevel.ADMIN
    handler = dlq_cleanup_purge


class DLQListView(HandlerAPIView):
    """GET /api/baldur/dlq/list/ — paginated list (viewer+)."""

    permission_level = PermissionLevel.VIEWER
    handler = dlq_list


class DLQDetailView(HandlerAPIView):
    """GET /api/baldur/dlq/<pk>/ — single entry detail (viewer+)."""

    permission_level = PermissionLevel.VIEWER
    handler = dlq_detail


class DLQRetryView(HandlerAPIView):
    """POST /api/baldur/dlq/<pk>/retry/ — retry a single entry (operator)."""

    permission_level = PermissionLevel.OPERATOR
    handler = dlq_retry


class DLQResolveView(HandlerAPIView):
    """POST /api/baldur/dlq/<pk>/resolve/ — manual resolve (operator)."""

    permission_level = PermissionLevel.OPERATOR
    handler = dlq_resolve


class DLQForceRedriveView(HandlerAPIView):
    """POST /api/baldur/dlq/<pk>/force-redrive/ — force-redrive an at-cap entry.

    Privileged cap-override bound at ADMIN (strictly above the OPERATOR-level
    normal retry/resolve), mirroring the destructive-purge ADMIN precedent.
    """

    permission_level = PermissionLevel.ADMIN
    handler = dlq_force_redrive


class DLQTestCreateView(HandlerAPIView):
    """POST /api/baldur/dlq/test/create/ — create test entry (admin)."""

    permission_level = PermissionLevel.ADMIN
    handler = dlq_test_create
