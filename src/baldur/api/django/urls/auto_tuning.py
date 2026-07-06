"""Auto-tuning URL patterns (status, enable/disable, bounds, history, override, metrics)."""

from __future__ import annotations

from django.urls import path

from baldur.api.django.views.auto_tuning import (
    AutoTuningBoundsView,
    AutoTuningDisableView,
    AutoTuningEnableView,
    AutoTuningHistoryView,
    AutoTuningMetricsView,
    AutoTuningModuleDisableView,
    AutoTuningModuleEnableView,
    AutoTuningOverrideView,
    AutoTuningStatusView,
)

urlpatterns = [
    # Status
    path(
        "auto-tuning/status/", AutoTuningStatusView.as_view(), name="auto-tuning-status"
    ),
    # Enable/Disable
    path(
        "auto-tuning/enable/", AutoTuningEnableView.as_view(), name="auto-tuning-enable"
    ),
    path(
        "auto-tuning/disable/",
        AutoTuningDisableView.as_view(),
        name="auto-tuning-disable",
    ),
    # Module Control
    path(
        "auto-tuning/<str:module>/enable/",
        AutoTuningModuleEnableView.as_view(),
        name="auto-tuning-module-enable",
    ),
    path(
        "auto-tuning/<str:module>/disable/",
        AutoTuningModuleDisableView.as_view(),
        name="auto-tuning-module-disable",
    ),
    # Bounds
    path(
        "auto-tuning/bounds/", AutoTuningBoundsView.as_view(), name="auto-tuning-bounds"
    ),
    # History
    path(
        "auto-tuning/history/",
        AutoTuningHistoryView.as_view(),
        name="auto-tuning-history",
    ),
    path(
        "auto-tuning/history/<str:history_id>/",
        AutoTuningHistoryView.as_view(),
        name="auto-tuning-history-detail",
    ),
    # Override
    path(
        "auto-tuning/override/",
        AutoTuningOverrideView.as_view(),
        name="auto-tuning-override",
    ),
    path(
        "auto-tuning/override/<str:parameter>/",
        AutoTuningOverrideView.as_view(),
        name="auto-tuning-override-clear",
    ),
    # Metrics
    path(
        "auto-tuning/metrics/",
        AutoTuningMetricsView.as_view(),
        name="auto-tuning-metrics",
    ),
]
