"""
Framework-agnostic Grafana Alert Webhook handlers.

Extracted from api/django/views/grafana_webhook.py (Phase 2b).

Endpoints:
    POST /webhook/grafana/alert  Receive Grafana Alert webhooks
    GET  /webhook/grafana/test   Webhook connection test
    POST /webhook/grafana/test   Test webhook receive
"""

from __future__ import annotations

from typing import Any

import structlog

from baldur.interfaces.web_framework import RequestContext, ResponseContext
from baldur.utils.time import utc_now

logger = structlog.get_logger()

__all__ = [
    "grafana_alert_webhook",
    "grafana_webhook_test_get",
    "grafana_webhook_test_post",
]


def _map_severity_to_priority(severity: str):
    from baldur.models.notification import NotificationPriority

    severity_mapping = {
        "critical": NotificationPriority.CRITICAL,
        "high": NotificationPriority.HIGH,
        "warning": NotificationPriority.HIGH,
        "medium": NotificationPriority.MEDIUM,
        "low": NotificationPriority.LOW,
        "info": NotificationPriority.INFO,
    }
    return severity_mapping.get(severity.lower(), NotificationPriority.MEDIUM)


def _map_category(category: str):
    try:
        from baldur_pro.services.unified_notification import NotificationCategory
    except ImportError:
        NotificationCategory = None  # type: ignore[assignment,misc]

    category_mapping = {
        "sla": NotificationCategory.SLA,
        "circuit_breaker": NotificationCategory.CIRCUIT_BREAKER,
        "security": NotificationCategory.SECURITY,
        "dlq": NotificationCategory.OPERATIONS,
        "system": NotificationCategory.OPERATIONS,
        "retry": NotificationCategory.OPERATIONS,
        "tiering": NotificationCategory.OPERATIONS,
    }
    return category_mapping.get(category.lower(), NotificationCategory.OPERATIONS)


def _extract_metadata(annotations: dict[str, Any]) -> dict[str, Any]:
    metadata: dict[str, Any] = {}

    if "current_latency_ms" in annotations:
        try:
            metadata["current_latency_ms"] = float(annotations["current_latency_ms"])
        except (ValueError, TypeError):
            pass

    if "threshold_ms" in annotations:
        try:
            metadata["threshold_ms"] = float(annotations["threshold_ms"])
        except (ValueError, TypeError):
            pass

    for field in ["affected_service", "runbook_url", "dashboard_url"]:
        if field in annotations:
            metadata[field] = annotations[field]

    return metadata


def _process_single_alert(alert: dict[str, Any], notification_manager) -> None:
    alert_status = alert.get("status", "firing")
    labels = alert.get("labels", {})
    annotations = alert.get("annotations", {})

    if alert_status == "resolved":
        alertname = labels.get("alertname", "unknown")
        logger.info("grafana_webhook.alert_resolved", alertname=alertname)
        return

    alertname = labels.get("alertname", "Unknown Alert")
    severity = labels.get("severity", "warning")
    category = labels.get("category", "operations")

    summary = annotations.get("summary", alertname)
    description = annotations.get("description", "No description provided")

    priority = _map_severity_to_priority(severity)
    notification_category = _map_category(category)

    metadata = _extract_metadata(annotations)
    metadata["alertname"] = alertname
    metadata["status"] = alert_status
    metadata["grafana_labels"] = labels

    from baldur.models.notification import NotificationPayload

    payload = NotificationPayload(
        title=summary,
        message=description,
        priority=priority,
        category=notification_category,
        source="grafana_alerting",
        metadata=metadata,
        tags=[
            f"alert:{alertname}",
            f"severity:{severity}",
            f"category:{category}",
        ],
        dedup_key=(
            f"grafana_alert_{alertname}_{labels.get('service_name', 'unknown')}"
        ),
    )

    result = notification_manager.notify(payload)

    if result.success:
        logger.info(
            "grafana_webhook.alert_notification_sent",
            alertname=alertname,
            channels_sent=result.channels_sent,
        )
    elif result.suppressed:
        logger.info(
            "grafana_webhook.alert_notification_suppressed",
            alertname=alertname,
            suppression_reason=result.suppression_reason,
        )
    else:
        logger.warning(
            "grafana_webhook.alert_notification_failed",
            alertname=alertname,
            error=result.error,
        )


def grafana_alert_webhook(ctx: RequestContext) -> ResponseContext:
    """POST /webhook/grafana/alert — receive Grafana Alert webhooks (public)."""
    try:
        from baldur_pro.services.unified_notification import (
            UnifiedNotificationManager,
        )
    except ImportError:
        return ResponseContext.service_unavailable("Notification service unavailable")

    body = ctx.json_body or {}
    alerts = body.get("alerts", [])
    if not alerts:
        logger.info("grafana_webhook.no_alerts")
        return ResponseContext.json({"status": "ok", "message": "No alerts to process"})

    notification_manager = UnifiedNotificationManager()
    processed_count = 0
    error_count = 0

    for alert in alerts:
        try:
            _process_single_alert(alert, notification_manager)
            processed_count += 1
        except Exception as e:
            logger.exception("grafana_webhook.alert_processing_failed", error=str(e))
            error_count += 1

    logger.info(
        "grafana_webhook.alerts_processed",
        processed=processed_count,
        errors=error_count,
    )

    return ResponseContext.json(
        {
            "status": "ok",
            "processed": processed_count,
            "errors": error_count,
        }
    )


def grafana_webhook_test_get(ctx: RequestContext) -> ResponseContext:
    """GET /webhook/grafana/test — webhook endpoint status check (public)."""
    return ResponseContext.json(
        {
            "status": "ok",
            "endpoint": "grafana_alert_webhook",
            "timestamp": utc_now().isoformat(),
        }
    )


def grafana_webhook_test_post(ctx: RequestContext) -> ResponseContext:
    """POST /webhook/grafana/test — test webhook receive (public)."""
    payload = ctx.json_body or {}
    logger.info("grafana_webhook.test_received", payload=payload)
    return ResponseContext.json(
        {
            "status": "ok",
            "message": "Test webhook received",
            "received_payload": payload,
        }
    )
