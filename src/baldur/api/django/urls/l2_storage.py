"""L2 storage resilience URL patterns (config, status, shadow log, drift, sync, metrics)."""

from __future__ import annotations

from django.urls import path

from baldur.api.django.views.l2_storage_config import (
    L2StorageConfigResetView,
    L2StorageConfigView,
)
from baldur.api.django.views.l2_storage_drift import (
    DriftReconciliationHistoryView,
    DriftReconciliationServiceView,
    DriftReconciliationStatsView,
    DriftReconciliationTriggerView,
)
from baldur.api.django.views.l2_storage_shadow_log import (
    ShadowLogAnalyzeView,
    ShadowLogByServiceView,
    ShadowLogClearView,
    ShadowLogListView,
    ShadowLogReplayView,
    ShadowLogStatsView,
)
from baldur.api.django.views.l2_storage_status import (
    L2StorageHealthResetView,
    L2StorageHealthView,
    L2StorageMetricsView,
    L2StorageStatusView,
    L2StorageSyncFromL2View,
    L2StorageSyncToL2View,
)

urlpatterns = [
    # Configuration
    path("l2-storage/config/", L2StorageConfigView.as_view(), name="l2-storage-config"),
    path(
        "l2-storage/config/reset/",
        L2StorageConfigResetView.as_view(),
        name="l2-storage-config-reset",
    ),
    # Status & Health
    path("l2-storage/status/", L2StorageStatusView.as_view(), name="l2-storage-status"),
    path("l2-storage/health/", L2StorageHealthView.as_view(), name="l2-storage-health"),
    path(
        "l2-storage/health/reset/",
        L2StorageHealthResetView.as_view(),
        name="l2-storage-health-reset",
    ),
    # Shadow Log
    path(
        "l2-storage/shadow-log/",
        ShadowLogListView.as_view(),
        name="l2-storage-shadow-log",
    ),
    path(
        "l2-storage/shadow-log/stats/",
        ShadowLogStatsView.as_view(),
        name="l2-storage-shadow-log-stats",
    ),
    path(
        "l2-storage/shadow-log/clear/",
        ShadowLogClearView.as_view(),
        name="l2-storage-shadow-log-clear",
    ),
    path(
        "l2-storage/shadow-log/analyze/",
        ShadowLogAnalyzeView.as_view(),
        name="l2-storage-shadow-log-analyze",
    ),
    path(
        "l2-storage/shadow-log/replay/",
        ShadowLogReplayView.as_view(),
        name="l2-storage-shadow-log-replay",
    ),
    path(
        "l2-storage/shadow-log/service/<str:service_name>/",
        ShadowLogByServiceView.as_view(),
        name="l2-storage-shadow-log-by-service",
    ),
    # Sync Operations
    path(
        "l2-storage/sync/from-l2/",
        L2StorageSyncFromL2View.as_view(),
        name="l2-storage-sync-from-l2",
    ),
    path(
        "l2-storage/sync/to-l2/",
        L2StorageSyncToL2View.as_view(),
        name="l2-storage-sync-to-l2",
    ),
    # Drift Reconciliation
    path(
        "l2-storage/drift/stats/",
        DriftReconciliationStatsView.as_view(),
        name="l2-storage-drift-stats",
    ),
    path(
        "l2-storage/drift/history/",
        DriftReconciliationHistoryView.as_view(),
        name="l2-storage-drift-history",
    ),
    path(
        "l2-storage/drift/reconcile/",
        DriftReconciliationTriggerView.as_view(),
        name="l2-storage-drift-reconcile",
    ),
    path(
        "l2-storage/drift/reconcile/<str:service_name>/",
        DriftReconciliationServiceView.as_view(),
        name="l2-storage-drift-reconcile-service",
    ),
    # Metrics
    path(
        "l2-storage/metrics/", L2StorageMetricsView.as_view(), name="l2-storage-metrics"
    ),
]
