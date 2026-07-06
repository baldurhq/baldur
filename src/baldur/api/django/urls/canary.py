"""Canary rollout URL patterns (gradual config change deployment).

Reference: docs/baldur/middleware_system/71_CANARY_CONFIG_ROLLOUT.md
"""

from __future__ import annotations

from django.urls import path

from baldur.api.django.views.canary import (
    CanaryHistoryView,
    CanaryMetricsView,
    CanaryPanicRollbackView,
    CanaryRolloutActionView,
    CanaryRolloutDetailView,
    CanaryRolloutListView,
)

urlpatterns = [
    # List & Create
    path(
        "canary/rollouts/", CanaryRolloutListView.as_view(), name="canary-rollout-list"
    ),
    # History (completed rollouts)
    path("canary/history/", CanaryHistoryView.as_view(), name="canary-history"),
    # Panic Rollback (all active rollouts)
    path(
        "canary/panic-rollback/",
        CanaryPanicRollbackView.as_view(),
        name="canary-panic-rollback",
    ),
    # Detail
    path(
        "canary/rollouts/<str:rollout_id>/",
        CanaryRolloutDetailView.as_view(),
        name="canary-rollout-detail",
    ),
    # Metrics
    path(
        "canary/rollouts/<str:rollout_id>/metrics/",
        CanaryMetricsView.as_view(),
        name="canary-rollout-metrics",
    ),
    # Actions: start, promote, rollback, pause, resume, cancel
    path(
        "canary/rollouts/<str:rollout_id>/<str:action>/",
        CanaryRolloutActionView.as_view(),
        name="canary-rollout-action",
    ),
]
