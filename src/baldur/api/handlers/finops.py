"""
Framework-agnostic FinOps DNA handlers.

Extracted from api/django/views/finops.py (Phase 2b).

Endpoints:
    GET    /finops/budget                              All budgets
    GET    /finops/budget/{service_name}                Budget for service
    POST   /finops/budget/{service_name}                Set budget
    DELETE /finops/budget/{service_name}                Reset budget
    POST   /finops/cost                                Record cost
    GET    /finops/report                              Generate report
    GET    /finops/alerts                              List alerts
    POST   /finops/alerts/{alert_index}                Acknowledge alert
"""

from __future__ import annotations

from decimal import Decimal

import structlog

from baldur.interfaces.web_framework import RequestContext, ResponseContext

logger = structlog.get_logger()

__all__ = [
    "finops_budget_get",
    "finops_budget_set",
    "finops_budget_reset",
    "finops_cost_record",
    "finops_report",
    "finops_alerts_list",
    "finops_alert_acknowledge",
]


def _service():
    # 599 D7 — canary/chaos pattern: the implementation lives in the
    # private distribution; resolve via the registry slot populated by
    # register_pro_services(). None -> handlers return 503.
    from baldur.factory.registry import ProviderRegistry

    return ProviderRegistry.finops_service.safe_get()


def finops_budget_get(ctx: RequestContext) -> ResponseContext:
    """GET /finops/budget/{service_name} — budget for service (viewer)."""
    service_name = ctx.get_path_param("service_name")
    service = _service()
    if not service:
        return ResponseContext.service_unavailable("FinOps service not available")

    if service_name:
        budget = service.get_budget(service_name)
        if budget:
            return ResponseContext.json(budget.to_dict())
        return ResponseContext.not_found("Budget not found")
    budgets = service.get_all_budgets()
    return ResponseContext.json({"budgets": budgets})


def finops_budget_set(ctx: RequestContext) -> ResponseContext:
    """POST /finops/budget/{service_name} — set budget (admin)."""
    service_name = ctx.get_path_param("service_name")
    service = _service()
    if not service:
        return ResponseContext.service_unavailable("FinOps service not available")

    from baldur.settings.finops import FinOpsSettings, get_finops_settings

    try:
        finops_settings = get_finops_settings()
    except Exception:
        finops_settings = FinOpsSettings()

    body = ctx.json_body or {}
    budget = service.set_budget(
        service_name=service_name,
        max_budget=Decimal(
            str(body.get("max_budget", str(finops_settings.default_max_budget)))
        ),
        alert_threshold=body.get(
            "alert_threshold", finops_settings.default_alert_threshold
        ),
        hard_limit=body.get("hard_limit", finops_settings.default_hard_limit),
        reset_period=body.get("reset_period", finops_settings.default_reset_period),
    )
    return ResponseContext.created(budget.to_dict())


def finops_budget_reset(ctx: RequestContext) -> ResponseContext:
    """DELETE /finops/budget/{service_name} — reset budget (admin)."""
    service_name = ctx.get_path_param("service_name")
    service = _service()
    if not service:
        return ResponseContext.service_unavailable("FinOps service not available")

    if service.reset_budget(service_name):
        return ResponseContext.json({"message": "Budget reset successfully"})
    return ResponseContext.not_found("Budget not found")


def finops_cost_record(ctx: RequestContext) -> ResponseContext:
    """POST /finops/cost — record cost (operator)."""
    service = _service()
    if not service:
        return ResponseContext.service_unavailable("FinOps service not available")

    body = ctx.json_body or {}
    cost = body.get("cost")
    record = service.record_cost(
        operation=body.get("operation"),
        service_name=body.get("service_name"),
        cost=Decimal(str(cost)) if cost else None,
        success=body.get("success", True),
        metadata=body.get("metadata", {}),
    )
    return ResponseContext.created(record.to_dict())


def finops_report(ctx: RequestContext) -> ResponseContext:
    """GET /finops/report — generate report (viewer)."""
    service = _service()
    if not service:
        return ResponseContext.service_unavailable("FinOps service not available")

    period = ctx.get_query("period", "daily")
    service_name = ctx.get_query("service_name")

    report = service.generate_report(period=period, service_name=service_name)
    return ResponseContext.json(report.to_dict())


def finops_alerts_list(ctx: RequestContext) -> ResponseContext:
    """GET /finops/alerts — list alerts (viewer)."""
    service = _service()
    if not service:
        return ResponseContext.service_unavailable("FinOps service not available")

    service_name = ctx.get_query("service_name")
    unacknowledged = ctx.get_query("unacknowledged", "false").lower() == "true"

    alerts = service.get_alerts(
        service_name=service_name,
        unacknowledged_only=unacknowledged,
    )
    return ResponseContext.json({"alerts": [a.to_dict() for a in alerts]})


def finops_alert_acknowledge(ctx: RequestContext) -> ResponseContext:
    """POST /finops/alerts/{alert_index} — acknowledge alert (operator)."""
    try:
        alert_index = int(ctx.get_path_param("alert_index"))
    except (TypeError, ValueError):
        return ResponseContext.bad_request("alert_index must be an integer")

    service = _service()
    if not service:
        return ResponseContext.service_unavailable("FinOps service not available")

    if service.acknowledge_alert(alert_index):
        return ResponseContext.json({"message": "Alert acknowledged"})
    return ResponseContext.not_found("Alert not found")
