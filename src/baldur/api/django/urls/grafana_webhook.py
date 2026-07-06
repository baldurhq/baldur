"""Grafana alert webhook URL patterns.

Receives Alerts from Grafana Alerting and forwards to UnifiedNotificationManager.
"""

from __future__ import annotations

from django.urls import path

from baldur.api.django.views.grafana_webhook import (
    GrafanaAlertWebhookTestView,
    GrafanaAlertWebhookView,
)

urlpatterns = [
    # Grafana alert webhook receiver (CSRF exempt — external system integration)
    path(
        "webhook/grafana/alert/",
        GrafanaAlertWebhookView.as_view(),
        name="grafana-alert-webhook",
    ),
    # Grafana webhook test endpoint
    path(
        "webhook/grafana/test/",
        GrafanaAlertWebhookTestView.as_view(),
        name="grafana-alert-webhook-test",
    ),
]
