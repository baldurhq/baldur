"""
Canary Rollout API Views.

Thin HandlerAPIView wrappers for canary rollout management.
Handlers extracted to api/handlers/canary.py (Phase 2b — 432).
"""

from baldur.api.django.base import HandlerAPIView
from baldur.api.handlers.canary import (
    canary_panic_rollback,
    canary_rollout_action,
    canary_rollout_create,
    canary_rollout_detail,
    canary_rollout_history,
    canary_rollout_list,
    canary_rollout_metrics,
)
from baldur.interfaces.web_framework import HttpMethod, PermissionLevel


class CanaryRolloutListView(HandlerAPIView):
    """Active rollout list and create endpoint."""

    handler_map = {
        HttpMethod.GET: canary_rollout_list,
        HttpMethod.POST: canary_rollout_create,
    }
    permission_map = {
        HttpMethod.GET: PermissionLevel.VIEWER,
        HttpMethod.POST: PermissionLevel.ADMIN,
    }


class CanaryRolloutDetailView(HandlerAPIView):
    """Rollout detail endpoint."""

    handler = canary_rollout_detail
    permission_level = PermissionLevel.VIEWER


class CanaryRolloutActionView(HandlerAPIView):
    """Rollout action (start/promote/rollback/pause/resume/cancel) endpoint."""

    handler = canary_rollout_action
    permission_level = PermissionLevel.ADMIN


class CanaryPanicRollbackView(HandlerAPIView):
    """Panic rollback all active rollouts endpoint."""

    handler = canary_panic_rollback
    permission_level = PermissionLevel.ADMIN


class CanaryMetricsView(HandlerAPIView):
    """Rollout metrics endpoint."""

    handler = canary_rollout_metrics
    permission_level = PermissionLevel.VIEWER


class CanaryHistoryView(HandlerAPIView):
    """Rollout history endpoint."""

    handler = canary_rollout_history
    permission_level = PermissionLevel.VIEWER
