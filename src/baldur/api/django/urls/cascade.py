"""Cascade event audit URL patterns (causation tracing + hash-chain integrity)."""

from __future__ import annotations

from django.urls import path

from baldur.api.django.views.cascade import (
    CascadeChainVerifyView,
    CascadeCheckpointView,
    CascadeEventDetailView,
    CascadeEventListView,
    CascadeLoadSheddingStatusView,
    CausationTraceView,
)

urlpatterns = [
    path("cascade/events/", CascadeEventListView.as_view(), name="cascade-event-list"),
    path(
        "cascade/events/<str:cascade_id>/",
        CascadeEventDetailView.as_view(),
        name="cascade-event-detail",
    ),
    path(
        "cascade/verify/", CascadeChainVerifyView.as_view(), name="cascade-chain-verify"
    ),
    path(
        "cascade/trace/<str:event_id>/",
        CausationTraceView.as_view(),
        name="cascade-causation-trace",
    ),
    path(
        "cascade/checkpoint/",
        CascadeCheckpointView.as_view(),
        name="cascade-checkpoint",
    ),
    path(
        "cascade/load-shedding/status/",
        CascadeLoadSheddingStatusView.as_view(),
        name="cascade-load-shedding-status",
    ),
]
