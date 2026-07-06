"""
Framework-agnostic Error Budget Reconciliation handlers.

Extracted from api/django/views/error_budget/reconciliation.py (Phase 2b).

Endpoints:
    GET   /reconciliation/status                           Reconciliation status
    GET   /reconciliation/failsafe-periods                 Fail-safe periods
    GET   /reconciliation/shadow-budgets                   Shadow budget list
    POST  /reconciliation/shadow-budgets                   Calculate shadow budget
    GET   /reconciliation/shadow-budgets/{calculation_id}  Shadow budget detail
    POST  /reconciliation/shadow-budgets/{calculation_id}/approve  Approve shadow budget
    POST  /reconciliation/shadow-budgets/{calculation_id}/reject   Reject shadow budget
    GET   /reconciliation/excluded-periods                 Excluded periods list
    POST  /reconciliation/excluded-periods                 Exclude period
    DELETE /reconciliation/excluded-periods/{exclusion_id} Remove exclusion
    GET   /reconciliation/config                           Reconciliation config
    PUT   /reconciliation/config                           Update reconciliation config
"""

from __future__ import annotations

from datetime import datetime

import structlog

from baldur.api.handlers._common import resolve_actor
from baldur.interfaces.web_framework import RequestContext, ResponseContext
from baldur.utils.time import utc_now

logger = structlog.get_logger()

__all__ = [
    "reconciliation_status",
    "reconciliation_failsafe_periods",
    "reconciliation_shadow_budgets_list",
    "reconciliation_shadow_budgets_calculate",
    "reconciliation_shadow_budget_detail",
    "reconciliation_shadow_budget_approve",
    "reconciliation_shadow_budget_reject",
    "reconciliation_excluded_periods_list",
    "reconciliation_excluded_periods_create",
    "reconciliation_excluded_period_delete",
    "reconciliation_config_get",
    "reconciliation_config_update",
]


def _service():
    try:
        from baldur_pro.services.error_budget.reconciliation import (
            get_reconciliation_service,
        )
    except ImportError:
        get_reconciliation_service = None  # type: ignore[assignment,misc]

    return get_reconciliation_service()


def _period_tracker():
    try:
        from baldur_pro.services.error_budget.reconciliation import get_period_tracker
    except ImportError:
        get_period_tracker = None  # type: ignore[assignment,misc]

    return get_period_tracker()


def reconciliation_status(ctx: RequestContext) -> ResponseContext:
    """GET /reconciliation/status — reconciliation status (viewer)."""
    service = _service()
    status_data = service.get_status()

    return ResponseContext.json(
        {
            "status": "success",
            "data": status_data,
            "timestamp": utc_now().isoformat(),
        }
    )


def reconciliation_failsafe_periods(ctx: RequestContext) -> ResponseContext:
    """GET /reconciliation/failsafe-periods — fail-safe periods (viewer)."""
    try:
        limit = int(ctx.get_query("limit", 50))
    except (TypeError, ValueError):
        limit = 50

    tracker = _period_tracker()
    periods = tracker.get_all_periods(limit=limit)
    active_period = tracker.get_active_period()

    return ResponseContext.json(
        {
            "status": "success",
            "data": {
                "periods": [p.to_dict() for p in periods],
                "count": len(periods),
                "active_period": (active_period.to_dict() if active_period else None),
            },
            "timestamp": utc_now().isoformat(),
        }
    )


def reconciliation_shadow_budgets_list(ctx: RequestContext) -> ResponseContext:
    """GET /reconciliation/shadow-budgets — shadow budget list (viewer)."""
    try:
        limit = int(ctx.get_query("limit", 50))
    except (TypeError, ValueError):
        limit = 50

    pending_only = ctx.get_query("pending_only", "false").lower() == "true"

    service = _service()

    if pending_only:
        budgets = service.get_pending_shadow_budgets()
    else:
        budgets = service.get_all_shadow_budgets(limit=limit)

    return ResponseContext.json(
        {
            "status": "success",
            "data": {
                "shadow_budgets": [sb.to_dict() for sb in budgets],
                "count": len(budgets),
            },
            "timestamp": utc_now().isoformat(),
        }
    )


def reconciliation_shadow_budgets_calculate(ctx: RequestContext) -> ResponseContext:
    """POST /reconciliation/shadow-budgets — calculate shadow budget (operator)."""
    body = ctx.json_body or {}
    period_id = body.get("period_id")
    if not period_id:
        return ResponseContext.bad_request("period_id is required")

    service = _service()
    shadow = service.calculate_shadow_budget(period_id)

    if not shadow:
        return ResponseContext.bad_request("Failed to calculate shadow budget")

    return ResponseContext.json(
        {
            "status": "success",
            "data": shadow.to_dict(),
            "timestamp": utc_now().isoformat(),
        }
    )


def reconciliation_shadow_budget_detail(ctx: RequestContext) -> ResponseContext:
    """GET /reconciliation/shadow-budgets/{calculation_id} — shadow budget detail (viewer)."""
    calculation_id = ctx.get_path_param("calculation_id")

    service = _service()
    shadow = service.get_shadow_budget(calculation_id)

    if not shadow:
        return ResponseContext.not_found(f"Shadow budget {calculation_id} not found")

    return ResponseContext.json(
        {
            "status": "success",
            "data": shadow.to_dict(),
            "timestamp": utc_now().isoformat(),
        }
    )


def reconciliation_shadow_budget_approve(ctx: RequestContext) -> ResponseContext:
    """POST /reconciliation/shadow-budgets/{calculation_id}/approve — approve (admin)."""
    calculation_id = ctx.get_path_param("calculation_id")
    body = ctx.json_body or {}
    justification = body.get("justification", "")
    if not justification:
        return ResponseContext.bad_request("justification is required")

    actor = resolve_actor(ctx)

    service = _service()
    shadow = service.approve_shadow_budget(
        calculation_id=calculation_id,
        approved_by=actor,
        justification=justification,
    )

    if not shadow:
        return ResponseContext.bad_request("Shadow budget not found or invalid status")

    return ResponseContext.json(
        {
            "status": "success",
            "message": f"Shadow budget approved and applied (adjustment: {shadow.adjustment_percent:.2f}%)",
            "data": shadow.to_dict(),
            "timestamp": utc_now().isoformat(),
        }
    )


def reconciliation_shadow_budget_reject(ctx: RequestContext) -> ResponseContext:
    """POST /reconciliation/shadow-budgets/{calculation_id}/reject — reject (admin)."""
    calculation_id = ctx.get_path_param("calculation_id")
    body = ctx.json_body or {}
    reason = body.get("reason", "")
    if not reason:
        return ResponseContext.bad_request("reason is required")

    actor = resolve_actor(ctx)

    service = _service()
    shadow = service.reject_shadow_budget(
        calculation_id=calculation_id,
        rejected_by=actor,
        reason=reason,
    )

    if not shadow:
        return ResponseContext.bad_request("Shadow budget not found or invalid status")

    return ResponseContext.json(
        {
            "status": "success",
            "message": "Shadow budget rejected, period excluded from calculation",
            "data": shadow.to_dict(),
            "timestamp": utc_now().isoformat(),
        }
    )


def reconciliation_excluded_periods_list(ctx: RequestContext) -> ResponseContext:
    """GET /reconciliation/excluded-periods — excluded periods list (viewer)."""
    try:
        limit = int(ctx.get_query("limit", 50))
    except (TypeError, ValueError):
        limit = 50

    service = _service()
    periods = service.get_excluded_periods(limit=limit)

    return ResponseContext.json(
        {
            "status": "success",
            "data": {
                "excluded_periods": [p.to_dict() for p in periods],
                "count": len(periods),
            },
            "timestamp": utc_now().isoformat(),
        }
    )


def reconciliation_excluded_periods_create(ctx: RequestContext) -> ResponseContext:
    """POST /reconciliation/excluded-periods — exclude period (operator)."""
    body = ctx.json_body or {}
    start_str = body.get("start")
    end_str = body.get("end")
    reason = body.get("reason", "")

    if not isinstance(start_str, str) or not isinstance(end_str, str) or not reason:
        return ResponseContext.bad_request("start, end, and reason are required")

    start = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
    end = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
    notes = body.get("notes", "")
    actor = resolve_actor(ctx)

    service = _service()
    exclusion = service.exclude_period(
        start=start,
        end=end,
        reason=reason,
        excluded_by=actor,
        notes=notes,
    )

    return ResponseContext.json(
        {
            "status": "success",
            "message": "Period excluded from budget calculation",
            "data": exclusion.to_dict(),
            "timestamp": utc_now().isoformat(),
        }
    )


def reconciliation_excluded_period_delete(ctx: RequestContext) -> ResponseContext:
    """DELETE /reconciliation/excluded-periods/{exclusion_id} — remove exclusion (admin)."""
    exclusion_id = ctx.get_path_param("exclusion_id")

    service = _service()
    success = service.remove_exclusion(exclusion_id)

    if not success:
        return ResponseContext.not_found(f"Exclusion {exclusion_id} not found")

    return ResponseContext.json(
        {
            "status": "success",
            "message": "Exclusion removed, period re-included in calculation",
            "timestamp": utc_now().isoformat(),
        }
    )


def reconciliation_config_get(ctx: RequestContext) -> ResponseContext:
    """GET /reconciliation/config — reconciliation config (admin)."""
    service = _service()
    config = service.get_config()

    return ResponseContext.json(
        {
            "status": "success",
            "data": config.to_dict(),
            "timestamp": utc_now().isoformat(),
        }
    )


def reconciliation_config_update(ctx: RequestContext) -> ResponseContext:
    """PUT /reconciliation/config — update reconciliation config (admin)."""
    body = ctx.json_body or {}
    if not body:
        return ResponseContext.bad_request("No changes provided")

    service = _service()
    config = service.update_config(**body)

    return ResponseContext.json(
        {
            "status": "success",
            "message": "Reconciliation config updated",
            "data": config.to_dict(),
            "timestamp": utc_now().isoformat(),
        }
    )
