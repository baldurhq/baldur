"""
Grafana Alert Webhook Endpoints.

Receives Grafana Alerting webhooks and forwards to UnifiedNotificationManager.

Handlers extracted to api/handlers/grafana_webhook.py (Phase 2b — 432).
"""

from baldur.api.django.base import HandlerAPIView
from baldur.api.handlers.grafana_webhook import (
    grafana_alert_webhook,
    grafana_webhook_test_get,
    grafana_webhook_test_post,
)
from baldur.interfaces.web_framework import HttpMethod, PermissionLevel


class GrafanaAlertWebhookView(HandlerAPIView):
    """Receive Grafana Alert webhooks."""

    permission_level = PermissionLevel.PUBLIC
    authentication_classes: list = []
    handler = grafana_alert_webhook


class GrafanaAlertWebhookTestView(HandlerAPIView):
    """Webhook connection test endpoint."""

    permission_level = PermissionLevel.PUBLIC
    authentication_classes: list = []
    handler_map = {
        HttpMethod.GET: grafana_webhook_test_get,
        HttpMethod.POST: grafana_webhook_test_post,
    }
