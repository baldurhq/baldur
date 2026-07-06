"""
Framework-agnostic Canary Rollout handlers.

Extracted from api/django/views/canary.py (Phase 2b).

Endpoints:
    GET   /canary/rollouts                         Active rollout list
    POST  /canary/rollouts                         Create rollout
    GET   /canary/rollouts/{rollout_id}             Rollout detail
    POST  /canary/rollouts/{rollout_id}/{action}    Rollout action (start/promote/rollback/pause/resume/cancel)
    POST  /canary/panic-rollback                    Panic rollback all active rollouts
    GET   /canary/rollouts/{rollout_id}/metrics     Rollout metrics
    GET   /canary/history                           Rollout history
"""

from __future__ import annotations

import structlog

from baldur.api.handlers._common import resolve_actor
from baldur.interfaces.web_framework import RequestContext, ResponseContext

logger = structlog.get_logger()

__all__ = [
    "canary_rollout_list",
    "canary_rollout_create",
    "canary_rollout_detail",
    "canary_rollout_action",
    "canary_panic_rollback",
    "canary_rollout_metrics",
    "canary_rollout_history",
]


def _service():
    from baldur.factory.registry import ProviderRegistry

    service = ProviderRegistry.canary_rollout_service.safe_get()
    if service is None:
        raise RuntimeError("Canary handlers require baldur_pro CanaryRolloutService")
    return service


def _format_rollout_summary(rollout) -> dict:
    return {
        "id": rollout.id,
        "config_type": rollout.config_type,
        "state": rollout.state.value,
        "current_stage": (
            rollout.current_stage.name if rollout.current_stage else None
        ),
        "current_stage_index": rollout.current_stage_index,
        "total_stages": len(rollout.stages),
        "affected_clusters": rollout.affected_clusters,
        "created_by": rollout.created_by,
        "created_at": rollout.created_at.isoformat(),
        "progress_percentage": rollout.progress_percentage,
    }


def _format_rollout_detail(rollout) -> dict:
    return {
        "id": rollout.id,
        "config_type": rollout.config_type,
        "state": rollout.state.value,
        "current_stage_index": rollout.current_stage_index,
        "previous_values": rollout.previous_values,
        "new_values": rollout.new_values,
        "stages": [
            {
                "name": s.name,
                "clusters": s.clusters,
                "percentage": s.percentage,
                "duration_minutes": s.duration_minutes,
                "auto_promote": s.auto_promote,
            }
            for s in rollout.stages
        ],
        "affected_clusters": rollout.affected_clusters,
        "created_by": rollout.created_by,
        "created_at": rollout.created_at.isoformat(),
        "completed_at": (
            rollout.completed_at.isoformat() if rollout.completed_at else None
        ),
        "reason": rollout.reason,
        "rollback_reason": rollout.rollback_reason,
        "progress_percentage": rollout.progress_percentage,
        "is_terminal": rollout.is_terminal,
    }


def canary_rollout_list(ctx: RequestContext) -> ResponseContext:
    """GET /canary/rollouts — active rollout list (viewer)."""
    service = _service()

    include_completed = ctx.get_query("include_completed", "false").lower() == "true"
    config_type_filter = ctx.get_query("config_type")

    rollouts = service.get_active_rollouts()

    if include_completed:
        try:
            from baldur.settings.canary import get_canary_settings

            limit = get_canary_settings().default_completed_rollouts_limit
        except Exception:
            limit = 20
        completed = service.get_completed_rollouts(limit=limit)
        rollouts = rollouts + completed

    if config_type_filter:
        rollouts = [r for r in rollouts if r.config_type == config_type_filter]

    return ResponseContext.json(
        {
            "status": "success",
            "count": len(rollouts),
            "rollouts": [_format_rollout_summary(r) for r in rollouts],
        }
    )


def canary_rollout_create(ctx: RequestContext) -> ResponseContext:
    """POST /canary/rollouts — create rollout (admin)."""
    body = ctx.json_body or {}

    config_type = body.get("config_type")
    if not config_type:
        return ResponseContext.bad_request("config_type is required")

    new_values = body.get("new_values", {})
    if not new_values:
        return ResponseContext.bad_request("new_values is required")

    stages_data = body.get("stages", [])
    if not stages_data:
        return ResponseContext.bad_request("At least one stage is required")

    from baldur.models.canary import CanaryStage

    stages = [
        CanaryStage(
            name=s.get("name", f"stage_{i}"),
            clusters=s.get("clusters", []),
            percentage=float(s.get("percentage", 0)),
            duration_minutes=int(s.get("duration_minutes", 5)),
            auto_promote=s.get("auto_promote", True),
        )
        for i, s in enumerate(stages_data)
    ]

    actor = resolve_actor(ctx)
    service = _service()
    try:
        rollout = service.create_rollout(
            config_type=config_type,
            new_values=new_values,
            stages=stages,
            created_by=actor,
            reason=body.get("reason", ""),
            force_during_chaos=body.get("force_during_chaos", False),
        )
    except ValueError as exc:
        # Service-side validation (empty stages / active-chaos conflict guard).
        return ResponseContext.bad_request(str(exc))
    except Exception as exc:  # noqa: BLE001
        # ConfigLockError (PRO): another active rollout already holds this
        # config_type's lock — a client conflict, not a server fault. Matched by
        # name so OSS handler code does not import the baldur_pro symbol (the
        # PRO service is resolved via the registry, never imported here). Any
        # other exception is genuinely unexpected and propagates to a 500.
        if type(exc).__name__ == "ConfigLockError":
            return ResponseContext.error(
                str(exc), status_code=409, error_code="ROLLOUT_CONFLICT"
            )
        raise

    logger.info(
        "canary_api.rollout_created",
        rollout=rollout.id,
        config_type=config_type,
        request_user=actor,
    )

    return ResponseContext.created(
        {
            "status": "success",
            "rollout": _format_rollout_detail(rollout),
        }
    )


def canary_rollout_detail(ctx: RequestContext) -> ResponseContext:
    """GET /canary/rollouts/{rollout_id} — rollout detail (viewer)."""
    rollout_id = ctx.get_path_param("rollout_id")
    service = _service()
    rollout = service.get_rollout(rollout_id)

    if not rollout:
        return ResponseContext.not_found("Rollout not found")

    return ResponseContext.json(
        {
            "status": "success",
            "rollout": _format_rollout_detail(rollout),
        }
    )


VALID_ACTIONS = ["start", "promote", "rollback", "pause", "resume", "cancel"]


def _execute_action(service, rollout_id, rollout, action, body):
    if action == "start":
        success = service.start_rollout(rollout_id)
        return success, (
            None if success else f"Cannot start rollout in state: {rollout.state.value}"
        )
    if action == "promote":
        force = body.get("force", False)
        success = service.promote(rollout_id, force=force)
        return success, (
            None
            if success
            else "Promotion failed. Check the stage metrics or rollout state."
        )
    if action == "rollback":
        reason = body.get("reason", "Manual rollback")
        success = service.rollback(rollout_id, reason=reason)
        return success, (
            None
            if success
            else (
                "Only a started rollout can be rolled back "
                f"(this one is in '{rollout.state.value}'). "
                "Use Cancel for a not-yet-started one."
            )
        )
    if action == "pause":
        success = service.pause(rollout_id, triggered_by="manual")
        return success, (
            None if success else "Pause only works on a rollout in the CANARY state."
        )
    if action == "resume":
        success = service.resume(rollout_id)
        return success, (
            None if success else "Resume only works on a rollout in the PAUSED state."
        )
    if action == "cancel":
        success = service.cancel(rollout_id)
        return success, (
            None
            if success
            else (
                "Only a not-yet-started rollout can be cancelled "
                f"(this one is in '{rollout.state.value}'). "
                "Use Rollback to revert a started one."
            )
        )
    return False, f"Unknown action: {action}"


def canary_rollout_action(ctx: RequestContext) -> ResponseContext:
    """POST /canary/rollouts/{rollout_id}/{action} — rollout action (admin)."""
    rollout_id = ctx.get_path_param("rollout_id")
    action = ctx.get_path_param("action")

    if action not in VALID_ACTIONS:
        return ResponseContext.bad_request(
            f"Unknown action: {action}. Valid actions: {VALID_ACTIONS}"
        )

    service = _service()
    rollout = service.get_rollout(rollout_id)

    if not rollout:
        return ResponseContext.not_found("Rollout not found")

    body = ctx.json_body or {}
    success, error_message = _execute_action(service, rollout_id, rollout, action, body)

    if not success:
        return ResponseContext.bad_request(error_message or "Action failed")

    updated_rollout = service.get_rollout(rollout_id)

    logger.info(
        "canary_api.action_executed",
        rollout_id=rollout_id,
        action=action,
        updated_state=updated_rollout.state.value if updated_rollout else "unknown",
    )

    return ResponseContext.json(
        {
            "status": "success",
            "action": action,
            "rollout": (
                _format_rollout_summary(updated_rollout) if updated_rollout else None
            ),
        }
    )


def canary_panic_rollback(ctx: RequestContext) -> ResponseContext:
    """POST /canary/panic-rollback — panic rollback all active rollouts (admin)."""
    from baldur.models.canary import CanaryState

    service = _service()
    actor = resolve_actor(ctx)
    body = ctx.json_body or {}

    reason = body.get("reason", f"Panic rollback by {actor}")
    emergency_code = body.get("emergency_code", "")

    active_rollouts = service.get_active_rollouts()

    if not active_rollouts:
        return ResponseContext.json(
            {
                "status": "success",
                "message": "No active rollouts to rollback",
                "rolled_back": [],
            }
        )

    results = []
    for rollout in active_rollouts:
        try:
            # A never-started (CREATED) rollout has applied nothing — rollback
            # would be an off-state-machine transition. Cancel is its correct
            # terminal exit: it releases the config lock and clears the slot.
            if rollout.state == CanaryState.CREATED:
                success = service.cancel(rollout.id, reason=f"[PANIC] {reason}")
                action = "cancelled"
            else:
                success = service.rollback(rollout.id, reason=f"[PANIC] {reason}")
                action = "rolled_back"
            results.append(
                {
                    "id": rollout.id,
                    "config_type": rollout.config_type,
                    "success": success,
                    "action": action,
                    "affected_clusters": rollout.affected_clusters,
                }
            )
        except Exception as e:
            results.append(
                {
                    "id": rollout.id,
                    "config_type": rollout.config_type,
                    "success": False,
                    "error": str(e),
                }
            )

    success_count = sum(1 for r in results if r.get("success"))

    logger.warning(
        "canary_api.panic_rollback_executed",
        request_user=actor,
        reason=reason,
        emergency_code=emergency_code,
        success_count=success_count,
        results_count=len(results),
    )

    return ResponseContext.json(
        {
            "status": "success",
            "message": f"Panic rollback completed: {success_count}/{len(results)} successful",
            "emergency_code": emergency_code,
            "rolled_back": results,
        }
    )


def canary_rollout_metrics(ctx: RequestContext) -> ResponseContext:
    """GET /canary/rollouts/{rollout_id}/metrics — rollout metrics (viewer)."""
    rollout_id = ctx.get_path_param("rollout_id")
    service = _service()
    rollout = service.get_rollout(rollout_id)

    if not rollout:
        return ResponseContext.not_found("Rollout not found")

    metrics = service.collect_metrics(rollout_id)

    return ResponseContext.json(
        {
            "status": "success",
            "rollout_id": rollout_id,
            "state": rollout.state.value,
            "current_stage": (
                rollout.current_stage.name if rollout.current_stage else None
            ),
            "metrics": (
                [
                    {
                        "cluster": m.cluster,
                        "stage_name": m.stage_name,
                        "error_rate_before": m.error_rate_before,
                        "error_rate_after": m.error_rate_after,
                        "latency_p50_before": m.latency_p50_before,
                        "latency_p50_after": m.latency_p50_after,
                        "latency_p99_before": m.latency_p99_before,
                        "latency_p99_after": m.latency_p99_after,
                        "requests_total": m.requests_total,
                        "errors_total": m.errors_total,
                        "is_healthy": m.is_healthy,
                        "unhealthy_reason": m.unhealthy_reason,
                    }
                    for m in metrics
                ]
                if metrics
                else []
            ),
        }
    )


def canary_rollout_history(ctx: RequestContext) -> ResponseContext:
    """GET /canary/history — rollout history (viewer)."""
    service = _service()

    config_type = ctx.get_query("config_type")
    state_filter = ctx.get_query("state")

    try:
        limit = int(ctx.get_query("limit", 20))
        limit = min(max(limit, 1), 100)
    except (TypeError, ValueError):
        limit = 20

    rollouts = service.get_completed_rollouts(limit=limit)

    if config_type:
        rollouts = [r for r in rollouts if r.config_type == config_type]

    if state_filter:
        try:
            from baldur.models.canary import CanaryState

            target_state = CanaryState(state_filter)
            rollouts = [r for r in rollouts if r.state == target_state]
        except ValueError:
            pass

    return ResponseContext.json(
        {
            "status": "success",
            "count": len(rollouts),
            "history": [_format_rollout_summary(r) for r in rollouts],
        }
    )
