"""
Framework-agnostic Chaos Safety handlers.

Extracted from api/django/views/chaos/safety_views.py (Phase 2b).

Endpoints:
    GET  /chaos/kill-switch           Kill switch status
    POST /chaos/kill-switch           Kill switch action
    POST /chaos/safety/check          Safety check
    POST /chaos/safety/blast-radius   Blast radius check
    GET  /chaos/config/stop-conditions    Stop conditions config
    PATCH /chaos/config/stop-conditions   Update stop conditions
    GET  /chaos/config/ttl            TTL config
    PATCH /chaos/config/ttl           Update TTL config
    GET  /chaos/config/dry-run        Dry-run config
    PATCH /chaos/config/dry-run       Update dry-run config
    POST /chaos/kill-all              Kill all experiments
"""

from __future__ import annotations

import time as _time

import structlog

from baldur.api.handlers._common import resolve_actor
from baldur.interfaces.web_framework import RequestContext, ResponseContext

logger = structlog.get_logger()

__all__ = [
    "kill_switch_status",
    "kill_switch_action",
    "safety_check",
    "blast_radius_check",
    "stop_conditions_config_get",
    "stop_conditions_config_update",
    "ttl_config_get",
    "ttl_config_update",
    "dry_run_config_get",
    "dry_run_config_update",
    "kill_all",
]


def _safety_guard():
    from baldur.factory.registry import ProviderRegistry

    guard = ProviderRegistry.safety_guard.safe_get()
    if guard is None:
        raise RuntimeError("Chaos safety handlers require baldur_pro SafetyGuard")
    return guard


def _scheduler():
    from baldur.factory.registry import ProviderRegistry

    scheduler = ProviderRegistry.chaos_scheduler.safe_get()
    if scheduler is None:
        raise RuntimeError("Chaos safety handlers require baldur_pro ChaosScheduler")
    return scheduler


def _runtime_config_manager():
    from baldur.factory.registry import ProviderRegistry

    manager = ProviderRegistry.runtime_config_manager.safe_get()
    if manager is None:
        raise RuntimeError(
            "Chaos safety handlers require baldur_pro RuntimeConfigManager"
        )
    return manager


def _blast_radius_manager():
    from baldur.factory.registry import ProviderRegistry

    manager = ProviderRegistry.blast_radius_manager.safe_get()
    if manager is None:
        raise RuntimeError(
            "Chaos safety handlers require baldur_pro BlastRadiusManager"
        )
    return manager


def kill_switch_status(ctx: RequestContext) -> ResponseContext:
    """GET /chaos/kill-switch — kill switch status (viewer)."""
    guard = _safety_guard()
    scheduler = _scheduler()

    global_blocked, block_reason = guard.is_globally_blocked()
    running = scheduler.get_running_experiments()

    current_mono = _time.monotonic()
    running_dict = {}
    for schedule_id, info in running.items():
        running_dict[schedule_id] = {
            "experiment_id": info.experiment_id,
            "elapsed_seconds": round(current_mono - info.started_at_monotonic, 1),
        }

    return ResponseContext.json(
        {
            "status": "success",
            "data": {
                "global_block_active": global_blocked,
                "global_block_reason": block_reason,
                "running_experiments": running_dict,
                "running_count": len(running_dict),
            },
        }
    )


def kill_switch_action(ctx: RequestContext) -> ResponseContext:
    """POST /chaos/kill-switch — kill switch action (admin)."""
    body = ctx.json_body or {}
    action = body.get("action")
    if not action:
        return ResponseContext.bad_request("action is required")

    reason = body.get("reason", "")
    experiment_id = body.get("experiment_id")

    guard = _safety_guard()
    scheduler = _scheduler()

    if action == "kill_one":
        if not experiment_id:
            return ResponseContext.bad_request("experiment_id is required for kill_one")
        killed = scheduler.kill_experiment(experiment_id, reason)
        message = (
            f"Experiment {experiment_id} killed"
            if killed
            else f"Experiment {experiment_id} not found"
        )
    elif action == "kill_all":
        count = scheduler.kill_all(reason)
        message = f"{count} experiments killed"
    elif action == "block_global":
        guard.block_globally(reason)
        message = "Global block activated"
    elif action == "unblock_global":
        guard.unblock_globally()
        message = "Global block deactivated"
    else:
        return ResponseContext.bad_request(f"Unknown action: {action}")

    actor = resolve_actor(ctx)
    logger.warning(
        "chaos_api.kill_switch",
        action=action,
        reason=reason,
        request_user=actor,
    )

    return ResponseContext.json(
        {"status": "success", "message": message, "action": action}
    )


def safety_check(ctx: RequestContext) -> ResponseContext:
    """POST /chaos/safety/check — safety check (viewer)."""
    body = ctx.json_body or {}
    guard = _safety_guard()
    result = guard.check(
        experiment_id=body.get("experiment_id"),
        target_service=body.get("target_service"),
        force=body.get("force"),
    )
    return ResponseContext.json({"status": "success", "data": result.to_dict()})


def blast_radius_check(ctx: RequestContext) -> ResponseContext:
    """POST /chaos/safety/blast-radius — blast radius check (viewer)."""
    body = ctx.json_body or {}
    if not body:
        return ResponseContext.bad_request("Request body is required")

    manager = _blast_radius_manager()
    result = manager.check(**body)
    return ResponseContext.json({"status": "success", "data": result.to_dict()})


def stop_conditions_config_get(ctx: RequestContext) -> ResponseContext:
    """GET /chaos/config/stop-conditions — stop conditions config (viewer)."""
    from baldur_pro.services.chaos.stop_conditions import get_stop_conditions_config

    config = get_stop_conditions_config()
    return ResponseContext.json({"status": "success", "data": config.to_dict()})


def stop_conditions_config_update(ctx: RequestContext) -> ResponseContext:
    """PATCH /chaos/config/stop-conditions — update stop conditions (admin)."""
    body = ctx.json_body or {}
    if not body:
        return ResponseContext.bad_request("Request body is required")

    manager = _runtime_config_manager()
    updated = manager.update_chaos_stop_conditions_config(**body)
    logger.info("chaos_api.stop_conditions_config_updated")
    return ResponseContext.json({"status": "success", "data": updated})


def ttl_config_get(ctx: RequestContext) -> ResponseContext:
    """GET /chaos/config/ttl — TTL config (viewer)."""
    from baldur_pro.services.chaos.stop_conditions import get_ttl_config

    config = get_ttl_config()
    return ResponseContext.json({"status": "success", "data": config.to_dict()})


def ttl_config_update(ctx: RequestContext) -> ResponseContext:
    """PATCH /chaos/config/ttl — update TTL config (admin)."""
    body = ctx.json_body or {}
    if not body:
        return ResponseContext.bad_request("Request body is required")

    manager = _runtime_config_manager()
    updated = manager.update_chaos_ttl_config(**body)
    logger.info("chaos_api.ttl_config_updated")
    return ResponseContext.json({"status": "success", "data": updated})


def dry_run_config_get(ctx: RequestContext) -> ResponseContext:
    """GET /chaos/config/dry-run — dry-run config (viewer)."""
    from baldur_pro.services.chaos.stop_conditions import get_dry_run_config

    config = get_dry_run_config()
    return ResponseContext.json({"status": "success", "data": config.to_dict()})


def dry_run_config_update(ctx: RequestContext) -> ResponseContext:
    """PATCH /chaos/config/dry-run — update dry-run config (admin)."""
    body = ctx.json_body or {}
    if not body:
        return ResponseContext.bad_request("Request body is required")

    manager = _runtime_config_manager()
    updated = manager.update_chaos_dry_run_config(**body)
    logger.info("chaos_api.dry_run_config_updated")
    return ResponseContext.json({"status": "success", "data": updated})


def kill_all(ctx: RequestContext) -> ResponseContext:
    """POST /chaos/kill-all — kill all experiments (admin)."""
    body = ctx.json_body or {}
    reason = body.get("reason", "")
    actor = resolve_actor(ctx)
    operator = body.get("operator", actor)

    scheduler = _scheduler()
    experiments_killed = scheduler.kill_all(reason=reason)
    rollbacks_initiated = experiments_killed

    ttl_configs_cleared = 0
    try:
        manager = _runtime_config_manager()
        chaos_config = manager.get_chaos_config()
        active_ttl = chaos_config.get("active_ttl_configs")
        if active_ttl:
            ttl_configs_cleared = len(active_ttl)
            manager.clear_active_ttl_configs()
    except Exception as e:
        logger.warning("chaos_api.kill_all_ttl_clear_failed", error=str(e))

    logger.warning(
        "chaos_api.kill_all_executed",
        operator=operator,
        reason=reason,
        experiments_killed=experiments_killed,
    )

    return ResponseContext.json(
        {
            "status": "success",
            "data": {
                "experiments_killed": experiments_killed,
                "rollbacks_initiated": rollbacks_initiated,
                "ttl_configs_cleared": ttl_configs_cleared,
            },
        }
    )
