"""
Framework-agnostic Governance handlers.

Extracted from api/django/views/governance/ (Phase 2a).
Covers approval workflows, governance config, L2 storage config,
reconciliation, mode switching, metric status, and RBAC status.

Endpoints:
    GET  /governance/approval-requests/                List approval requests
    POST /governance/approval-requests/                Create approval request
    POST /governance/approval-requests/{id}/approve/   Approve request
    POST /governance/approval-requests/{id}/reject/    Reject request
    GET  /config/governance/                           Governance config
    PUT  /config/governance/                           Update governance config
    GET  /config/l2-storage/                           L2 storage config
    PUT  /config/l2-storage/                           Update L2 storage config
    POST /governance/reconcile/                        Manual reconciliation
    GET  /governance/mode/                             Current operating mode
    POST /governance/mode/                             Switch operating mode
    GET  /metrics/status/                              Metric status overview
    GET  /governance/status/                           RBAC status overview
"""

from __future__ import annotations

import structlog

from baldur.api.handlers._common import resolve_actor
from baldur.interfaces.web_framework import RequestContext, ResponseContext
from baldur.utils.time import utc_now

logger = structlog.get_logger()

__all__ = [
    "approval_request_list",
    "approval_request_create",
    "approval_request_approve",
    "approval_request_reject",
    "governance_config_get",
    "governance_config_update",
    "l2_storage_config_get",
    "l2_storage_config_update",
    "governance_reconcile",
    "governance_mode_get",
    "governance_mode_set",
    "metric_status",
    "governance_rbac_status",
]


# ---------------------------------------------------------------------------
# Lazy-import helpers
# ---------------------------------------------------------------------------


def _runtime_config_manager():
    from baldur.factory.registry import ProviderRegistry

    manager = ProviderRegistry.runtime_config_manager.safe_get()
    if manager is None:
        raise RuntimeError(
            "Governance handlers require baldur_pro RuntimeConfigManager"
        )
    return manager


def _governance_api_service():
    from baldur_pro.services.governance.api_service import get_governance_api_service

    return get_governance_api_service()


def _governance_settings():
    from baldur.settings.governance import get_governance_settings

    return get_governance_settings()


def _get_governance_channels() -> list[str]:
    """Get governance notification channels from ChannelRoutingSettings."""
    try:
        from baldur.settings.channel_routing import get_channel_routing_settings

        return get_channel_routing_settings().category_channels.get(
            "governance", ["slack"]
        )
    except Exception:
        return ["slack"]


# ---------------------------------------------------------------------------
# Approval Request handlers
# ---------------------------------------------------------------------------


def approval_request_list(ctx: RequestContext) -> ResponseContext:
    """GET /governance/approval-requests/ — list approval requests.

    Query Parameters:
        status: Filter by status (PENDING, APPROVED, REJECTED, EXPIRED)
        for_me: Only show requests I can approve (excludes my own)
    """
    manager = _runtime_config_manager()

    # Expire old requests first
    manager.expire_old_requests()

    status_filter = ctx.get_query("status")
    for_me = (ctx.get_query("for_me") or "").lower() == "true"

    actor = resolve_actor(ctx)

    if for_me:
        requests_list = manager.get_pending_requests_for_user(actor)
    else:
        requests_list = manager.get_approval_requests(status=status_filter)

    return ResponseContext.json(
        {
            "status": "success",
            "requests": requests_list,
            "count": len(requests_list),
            "timestamp": utc_now().isoformat(),
        }
    )


def approval_request_create(ctx: RequestContext) -> ResponseContext:
    """POST /governance/approval-requests/ — create a new approval request.

    Request Body:
        request_type: Type (config_change, mode_change, emergency_action)
        description: Human-readable description
        payload: Request data to be approved
        expiry_hours: Hours until expiry (default from governance settings)
    """
    manager = _runtime_config_manager()
    actor = resolve_actor(ctx)

    body = ctx.json_body or {}
    request_type = body.get("request_type", "")
    description = body.get("description", "")
    payload = body.get("payload", {})
    default_expiry = _governance_settings().approval_timeout_hours
    expiry_hours = body.get("expiry_hours", default_expiry)

    if not request_type:
        return ResponseContext.bad_request("request_type is required")

    if not description:
        return ResponseContext.bad_request("description is required")

    approval_request = manager.create_approval_request(
        request_type=request_type,
        description=description,
        requested_by=actor,
        payload=payload,
        expiry_hours=expiry_hours,
    )

    logger.info(
        "governance.approval_request_created",
        approval_request=approval_request["id"],
        actor_id=actor,
    )

    return ResponseContext.json(
        {
            "status": "created",
            "request": approval_request,
            "message": "Approval request created. Awaiting approval from another admin.",
            "timestamp": utc_now().isoformat(),
        },
        status_code=201,
    )


def approval_request_approve(ctx: RequestContext) -> ResponseContext:
    """POST /governance/approval-requests/{request_id}/approve/ — approve a pending request.

    The approver must be different from the requester (4-Eyes Principle).
    """
    manager = _runtime_config_manager()
    actor = resolve_actor(ctx)
    request_id = ctx.get_path_param("request_id", "")

    if not request_id:
        return ResponseContext.bad_request("request_id path parameter is required")

    result = manager.approve_request(request_id, actor)

    if result is None:
        return ResponseContext.bad_request(
            "Request not found, already processed, expired, or self-approval attempted"
        )

    logger.info(
        "governance.request_approved",
        request_id=request_id,
        actor_id=actor,
    )

    return ResponseContext.json(
        {
            "status": "approved",
            "request": result,
            "approved_by": actor,
            "timestamp": utc_now().isoformat(),
        }
    )


def approval_request_reject(ctx: RequestContext) -> ResponseContext:
    """POST /governance/approval-requests/{request_id}/reject/ — reject a pending request."""
    manager = _runtime_config_manager()
    actor = resolve_actor(ctx)
    request_id = ctx.get_path_param("request_id", "")

    if not request_id:
        return ResponseContext.bad_request("request_id path parameter is required")

    body = ctx.json_body or {}
    reason = body.get("reason", "")

    result = manager.reject_request(request_id, actor, reason)

    if result is None:
        return ResponseContext.bad_request("Request not found or already processed")

    logger.info(
        "governance.request_rejected",
        request_id=request_id,
        actor_id=actor,
    )

    return ResponseContext.json(
        {
            "status": "rejected",
            "request": result,
            "rejected_by": actor,
            "reason": reason,
            "timestamp": utc_now().isoformat(),
        }
    )


# ---------------------------------------------------------------------------
# Governance Config handlers
# ---------------------------------------------------------------------------

_GOVERNANCE_CONFIG_ALLOWED_FIELDS = {
    "threshold_operator",
    "threshold_admin",
    "emergency_expiry_hours",
    "emergency_warning_hours",
    "emergency_final_warning_hours",
    "default_mode",
    "notify_on_emergency",
    "notify_channels",
    "emergency_slack_channel",
}


def governance_config_get(ctx: RequestContext) -> ResponseContext:
    """GET /config/governance/ — governance configuration (viewer+)."""
    manager = _runtime_config_manager()
    config = manager.get_governance_config()

    return ResponseContext.json(
        {
            "status": "success",
            "config": config,
            "timestamp": utc_now().isoformat(),
        }
    )


def governance_config_update(ctx: RequestContext) -> ResponseContext:
    """PUT /config/governance/ — update governance configuration (admin).

    Only allowed fields are accepted. A reason is required when
    governance settings enforce it.
    """
    manager = _runtime_config_manager()
    actor = resolve_actor(ctx)

    body = ctx.json_body or {}

    update_fields = {
        k: v for k, v in body.items() if k in _GOVERNANCE_CONFIG_ALLOWED_FIELDS
    }

    if not update_fields:
        return ResponseContext.bad_request("No valid fields provided")

    # Validate reason requirement
    settings = _governance_settings()
    if settings.require_reason_for_changes:
        reason = body.get("reason", "")
        if not reason:
            return ResponseContext.bad_request(
                "reason is required for configuration changes"
            )

    new_config = manager.update_governance_config(**update_fields)

    logger.info(
        "governance.config_updated",
        actor_id=actor,
        value=list(update_fields.keys()),
    )

    return ResponseContext.json(
        {
            "status": "updated",
            "config": new_config,
            "updated_by": actor,
            "updated_fields": list(update_fields.keys()),
            "timestamp": utc_now().isoformat(),
        }
    )


# ---------------------------------------------------------------------------
# L2 Storage Config handlers
# ---------------------------------------------------------------------------

_L2_STORAGE_CONFIG_ALLOWED_FIELDS = {
    "redis_timeout_ms",
    "database_timeout_ms",
    "fallback_timeout_ms",
    "shadow_log_max_entries",
    "reconciliation_jitter_min_seconds",
    "reconciliation_jitter_max_seconds",
    "health_check_interval_seconds",
    "health_check_timeout_ms",
}


def l2_storage_config_get(ctx: RequestContext) -> ResponseContext:
    """GET /config/l2-storage/ — L2 storage configuration (admin)."""
    manager = _runtime_config_manager()
    config = manager.get_l2_storage_config()

    return ResponseContext.json(
        {
            "status": "success",
            "config": config,
            "timestamp": utc_now().isoformat(),
        }
    )


def l2_storage_config_update(ctx: RequestContext) -> ResponseContext:
    """PUT /config/l2-storage/ — update L2 storage configuration (admin)."""
    manager = _runtime_config_manager()
    actor = resolve_actor(ctx)

    body = ctx.json_body or {}

    update_fields = {
        k: v for k, v in body.items() if k in _L2_STORAGE_CONFIG_ALLOWED_FIELDS
    }

    if not update_fields:
        return ResponseContext.bad_request("No valid fields provided")

    new_config = manager.update_l2_storage_config(**update_fields)

    logger.info(
        "governance.storage_config_updated",
        actor_id=actor,
        value=list(update_fields.keys()),
    )

    return ResponseContext.json(
        {
            "status": "updated",
            "config": new_config,
            "updated_by": actor,
            "updated_fields": list(update_fields.keys()),
            "timestamp": utc_now().isoformat(),
        }
    )


# ---------------------------------------------------------------------------
# Control handlers (reconcile, mode)
# ---------------------------------------------------------------------------


def governance_reconcile(ctx: RequestContext) -> ResponseContext:
    """POST /governance/reconcile/ — manual reconciliation (admin).

    Request Body:
        domains (list, optional): Domains to reconcile
        dry_run (bool, optional): If true, only generate a report
        reason (str, optional): Reconciliation reason (audit)
    """
    body = ctx.json_body or {}

    domains = body.get("domains")
    dry_run = body.get("dry_run", False)
    reason = body.get("reason", "")

    if domains is not None and not isinstance(domains, list):
        return ResponseContext.bad_request("domains must be a list")

    actor = resolve_actor(ctx)

    service = _governance_api_service()
    result = service.reconcile(
        domains=domains,
        dry_run=dry_run,
        actor=actor,
        reason=reason,
    )

    return ResponseContext.json(result)


def governance_mode_get(ctx: RequestContext) -> ResponseContext:
    """GET /governance/mode/ — current operating mode."""
    from baldur.metrics.reliability_manager import get_reliability_manager

    manager = get_reliability_manager()
    mode = manager.get_global_mode()

    return ResponseContext.json(
        {
            "current_mode": mode.value if hasattr(mode, "value") else str(mode),
            "valid_modes": ["NORMAL", "CAUTIOUS", "STRICT", "EMERGENCY"],
        }
    )


def governance_mode_set(ctx: RequestContext) -> ResponseContext:
    """POST /governance/mode/ — switch operating mode (admin).

    Break Glass Pattern (emergency escalation):
        - STRICT: Operator also allowed (one-way emergency) + reason required
        - NORMAL recovery: Admin only
        - Other modes: Admin only

    Auto-expiry:
        - STRICT mode auto-expires after governance config emergency_expiry_hours

    Request Body:
        mode (str, required): NORMAL | CAUTIOUS | STRICT | EMERGENCY
        reason (str, required for STRICT): Change reason (forced audit)
    """
    body = ctx.json_body or {}

    mode = body.get("mode")
    reason = body.get("reason", "")

    # mode must be a non-empty string; a non-string value would otherwise
    # reach mode.upper() (the STRICT check below and inside set_mode) and
    # raise AttributeError -> 500 instead of a clean client error.
    if not isinstance(mode, str) or not mode:
        return ResponseContext.bad_request("mode is required")

    # STRICT escalation is break-glass: a non-empty reason is mandatory for
    # accountability. Case-normalized to mirror set_mode's own mode.upper()
    # normalization, and type-safe because a non-string reason would raise
    # on .strip().
    if mode.upper() == "STRICT" and not (isinstance(reason, str) and reason.strip()):
        return ResponseContext.bad_request("reason is required for STRICT mode")

    actor = resolve_actor(ctx)

    service = _governance_api_service()
    try:
        result = service.set_mode(
            mode=mode,
            actor=actor,
            reason=reason,
        )
    except ValueError as exc:
        # set_mode raises ValueError for an unrecognized mode value; surface
        # it as a 400 client error rather than an unhandled 500.
        return ResponseContext.bad_request(str(exc))

    return ResponseContext.json(result)


# ---------------------------------------------------------------------------
# Status handlers (metric status, RBAC status)
# ---------------------------------------------------------------------------


def metric_status(ctx: RequestContext) -> ResponseContext:
    """GET /metrics/status/ — integrated metric status overview (admin).

    Returns all metric reliability indicators as SSOT (Single Source of Truth):
    operating mode, overall health, sync status, snapshot health, drift summary,
    per-domain details, and next expected sync time.
    """
    service = _governance_api_service()
    result = service.get_status()

    return ResponseContext.json(result)


def governance_rbac_status(ctx: RequestContext) -> ResponseContext:
    """GET /governance/status/ — governance RBAC status (viewer+).

    Returns current operating mode, emergency state, thresholds,
    expiry information, warning state, and governance config details.
    """
    from baldur_pro.services.governance import get_emergency_tracker

    manager = _runtime_config_manager()
    tracker = get_emergency_tracker()

    # Governance config
    governance_config = manager.get_governance_config()

    # Emergency mode state
    emergency_state = tracker.get_current_state()

    # Expiry status
    expiry_status = tracker.check_expiry_status()

    response_data = {
        "status": "success",
        "governance": {
            # Current mode
            "current_mode": emergency_state.mode,
            "default_mode": governance_config.get("default_mode", "NORMAL"),
            # Mode change info
            "mode_changed_at": emergency_state.activated_at,
            "mode_changed_by": emergency_state.activated_by,
            # Expiry info
            "mode_expires_at": expiry_status.get("expires_at"),
            "time_remaining_hours": expiry_status.get("time_remaining_hours"),
            # Thresholds (Risk-Based Access Control)
            "thresholds": {
                "operator_approve": governance_config.get("threshold_operator", 0.15),
                "admin_approve": governance_config.get("threshold_admin", 0.30),
            },
            # Emergency mode state
            "emergency_active": emergency_state.is_active,
            "emergency_reason": emergency_state.reason,
            "emergency_warning_sent": emergency_state.warning_sent_at is not None,
            "emergency_final_warning_sent": emergency_state.final_warning_sent_at
            is not None,
            "pending_admin_acknowledgement": (
                emergency_state.is_active and emergency_state.acknowledged_by is None
            ),
            # Warning state
            "should_warn": expiry_status.get("should_warn", False),
            "should_final_warn": expiry_status.get("should_final_warn", False),
            "should_auto_restore": expiry_status.get("should_auto_restore", False),
            # Config details
            "config": {
                "emergency_expiry_hours": governance_config.get(
                    "emergency_expiry_hours", 8
                ),
                "emergency_warning_hours": governance_config.get(
                    "emergency_warning_hours", 4
                ),
                "emergency_final_warning_hours": governance_config.get(
                    "emergency_final_warning_hours", 6
                ),
                "notify_on_emergency": governance_config.get(
                    "notify_on_emergency", True
                ),
                "notify_channels": governance_config.get(
                    "notify_channels", _get_governance_channels()
                ),
            },
        },
        "timestamp": utc_now().isoformat(),
    }

    return ResponseContext.json(response_data)
