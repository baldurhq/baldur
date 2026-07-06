"""Dead-letter queue URL patterns."""

from __future__ import annotations

from django.urls import path

from baldur.api.django.views.dlq import (
    DLQArchiveView,
    DLQCleanupStatsView,
    DLQDetailView,
    DLQForceRedriveView,
    DLQListView,
    DLQPurgeView,
    DLQReplayView,
    DLQResolveView,
    DLQRetryView,
    DLQTestCreateView,
)

urlpatterns = [
    path("dlq/replay/", DLQReplayView.as_view(), name="dlq-replay"),
    path("dlq/cleanup/stats/", DLQCleanupStatsView.as_view(), name="dlq-cleanup-stats"),
    path("dlq/cleanup/archive/", DLQArchiveView.as_view(), name="dlq-cleanup-archive"),
    path("dlq/cleanup/purge/", DLQPurgeView.as_view(), name="dlq-cleanup-purge"),
    path("dlq/list/", DLQListView.as_view(), name="dlq-list"),
    path("dlq/<str:pk>/", DLQDetailView.as_view(), name="dlq-detail"),
    path("dlq/<str:pk>/retry/", DLQRetryView.as_view(), name="dlq-retry"),
    path("dlq/<str:pk>/resolve/", DLQResolveView.as_view(), name="dlq-resolve"),
    path(
        "dlq/<str:pk>/force-redrive/",
        DLQForceRedriveView.as_view(),
        name="dlq-force-redrive",
    ),
    path("dlq/test/create/", DLQTestCreateView.as_view(), name="dlq-test-create"),
]
