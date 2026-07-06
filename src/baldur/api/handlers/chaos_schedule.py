"""
Framework-agnostic Chaos Schedule handlers.

Extracted from api/django/views/chaos/schedule_views.py (Phase 2b).

Endpoints:
    GET  /chaos/schedules                    Schedule list
    POST /chaos/schedules                    Create schedule
    GET  /chaos/schedules/{schedule_id}      Schedule detail
    PATCH /chaos/schedules/{schedule_id}     Update schedule
    DELETE /chaos/schedules/{schedule_id}    Delete schedule
    POST /chaos/schedules/{schedule_id}/approval  Approve/deny schedule
    POST /chaos/schedules/{schedule_id}/execute   Execute schedule now
    GET  /chaos/pending-approvals            Pending approvals
"""

from __future__ import annotations

import structlog

from baldur.api.handlers._common import resolve_actor
from baldur.interfaces.web_framework import RequestContext, ResponseContext

logger = structlog.get_logger()

__all__ = [
    "chaos_schedule_list",
    "chaos_schedule_create",
    "chaos_schedule_detail",
    "chaos_schedule_update",
    "chaos_schedule_delete",
    "chaos_schedule_approval",
    "chaos_schedule_execute",
    "chaos_pending_approvals",
]


def _scheduler():
    from baldur.factory.registry import ProviderRegistry

    scheduler = ProviderRegistry.chaos_scheduler.safe_get()
    if scheduler is None:
        raise RuntimeError("Chaos schedule handlers require baldur_pro ChaosScheduler")
    return scheduler


def chaos_schedule_list(ctx: RequestContext) -> ResponseContext:
    """GET /chaos/schedules — schedule list (viewer)."""
    enabled_only = ctx.get_query("enabled_only", "false").lower() == "true"
    pending_approval_only = (
        ctx.get_query("pending_approval_only", "false").lower() == "true"
    )
    target_service = ctx.get_query("target_service")

    scheduler = _scheduler()
    schedules = scheduler.list_schedules(
        enabled_only=enabled_only,
        pending_approval_only=pending_approval_only,
        target_service=target_service,
    )
    return ResponseContext.json(
        {
            "status": "success",
            "data": [s.to_dict() for s in schedules],
            "count": len(schedules),
        }
    )


def chaos_schedule_create(ctx: RequestContext) -> ResponseContext:
    """POST /chaos/schedules — create schedule (admin)."""
    body = ctx.json_body or {}
    if not body:
        return ResponseContext.bad_request("Request body is required")

    actor = resolve_actor(ctx)
    experiment_config = body.pop("experiment_config", {})

    scheduler = _scheduler()
    schedule = scheduler.create_schedule(
        created_by=actor,
        experiment_config=experiment_config,
        **body,
    )
    logger.info(
        "chaos_api.schedule_created",
        schedule=schedule.id,
        request_user=actor,
    )
    return ResponseContext.created({"status": "success", "data": schedule.to_dict()})


def chaos_schedule_detail(ctx: RequestContext) -> ResponseContext:
    """GET /chaos/schedules/{schedule_id} — schedule detail (viewer)."""
    schedule_id = ctx.get_path_param("schedule_id")
    scheduler = _scheduler()
    schedule = scheduler.get_schedule(schedule_id)

    if not schedule:
        return ResponseContext.not_found("Schedule not found")

    return ResponseContext.json({"status": "success", "data": schedule.to_dict()})


def chaos_schedule_update(ctx: RequestContext) -> ResponseContext:
    """PATCH /chaos/schedules/{schedule_id} — update schedule (admin)."""
    schedule_id = ctx.get_path_param("schedule_id")
    body = ctx.json_body or {}

    scheduler = _scheduler()
    schedule = scheduler.update_schedule(schedule_id, **body)

    if not schedule:
        return ResponseContext.not_found("Schedule not found")

    actor = resolve_actor(ctx)
    logger.info(
        "chaos_api.schedule_updated",
        schedule_id=schedule_id,
        request_user=actor,
    )
    return ResponseContext.json({"status": "success", "data": schedule.to_dict()})


def chaos_schedule_delete(ctx: RequestContext) -> ResponseContext:
    """DELETE /chaos/schedules/{schedule_id} — delete schedule (admin)."""
    schedule_id = ctx.get_path_param("schedule_id")
    scheduler = _scheduler()
    deleted = scheduler.delete_schedule(schedule_id)

    if not deleted:
        return ResponseContext.not_found("Schedule not found")

    actor = resolve_actor(ctx)
    logger.info(
        "chaos_api.schedule_deleted",
        schedule_id=schedule_id,
        request_user=actor,
    )
    return ResponseContext.json(
        {"status": "success", "message": "Schedule deleted"}, status_code=204
    )


def chaos_schedule_approval(ctx: RequestContext) -> ResponseContext:
    """POST /chaos/schedules/{schedule_id}/approval — approve/deny (admin)."""
    schedule_id = ctx.get_path_param("schedule_id")
    body = ctx.json_body or {}
    action = body.get("action")

    if not action:
        return ResponseContext.bad_request("action is required")

    actor = resolve_actor(ctx)
    reason = body.get("reason", "")
    scheduler = _scheduler()

    if action == "approve":
        schedule = scheduler.approve_schedule(schedule_id, approved_by=actor)
    else:
        schedule = scheduler.deny_schedule(schedule_id, denied_by=actor, reason=reason)

    if not schedule:
        return ResponseContext.not_found("Schedule not found")

    logger.info(
        "chaos_api.schedule",
        action=action,
        schedule_id=schedule_id,
        request_user=actor,
    )
    return ResponseContext.json({"status": "success", "data": schedule.to_dict()})


def chaos_schedule_execute(ctx: RequestContext) -> ResponseContext:
    """POST /chaos/schedules/{schedule_id}/execute — execute now (admin)."""
    schedule_id = ctx.get_path_param("schedule_id")
    body = ctx.json_body or {}
    force = body.get("force", False)

    scheduler = _scheduler()
    result = scheduler.execute_now(schedule_id, force=force)

    actor = resolve_actor(ctx)
    logger.info(
        "chaos_api.schedule_executed",
        schedule_id=schedule_id,
        request_user=actor,
        execution_status=result.status,
    )
    return ResponseContext.json({"status": "success", "data": result.to_dict()})


def chaos_pending_approvals(ctx: RequestContext) -> ResponseContext:
    """GET /chaos/pending-approvals — pending approvals (viewer)."""
    from baldur.factory.registry import ProviderRegistry

    manager = ProviderRegistry.blast_radius_manager.safe_get()
    if manager is None:
        raise RuntimeError(
            "Chaos pending approvals require baldur_pro BlastRadiusManager"
        )
    scheduler = _scheduler()

    blast_radius_pending = manager.get_pending_approvals()
    schedule_pending = scheduler.list_schedules(pending_approval_only=True)

    return ResponseContext.json(
        {
            "status": "success",
            "data": {
                "blast_radius_approvals": [a.to_dict() for a in blast_radius_pending],
                "schedule_approvals": [s.to_dict() for s in schedule_pending],
            },
            "total_pending": len(blast_radius_pending) + len(schedule_pending),
        }
    )
