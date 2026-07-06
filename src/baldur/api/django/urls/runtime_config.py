"""Runtime configuration URL patterns (per-section GET/PUT)."""

from __future__ import annotations

from django.urls import path

from baldur.api.django.views.config import (
    AllConfigView,
    CancelPendingChangeView,
    CircuitBreakerConfigView,
    DLQConfigView,
    ErrorBudgetConfigView,
    ForensicConfigView,
    IdempotencyConfigView,
    LoggingConfigView,
    MetricsConfigView,
    NotificationConfigView,
    PendingChangesView,
    RateLimitConfigView,
    ReplayAutomationConfigView,
    ResetConfigView,
    RetryConfigView,
    SecurityConfigView,
    SLAConfigView,
    SLOConfigView,
)
from baldur.api.django.views.drift_threshold import (
    DriftThresholdConfigView,
    DriftThresholdResetView,
)

urlpatterns = [
    path("config/", AllConfigView.as_view(), name="config-all"),
    path("config/reset/", ResetConfigView.as_view(), name="config-reset"),
    path("config/pending/", PendingChangesView.as_view(), name="config-pending"),
    path(
        "config/pending/<str:pending_id>/cancel/",
        CancelPendingChangeView.as_view(),
        name="config-pending-cancel",
    ),
    path(
        "config/circuit-breaker/",
        CircuitBreakerConfigView.as_view(),
        name="config-circuit-breaker",
    ),
    path("config/dlq/", DLQConfigView.as_view(), name="config-dlq"),
    path("config/retry/", RetryConfigView.as_view(), name="config-retry"),
    path("config/sla/", SLAConfigView.as_view(), name="config-sla"),
    path("config/slo/", SLOConfigView.as_view(), name="config-slo"),
    path("config/rate-limit/", RateLimitConfigView.as_view(), name="config-rate-limit"),
    path("config/security/", SecurityConfigView.as_view(), name="config-security"),
    path(
        "config/idempotency/",
        IdempotencyConfigView.as_view(),
        name="config-idempotency",
    ),
    path(
        "config/notification/",
        NotificationConfigView.as_view(),
        name="config-notification",
    ),
    path("config/forensic/", ForensicConfigView.as_view(), name="config-forensic"),
    path("config/logging/", LoggingConfigView.as_view(), name="config-logging"),
    path("config/metrics/", MetricsConfigView.as_view(), name="config-metrics"),
    path(
        "config/error-budget/",
        ErrorBudgetConfigView.as_view(),
        name="config-error-budget",
    ),
    # Replay Automation Configuration (DLQ Replay Tracks)
    path(
        "config/replay-automation/",
        ReplayAutomationConfigView.as_view(),
        name="config-replay-automation",
    ),
    # Drift Threshold Configuration (Metric Collection Strategy)
    path(
        "config/drift-thresholds/",
        DriftThresholdConfigView.as_view(),
        name="config-drift-thresholds",
    ),
    path(
        "config/drift-thresholds/reset/",
        DriftThresholdResetView.as_view(),
        name="config-drift-thresholds-reset",
    ),
]
