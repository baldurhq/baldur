"""
Framework-agnostic Recovery handlers.

Extracted from api/django/views/recovery.py. Covers recovery status,
start, abort, pending approvals, approve, reject, history, and
dashboard widget.

Endpoints:
    GET  /recovery/status/              Current recovery status
    POST /recovery/start/               Start recovery
    POST /recovery/abort/               Abort recovery
    GET  /recovery/pending-approvals/   Pending approval list
    POST /recovery/approve/             Approve recovery
    POST /recovery/reject/              Reject recovery
    GET  /recovery/history/             Recovery history
    GET  /recovery/widget/              Dashboard widget data
"""

from __future__ import annotations

import structlog

from baldur.api.handlers._common import resolve_actor
from baldur.interfaces.web_framework import RequestContext, ResponseContext
from baldur.utils.time import utc_now

logger = structlog.get_logger()

__all__ = [
    "recovery_status",
    "recovery_start",
    "recovery_abort",
    "recovery_pending_approvals",
    "recovery_approve",
    "recovery_reject",
    "recovery_history",
    "recovery_dashboard_widget",
]


# =============================================================================
# Lazy Import Helpers
# =============================================================================


def _coordinator():
    """Return the recovery coordinator instance (lazy import)."""
    try:
        from baldur_pro.services.coordination.recovery_coordinator import (
            get_recovery_coordinator,
        )
    except ImportError:
        get_recovery_coordinator = None  # type: ignore[assignment,misc]

    return get_recovery_coordinator()


def _circuit_breaker():
    """Return the recovery circuit breaker instance (lazy import)."""
    try:
        from baldur_pro.services.coordination.recovery_circuit_breaker import (
            get_recovery_circuit_breaker,
        )
    except ImportError:
        get_recovery_circuit_breaker = None  # type: ignore[assignment,misc]

    return get_recovery_circuit_breaker()


def _approval_manager():
    """Return the pending recovery approval manager (lazy import)."""
    try:
        from baldur_pro.services.coordination.pending_recovery_approval import (
            get_pending_recovery_approval_manager,
        )
    except ImportError:
        get_pending_recovery_approval_manager = None  # type: ignore[assignment,misc]

    return get_pending_recovery_approval_manager()


def _policy_engine():
    """Return the regional recovery policy engine (lazy import)."""
    try:
        from baldur_pro.services.coordination.regional_recovery_policy import (
            get_regional_recovery_policy_engine,
        )
    except ImportError:
        get_regional_recovery_policy_engine = None  # type: ignore[assignment,misc]

    return get_regional_recovery_policy_engine()


def _dashboard_service():
    """Return the recovery dashboard service (lazy import)."""
    try:
        from baldur_pro.services.coordination.recovery_dashboard import (
            get_recovery_dashboard_service,
        )
    except ImportError:
        get_recovery_dashboard_service = None  # type: ignore[assignment,misc]

    return get_recovery_dashboard_service()


def _recovery_status_enum():
    """Return the RecoveryStatus enum (lazy import)."""
    try:
        from baldur_pro.services.coordination.enums import RecoveryStatus
    except ImportError:
        RecoveryStatus = None  # type: ignore[assignment,misc]

    return RecoveryStatus


# =============================================================================
# Handlers
# =============================================================================


def recovery_status(ctx: RequestContext) -> ResponseContext:
    """GET /recovery/status/ — current recovery status (authenticated)."""
    namespace = ctx.get_query("namespace", "global")

    coordinator = _coordinator()
    circuit_breaker = _circuit_breaker()
    approval_manager = _approval_manager()
    policy_engine = _policy_engine()

    # Current status
    current_status = coordinator.get_current_status(namespace)

    # Active session
    active_session = coordinator.get_active_session(namespace)
    session_data = None
    if active_session:
        # started_at is already an ISO 8601 string on RecoverySession.
        session_data = {
            "session_id": active_session.id,
            "status": active_session.status.value,
            "current_step": active_session.current_step_index,
            "total_steps": len(active_session.steps),
            "started_at": active_session.started_at,
            "namespace": active_session.namespace,
        }

    # Circuit breaker status
    cb_status = circuit_breaker.get_status(namespace)

    # Pending approvals
    pending = approval_manager.list_pending_requests(namespace=namespace)

    # Regional config
    config = policy_engine.get_config(namespace)

    return ResponseContext.json(
        {
            "status": current_status.value,
            "namespace": namespace,
            "active_session": session_data,
            "circuit_breaker": {
                "state": cb_status.get("state", "unknown"),
                "trip_count": cb_status.get("trip_count", 0),
                "is_permanently_open": cb_status.get("is_permanently_open", False),
            },
            "pending_approvals_count": len(pending),
            "regional_config": {
                "require_manual_approval": config.require_manual_approval,
                "stability_check_duration_minutes": (
                    config.stability_check_duration_minutes
                ),
                "approval_timeout_minutes": config.approval_timeout_minutes,
            },
            "timestamp": utc_now().isoformat(),
        }
    )


def recovery_start(ctx: RequestContext) -> ResponseContext:
    """POST /recovery/start/ — start recovery process (authenticated)."""
    RecoveryStatus = _recovery_status_enum()

    body = ctx.json_body or {}
    namespace = body.get("namespace", "global")
    force = body.get("force", False)
    skip_approval = body.get("skip_approval", False)

    coordinator = _coordinator()
    policy_engine = _policy_engine()
    approval_manager = _approval_manager()

    # Reject when a recovery is already advancing through its steps.
    active = coordinator.get_active_session(namespace)
    if active and active.status in (
        RecoveryStatus.IN_PROGRESS,
        RecoveryStatus.HEALTH_CHECK,
    ):
        return ResponseContext.json(
            {
                "error": "Recovery already in progress",
                "session_id": active.id,
                "status": active.status.value,
            },
            status_code=409,
        )

    # Resolve the emergency level to recover from (coordinator is the single
    # source of truth for the current level).
    trigger = coordinator.check_recovery_trigger(namespace)
    trigger_level = trigger.get("current_level", "NORMAL")
    if trigger_level == "NORMAL":
        return ResponseContext.json(
            {
                "error": "No active emergency to recover from",
                "status": RecoveryStatus.NORMAL.value,
                "namespace": namespace,
            },
            status_code=409,
        )

    # Check regional config
    config = policy_engine.get_config(namespace)
    actor = resolve_actor(ctx)

    # Manual approval required?
    if config.require_manual_approval and not skip_approval:
        # Check existing pending request
        existing = approval_manager.get_request_by_session_or_pending(
            namespace=namespace
        )

        if existing is None:
            # Create new approval request
            request_obj = approval_manager.create_request(
                session_id=f"manual-{namespace}-{utc_now().isoformat()}",
                namespace=namespace,
                trigger_level=trigger_level,
                timeout_minutes=config.approval_timeout_minutes,
                metadata={
                    "requested_by": actor,
                    "force": force,
                },
            )

            return ResponseContext.json(
                {
                    "message": "Approval required before starting recovery",
                    "approval_request_id": request_obj.request_id,
                    "status": RecoveryStatus.READY_TO_RESTORE.value,
                    "approval_timeout_minutes": config.approval_timeout_minutes,
                },
                status_code=202,
            )

        if existing.status.value == "pending":
            return ResponseContext.json(
                {
                    "message": "Waiting for approval",
                    "approval_request_id": existing.request_id,
                    "status": RecoveryStatus.READY_TO_RESTORE.value,
                },
                status_code=202,
            )

    # Start recovery
    try:
        session = coordinator.start_recovery(
            namespace=namespace,
            trigger_level=trigger_level,
            initiated_by=actor,
        )
    except ValueError as e:
        return ResponseContext.json(
            {"error": str(e), "namespace": namespace},
            status_code=409,
        )

    logger.info(
        "recovery_handler.recovery_started",
        session=session.id,
        namespace=namespace,
        actor=actor,
    )

    return ResponseContext.json(
        {
            "session_id": session.id,
            "status": session.status.value,
            "namespace": namespace,
            "steps": [s.step_type.value for s in session.steps],
            "message": "Recovery started successfully",
        },
        status_code=201,
    )


def recovery_abort(ctx: RequestContext) -> ResponseContext:
    """POST /recovery/abort/ — abort recovery process (authenticated)."""
    body = ctx.json_body or {}
    reason = body.get("reason", "Manual abort by user")
    namespace = body.get("namespace", "global")

    coordinator = _coordinator()
    actor = resolve_actor(ctx)

    # abort_recovery is namespace-scoped — it aborts that namespace's active
    # session and returns it (or None when there is nothing to abort).
    result = coordinator.abort_recovery(
        namespace=namespace,
        reason=f"{reason} (by {actor})",
    )

    if result is None:
        return ResponseContext.not_found("No active recovery session to abort")

    logger.info(
        "recovery_handler.recovery_aborted",
        session_id=result.id,
        actor=actor,
    )

    return ResponseContext.json(
        {
            "session_id": result.id,
            "status": result.status.value,
            "reason": reason,
            "message": "Recovery aborted successfully",
        }
    )


def recovery_pending_approvals(ctx: RequestContext) -> ResponseContext:
    """GET /recovery/pending-approvals/ — pending approval list (authenticated)."""
    namespace = ctx.get_query("namespace")  # None means all

    manager = _approval_manager()
    pending = manager.list_pending_requests(namespace=namespace)

    return ResponseContext.json(
        {
            "pending_approvals": [r.to_dict() for r in pending],
            "total_count": len(pending),
            "namespace_filter": namespace,
        }
    )


def recovery_approve(ctx: RequestContext) -> ResponseContext:
    """POST /recovery/approve/ — approve a recovery request (authenticated)."""
    body = ctx.json_body or {}
    request_id = body.get("request_id")
    reason = body.get("reason", "")
    auto_start = body.get("auto_start", True)

    if not request_id:
        return ResponseContext.bad_request("request_id is required")

    manager = _approval_manager()
    coordinator = _coordinator()
    actor = resolve_actor(ctx)

    result = manager.approve(
        request_id=request_id,
        approved_by=actor,
        reason=reason,
    )

    if result is None:
        return ResponseContext.not_found(f"Request not found: {request_id}")

    logger.info(
        "recovery_handler.approved",
        request_id=request_id,
        actor=actor,
    )

    response_data = {
        "request_id": request_id,
        "status": result.status.value,
        "approved_by": result.approved_by,
        "approved_at": (result.approved_at.isoformat() if result.approved_at else None),
    }

    # Auto-start after approval
    if auto_start:
        try:
            session = coordinator.start_recovery(
                namespace=result.namespace,
                trigger_level=result.trigger_level,
                initiated_by=actor,
            )
            response_data["session_id"] = session.id
            response_data["recovery_started"] = True
        except ValueError as e:
            response_data["recovery_started"] = False
            response_data["recovery_error"] = str(e)

    return ResponseContext.json(response_data)


def recovery_reject(ctx: RequestContext) -> ResponseContext:
    """POST /recovery/reject/ — reject a recovery request (authenticated)."""
    body = ctx.json_body or {}
    request_id = body.get("request_id")
    reason = body.get("reason", "")

    if not request_id:
        return ResponseContext.bad_request("request_id is required")

    if not reason:
        return ResponseContext.bad_request("reason is required for rejection")

    manager = _approval_manager()
    actor = resolve_actor(ctx)

    result = manager.reject(
        request_id=request_id,
        rejected_by=actor,
        reason=reason,
    )

    if result is None:
        return ResponseContext.not_found(f"Request not found: {request_id}")

    logger.info(
        "recovery_handler.rejected",
        request_id=request_id,
        reason=reason,
        actor=actor,
    )

    return ResponseContext.json(
        {
            "request_id": request_id,
            "status": result.status.value,
            "rejected_by": result.approved_by,
            "rejection_reason": reason,
        }
    )


def recovery_history(ctx: RequestContext) -> ResponseContext:
    """GET /recovery/history/ — recovery history (authenticated)."""
    namespace = ctx.get_query("namespace")

    try:
        limit = int(ctx.get_query("limit", 20))
    except (TypeError, ValueError):
        limit = 20

    coordinator = _coordinator()
    RecoveryStatus = _recovery_status_enum()

    # Session history
    history = coordinator.get_session_history(
        namespace=namespace,
        limit=limit,
    )

    return ResponseContext.json(
        {
            "history": [
                {
                    # started_at / completed_at are already ISO 8601 strings.
                    "session_id": s.id,
                    "status": s.status.value,
                    "namespace": s.namespace,
                    "started_at": s.started_at,
                    "completed_at": s.completed_at,
                    "steps_completed": sum(
                        1 for step in s.steps if step.status == RecoveryStatus.COMPLETED
                    ),
                    "total_steps": len(s.steps),
                }
                for s in history
            ],
            "total_count": len(history),
            "namespace_filter": namespace,
        }
    )


def recovery_dashboard_widget(ctx: RequestContext) -> ResponseContext:
    """GET /recovery/widget/ — dashboard widget data (authenticated)."""
    namespace = ctx.get_query("namespace", "global")

    service = _dashboard_service()
    widget_data = service.get_widget_data(namespace=namespace)
    return ResponseContext.json(widget_data.to_dict())
