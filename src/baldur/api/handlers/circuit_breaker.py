"""
Framework-agnostic Circuit Breaker control handlers.

Extracted from api/django/views/circuit_breaker.py. The handler functions
below replace the DRF-layer validation done by ControlRequestSerializer
with plain dict validation so the logic is usable from any framework
(Django, FastAPI, Flask, CLI).

Endpoints:
    POST /control/                         Execute control action
    GET  /control/status/                  All service states
    GET  /control/status/{service_name}/   Single service state
    GET  /control/audit/                   Audit logs
    POST /control/allow/{service_name}/    Quick allow
    POST /control/block/{service_name}/    Quick block
    POST /control/reset/{service_name}/    Quick reset
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import structlog

from baldur.api.handlers._common import resolve_actor
from baldur.core.constants import ControlAPIActions, ControlAPIEnvironments
from baldur.interfaces.web_framework import RequestContext, ResponseContext
from baldur.services.control_api_service import (
    ControlRequest,
    get_control_api_service,
)
from baldur.utils.time import utc_now

logger = structlog.get_logger()

__all__ = [
    "control_action",
    "control_status",
    "service_status",
    "control_audit",
    "quick_allow",
    "quick_block",
    "quick_reset",
]


def _actor_role(ctx: RequestContext) -> str:
    user = ctx.user
    if user is not None and getattr(user, "is_staff", False):
        return "admin"
    return "user"


def _validate_control_request(  # noqa: C901, PLR0912
    data: dict[str, Any],
) -> tuple[dict[str, Any], str | None]:
    """Validate control request payload.

    Returns (cleaned_data, error_message). error_message is None on success.
    Mirrors ControlRequestSerializer validation rules (service_name/action/
    reason/environment required; cross-field rules for ops environment).
    """
    errors: dict[str, str] = {}

    service_name = data.get("service_name")
    if not service_name or not isinstance(service_name, str):
        errors["service_name"] = "service_name is required"
    elif len(service_name) > 100:
        errors["service_name"] = "service_name exceeds 100 chars"

    action = data.get("action")
    if action not in ControlAPIActions.ALL:
        errors["action"] = f"action must be one of {ControlAPIActions.ALL}"

    reason = data.get("reason")
    if not reason or not isinstance(reason, str):
        errors["reason"] = "reason is required"
    elif len(reason) > 500:
        errors["reason"] = "reason exceeds 500 chars"

    environment = data.get("environment")
    if environment not in ControlAPIEnvironments.ALL:
        errors["environment"] = (
            f"environment must be one of {ControlAPIEnvironments.ALL}"
        )

    ttl_minutes = data.get("ttl_minutes")
    if ttl_minutes is not None:
        if not isinstance(ttl_minutes, int) or isinstance(ttl_minutes, bool):
            errors["ttl_minutes"] = "ttl_minutes must be an integer"
        elif ttl_minutes < 1 or ttl_minutes > 1440:
            errors["ttl_minutes"] = "ttl_minutes must be between 1 and 1440"

    if errors:
        return {}, str(errors)

    # Cross-field rules
    if (
        action == ControlAPIActions.INJECT_FAILURE
        and environment == ControlAPIEnvironments.OPS
    ):
        return {}, "inject_failure is FORBIDDEN in ops environment"

    if (
        action == ControlAPIActions.OVERRIDE
        and environment == ControlAPIEnvironments.OPS
    ):
        if not ttl_minutes:
            return {}, "TTL is required for override action in ops environment"
        if ttl_minutes > 60:
            return (
                {},
                f"TTL cannot exceed 60 minutes in ops environment (got: {ttl_minutes})",
            )

    cleaned = {
        "service_name": service_name,
        "action": action,
        "reason": reason,
        "environment": environment,
        "ttl_minutes": ttl_minutes,
        "request_id": data.get("request_id"),
        "metadata": data.get("metadata") or {},
    }
    return cleaned, None


def control_action(ctx: RequestContext) -> ResponseContext:
    """POST /control/ — execute a control action (admin)."""
    body = ctx.json_body or {}
    cleaned, err = _validate_control_request(body)
    if err:
        return ResponseContext.json(
            {
                "status": "rejected",
                "error_code": "VALIDATION_ERROR",
                "error_message": err,
                "action_requested": body.get("action", "unknown"),
                "environment": body.get("environment", "unknown"),
            },
            status_code=400,
        )

    control_request = ControlRequest(
        service_name=cleaned["service_name"],
        action=cleaned["action"],
        reason=cleaned["reason"],
        environment=cleaned["environment"],
        ttl_minutes=cleaned.get("ttl_minutes"),
        request_id=str(cleaned.get("request_id") or ""),
        metadata=cleaned.get("metadata", {}),
        actor=resolve_actor(ctx),
        actor_role=_actor_role(ctx),
    )

    service = get_control_api_service()
    response = service.execute(control_request)

    if response.status == "rejected":
        return ResponseContext.json(response.to_dict(), status_code=403)
    if response.status == "error":
        return ResponseContext.json(response.to_dict(), status_code=500)
    return ResponseContext.json(response.to_dict())


def control_status(ctx: RequestContext) -> ResponseContext:
    """GET /control/status/ — all services (viewer+)."""
    environment = ctx.get_query("environment", "ops")
    service = get_control_api_service()
    return ResponseContext.json(service.get_status(environment=environment))


def service_status(ctx: RequestContext) -> ResponseContext:
    """GET /control/status/{service_name}/ — single service (viewer+)."""
    service_name = ctx.get_path_param("service_name", "")
    if not service_name:
        return ResponseContext.json(
            {"error": "service_name path parameter is required"},
            status_code=400,
        )
    service = get_control_api_service()
    return ResponseContext.json(service.get_service_status(service_name))


def control_audit(ctx: RequestContext) -> ResponseContext:
    """GET /control/audit/ — audit logs (viewer+).

    D19: uses ProviderRegistry.get_audit_adapter().query() directly and
    returns the H1 schema (``schema_version="h1"``).
    """
    from baldur.factory import ProviderRegistry
    from baldur.interfaces.audit_adapter import AuditAction

    try:
        page = int(ctx.get_query("page", 1))
    except (TypeError, ValueError):
        page = 1
    try:
        page_size = int(ctx.get_query("page_size", 50))
    except (TypeError, ValueError):
        page_size = 50
    config_type = ctx.get_query("config_type")
    user = ctx.get_query("user")
    try:
        days = int(ctx.get_query("days", 7))
    except (TypeError, ValueError):
        days = 7

    try:
        adapter = ProviderRegistry.get_audit_adapter()
        end_time = utc_now()
        start_time = end_time - timedelta(days=days)

        entries = adapter.query(
            action=AuditAction.CONFIG_CHANGE,
            start_time=start_time,
            end_time=end_time,
            limit=(page + 1) * page_size * 4,
        )

        if config_type:
            entries = [e for e in entries if e.target_type == config_type]
        if user:
            entries = [e for e in entries if e.actor_id == user]

        start_idx = (page - 1) * page_size
        end_idx = start_idx + page_size
        paginated = [e.to_dict() for e in entries[start_idx:end_idx]]

        return ResponseContext.json(
            {
                "logs": paginated,
                "total_count": len(entries),
                "page": page,
                "page_size": page_size,
                "filters": {
                    "config_type": config_type,
                    "user": user,
                    "days": days,
                },
                "schema_version": "h1",
            }
        )
    except Exception as e:
        logger.warning("audit_logs_view.retrieval_error", error=str(e))
        return ResponseContext.json(
            {
                "logs": [],
                "total_count": 0,
                "page": page,
                "page_size": page_size,
                "error": "Audit log retrieval temporarily unavailable",
                "schema_version": "h1",
            }
        )


def _quick_action(
    ctx: RequestContext,
    action: str,
    default_reason: str,
    default_ttl_minutes: int | None = None,
) -> ResponseContext:
    service_name = ctx.get_path_param("service_name", "")
    if not service_name:
        return ResponseContext.json(
            {"error": "service_name path parameter is required"},
            status_code=400,
        )
    body = ctx.json_body or {}
    control_request = ControlRequest(
        service_name=service_name,
        action=action,
        reason=body.get("reason", default_reason),
        environment=body.get("environment", "ops"),
        ttl_minutes=body.get("ttl_minutes", default_ttl_minutes),
        actor=resolve_actor(ctx),
    )
    service = get_control_api_service()
    response = service.execute(control_request)
    return ResponseContext.json(response.to_dict())


def quick_allow(ctx: RequestContext) -> ResponseContext:
    """POST /control/allow/{service_name}/ — quick allow (admin)."""
    return _quick_action(ctx, ControlAPIActions.ALLOW, "Quick allow via API")


def quick_block(ctx: RequestContext) -> ResponseContext:
    """POST /control/block/{service_name}/ — quick block (admin)."""
    return _quick_action(
        ctx, ControlAPIActions.BLOCK, "Quick block via API", default_ttl_minutes=90
    )


def quick_reset(ctx: RequestContext) -> ResponseContext:
    """POST /control/reset/{service_name}/ — quick reset (admin)."""
    return _quick_action(ctx, ControlAPIActions.RESET, "Quick reset via API")
