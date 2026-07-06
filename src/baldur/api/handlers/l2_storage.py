"""
Framework-agnostic L2 Storage handlers.

Extracted from api/django/views/l2_storage_status.py, l2_storage_drift.py,
and l2_storage_config.py (Phase 2b).

Endpoints:
    GET  /l2-storage/status               Storage status
    GET  /l2-storage/health               L2 health status
    POST /l2-storage/health/reset         Reset L2 health
    POST /l2-storage/sync/from-l2         Force sync from L2
    POST /l2-storage/sync/to-l2           Force sync to L2
    GET  /l2-storage/metrics              Storage metrics
    GET  /l2-storage/drift/stats          Drift reconciliation stats
    GET  /l2-storage/drift/history        Drift reconciliation history
    POST /l2-storage/drift/reconcile      Force drift reconciliation
    POST /l2-storage/drift/reconcile/{service_name}  Reconcile single service
    GET  /l2-storage/config               L2 storage config
    PUT  /l2-storage/config               Update L2 storage config
    POST /l2-storage/config/reset         Reset config to defaults
"""

from __future__ import annotations

import structlog

from baldur.api.handlers._common import resolve_actor
from baldur.interfaces.web_framework import RequestContext, ResponseContext
from baldur.utils.time import utc_now

logger = structlog.get_logger()

__all__ = [
    "l2_storage_status",
    "l2_storage_health",
    "l2_storage_health_reset",
    "l2_storage_sync_from_l2",
    "l2_storage_sync_to_l2",
    "l2_storage_metrics",
    "drift_reconciliation_stats",
    "drift_reconciliation_history",
    "drift_reconciliation_trigger",
    "drift_reconciliation_service",
    "l2_storage_config_get",
    "l2_storage_config_update",
    "l2_storage_config_reset",
]


def _layered_repo():
    try:
        from baldur.adapters.memory.circuit_breaker import (
            LayeredCircuitBreakerStateRepository,
        )
        from baldur.factory import ProviderRegistry

        repo = ProviderRegistry.get_circuit_breaker_repo(name="layered")
        if isinstance(repo, LayeredCircuitBreakerStateRepository):
            return repo
        return None
    except Exception as e:
        logger.warning("l2_storage.get_layered_repository_failed", error=e)
        return None


def _runtime_config():
    from baldur.settings.l2_storage import get_l2_storage_runtime_config

    return get_l2_storage_runtime_config()


def _not_configured_response(extra: dict | None = None) -> ResponseContext:
    data = {
        "status": "success",
        "message": "Layered storage not configured",
        "timestamp": utc_now().isoformat(),
    }
    if extra:
        data.update(extra)
    return ResponseContext.json(data)


def l2_storage_status(ctx: RequestContext) -> ResponseContext:
    """GET /l2-storage/status — storage status (viewer)."""
    repo = _layered_repo()
    if repo is None:
        return _not_configured_response({"storage_type": "memory_only"})

    return ResponseContext.json(
        {
            "status": "success",
            "storage_info": repo.get_storage_info(),
            "timestamp": utc_now().isoformat(),
        }
    )


def l2_storage_health(ctx: RequestContext) -> ResponseContext:
    """GET /l2-storage/health — L2 health status (viewer)."""
    repo = _layered_repo()
    if repo is None:
        return _not_configured_response(
            {
                "health": {
                    "healthy": True,
                    "message": "Layered storage not configured (memory-only mode)",
                }
            }
        )

    return ResponseContext.json(
        {
            "status": "success",
            "health": repo.get_l2_health(),
            "timestamp": utc_now().isoformat(),
        }
    )


def l2_storage_health_reset(ctx: RequestContext) -> ResponseContext:
    """POST /l2-storage/health/reset — reset L2 health (admin)."""
    repo = _layered_repo()
    if repo is None:
        return ResponseContext.bad_request("Layered storage not configured")

    repo.reset_l2_health()
    actor = resolve_actor(ctx)
    logger.info("l2_storage_api.health_reset", request_user=actor)

    return ResponseContext.json(
        {
            "status": "success",
            "message": "L2 health status reset",
            "health": repo.get_l2_health(),
            "timestamp": utc_now().isoformat(),
        }
    )


def l2_storage_sync_from_l2(ctx: RequestContext) -> ResponseContext:
    """POST /l2-storage/sync/from-l2 — force sync from L2 (admin)."""
    repo = _layered_repo()
    if repo is None:
        return ResponseContext.bad_request("Layered storage not configured")

    success = repo.force_sync_from_l2()
    if not success:
        return ResponseContext.server_error("Sync from L2 failed")

    actor = resolve_actor(ctx)
    logger.info("l2_storage_api.force_sync", request_user=actor)

    return ResponseContext.json(
        {
            "status": "success",
            "message": "Synced from L2 successfully",
            "storage_info": repo.get_storage_info(),
            "timestamp": utc_now().isoformat(),
        }
    )


def l2_storage_sync_to_l2(ctx: RequestContext) -> ResponseContext:
    """POST /l2-storage/sync/to-l2 — force sync to L2 (admin)."""
    repo = _layered_repo()
    if repo is None:
        return ResponseContext.bad_request("Layered storage not configured")

    result = repo.force_sync_to_l2()
    actor = resolve_actor(ctx)
    logger.info("l2_storage_api.force_sync", request_user=actor, result=result)

    return ResponseContext.json(
        {
            "status": "success" if result["success"] else "partial",
            "message": "Sync to L2 completed",
            "result": result,
            "timestamp": utc_now().isoformat(),
        }
    )


def l2_storage_metrics(ctx: RequestContext) -> ResponseContext:
    """GET /l2-storage/metrics — storage metrics (viewer)."""
    repo = _layered_repo()
    if repo is None:
        return _not_configured_response({"metrics": {}})

    metrics = repo.get_metrics()
    if metrics.get("l2_latency_count", 0) > 0:
        metrics["avg_latency_ms"] = round(
            metrics["l2_latency_total_ms"] / metrics["l2_latency_count"], 2
        )
    else:
        metrics["avg_latency_ms"] = 0.0

    return ResponseContext.json(
        {
            "status": "success",
            "metrics": metrics,
            "timestamp": utc_now().isoformat(),
        }
    )


def drift_reconciliation_stats(ctx: RequestContext) -> ResponseContext:
    """GET /l2-storage/drift/stats — drift reconciliation stats (viewer)."""
    repo = _layered_repo()
    if repo is None:
        return _not_configured_response({"stats": {}})

    return ResponseContext.json(
        {
            "status": "success",
            "stats": repo.get_drift_reconciler_stats(),
            "timestamp": utc_now().isoformat(),
        }
    )


def drift_reconciliation_history(ctx: RequestContext) -> ResponseContext:
    """GET /l2-storage/drift/history — drift reconciliation history (viewer)."""
    repo = _layered_repo()
    if repo is None:
        return _not_configured_response({"history": []})

    try:
        limit = int(ctx.get_query("limit", 100))
    except (TypeError, ValueError):
        limit = 100

    history = repo.get_drift_reconciliation_history()
    if len(history) > limit:
        history = history[-limit:]

    return ResponseContext.json(
        {
            "status": "success",
            "count": len(history),
            "history": history,
            "timestamp": utc_now().isoformat(),
        }
    )


def drift_reconciliation_trigger(ctx: RequestContext) -> ResponseContext:
    """POST /l2-storage/drift/reconcile — force drift reconciliation (admin)."""
    repo = _layered_repo()
    if repo is None:
        return ResponseContext.bad_request("Layered storage not configured")

    result = repo.force_drift_reconciliation()
    actor = resolve_actor(ctx)
    logger.info(
        "l2_storage_api.manual_drift_reconciliation",
        request_user=actor,
        reconciled_count=result.get("reconciled", 0),
        l1_wins=result.get("l1_wins", 0),
        l2_wins=result.get("l2_wins", 0),
    )

    return ResponseContext.json(
        {
            "status": "success" if result.get("success", False) else "partial",
            "message": "Drift reconciliation completed",
            "result": result,
            "timestamp": utc_now().isoformat(),
        }
    )


def drift_reconciliation_service(ctx: RequestContext) -> ResponseContext:
    """POST /l2-storage/drift/reconcile/{service_name} — reconcile single service (admin)."""
    service_name = ctx.get_path_param("service_name")
    repo = _layered_repo()
    if repo is None:
        return ResponseContext.bad_request("Layered storage not configured")

    result = repo.reconcile_single_service(service_name)

    if not result.get("success", False):
        reason = result.get("reason", "Reconciliation failed")
        logger.warning(
            "l2_storage_api.drift_reconciliation_failed",
            service_name=service_name,
            reconcile_failure_reason=reason,
        )
        return ResponseContext.bad_request(reason)

    actor = resolve_actor(ctx)
    logger.info(
        "l2_storage_api.drift_reconciliation",
        service_name=service_name,
        request_user=actor,
        reconcile_action=result.get("action", "none"),
        winner=result.get("winner", "n/a"),
    )

    return ResponseContext.json(
        {
            "status": "success",
            "message": f"Drift reconciliation for {service_name} completed",
            "service_name": service_name,
            "result": result,
            "timestamp": utc_now().isoformat(),
        }
    )


def l2_storage_config_get(ctx: RequestContext) -> ResponseContext:
    """GET /l2-storage/config — L2 storage config (viewer)."""
    config = _runtime_config()
    return ResponseContext.json(
        {
            "status": "success",
            "config": config.to_dict(),
            "timestamp": utc_now().isoformat(),
        }
    )


def l2_storage_config_update(ctx: RequestContext) -> ResponseContext:
    """PUT /l2-storage/config — update L2 storage config (admin)."""
    body = ctx.json_body or {}
    if not body:
        return ResponseContext.bad_request("No changes provided")

    actor = resolve_actor(ctx)
    config = _runtime_config()
    updated_config = config.update(**body, updated_by=actor)

    logger.info(
        "l2_storage_api.config_updated",
        request_user=actor,
        changes=body,
    )

    return ResponseContext.json(
        {
            "status": "success",
            "message": "L2 storage configuration updated",
            "config": updated_config,
            "changes": body,
            "timestamp": utc_now().isoformat(),
        }
    )


def l2_storage_config_reset(ctx: RequestContext) -> ResponseContext:
    """POST /l2-storage/config/reset — reset to defaults (admin)."""
    actor = resolve_actor(ctx)
    config = _runtime_config()
    config.reset()

    logger.info("l2_storage_api.config_reset_defaults", request_user=actor)

    return ResponseContext.json(
        {
            "status": "success",
            "message": "L2 storage configuration reset to defaults",
            "config": config.to_dict(),
            "timestamp": utc_now().isoformat(),
        }
    )
