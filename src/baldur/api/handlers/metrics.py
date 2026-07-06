"""
Framework-agnostic metrics handlers.

Extracted from api/django/views/health.py — BaldurMetricsView and
PrometheusTextMetricsView. The Prometheus text handler returns
a raw text/plain response (not JSON) so the exposition format stays
byte-exact for scrapers.

Endpoints:
    GET /metrics/              Baldur control-API metrics (JSON)
    GET /prometheus/           Prometheus text exposition
"""

from __future__ import annotations

import structlog

from baldur.interfaces.web_framework import RequestContext, ResponseContext

logger = structlog.get_logger()

__all__ = ["baldur_metrics", "prometheus_text_metrics"]


def baldur_metrics(ctx: RequestContext) -> ResponseContext:
    """GET /metrics/ — Baldur control-API metrics (authenticated)."""
    from baldur.services.control_api_service import get_control_api_service

    service = get_control_api_service()
    return ResponseContext.json(service.get_metrics())


def prometheus_text_metrics(ctx: RequestContext) -> ResponseContext:
    """GET /prometheus/ — Prometheus text exposition endpoint."""
    try:
        from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
    except ImportError:
        return ResponseContext.json(
            {"error": "prometheus_client not installed"},
            status_code=503,
        )

    metrics_output = generate_latest()
    body = (
        metrics_output.decode("utf-8")
        if isinstance(metrics_output, (bytes, bytearray))
        else metrics_output
    )
    return ResponseContext.raw(body, content_type=CONTENT_TYPE_LATEST)
