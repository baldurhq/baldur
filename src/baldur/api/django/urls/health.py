"""Health, metrics, and Prometheus URL patterns + error-budget gate health."""

from __future__ import annotations

from django.urls import path

from baldur.api.django.views import (
    BaldurHealthView,
    BaldurMetricsView,
    ConnectionPoolHealthView,
    LivenessView,
    ReadinessView,
    simple_health_ping_view,
)
from baldur.api.django.views.health import (
    ErrorBudgetGateConfigView,
    ErrorBudgetGateHealthView,
    ErrorBudgetGateResetView,
    PrometheusTextMetricsView,
)

urlpatterns = [
    path("health/", BaldurHealthView.as_view(), name="health"),
    path("health/live/", LivenessView.as_view(), name="health-liveness"),
    path("health/ready/", ReadinessView.as_view(), name="health-readiness"),
    path("health/pool/", ConnectionPoolHealthView.as_view(), name="health-pool"),
    path("health/ping/", simple_health_ping_view, name="health-ping"),
    path("health/gate/", ErrorBudgetGateHealthView.as_view(), name="health-gate"),
    path("metrics/", BaldurMetricsView.as_view(), name="metrics"),
    path("prometheus/", PrometheusTextMetricsView.as_view(), name="prometheus-metrics"),
    path("config/gate/", ErrorBudgetGateConfigView.as_view(), name="config-gate"),
    path("gate/reset/", ErrorBudgetGateResetView.as_view(), name="gate-reset"),
]
