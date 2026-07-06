"""
Framework-agnostic Metric Sync handlers.

Extracted from api/django/views/metric_sync.py (Phase 2b).

Endpoints:
    POST /metrics/sync          Manual metric synchronization
    GET  /metrics/drift-report  Drift status report
"""

from __future__ import annotations

import structlog

from baldur.api.handlers._common import resolve_actor
from baldur.interfaces.web_framework import RequestContext, ResponseContext

logger = structlog.get_logger()

__all__ = [
    "metric_sync",
    "drift_report",
]


def _service():
    from baldur.services.metric_sync_service import get_metric_sync_service

    return get_metric_sync_service()


def metric_sync(ctx: RequestContext) -> ResponseContext:
    """POST /metrics/sync — manual metric synchronization (admin)."""
    body = ctx.json_body or {}

    domains = body.get("domains")
    dry_run = body.get("dry_run", False)
    reason = body.get("reason", "")

    if domains is not None and not isinstance(domains, list):
        return ResponseContext.bad_request("domains must be a list")

    actor = resolve_actor(ctx)
    service = _service()
    result = service.sync_metrics(
        domains=domains,
        dry_run=dry_run,
        actor=actor,
        reason=reason,
    )
    return ResponseContext.json(result)


def drift_report(ctx: RequestContext) -> ResponseContext:
    """GET /metrics/drift-report — drift status report (admin)."""
    service = _service()
    result = service.get_drift_report()
    return ResponseContext.json(result)
