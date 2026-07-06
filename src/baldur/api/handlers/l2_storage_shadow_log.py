"""
Framework-agnostic L2 Storage Shadow Log handlers.

Extracted from api/django/views/l2_storage_shadow_log.py (Phase 2b).

Endpoints:
    GET  /l2-storage/shadow-log                      Shadow log entries
    GET  /l2-storage/shadow-log/stats                Shadow log stats
    POST /l2-storage/shadow-log/clear                Clear shadow log
    GET  /l2-storage/shadow-log/analyze              Analyze L2 failures
    POST /l2-storage/shadow-log/replay               Replay unsynced records
    GET  /l2-storage/shadow-log/service/{service_name}  Logs by service
"""

from __future__ import annotations

import structlog

from baldur.api.handlers._common import resolve_actor
from baldur.interfaces.web_framework import RequestContext, ResponseContext
from baldur.utils.time import utc_now

logger = structlog.get_logger()

__all__ = [
    "shadow_log_list",
    "shadow_log_stats",
    "shadow_log_clear",
    "shadow_log_analyze",
    "shadow_log_replay",
    "shadow_log_by_service",
]


def _shadow_logger():
    try:
        from baldur.adapters.memory.circuit_breaker import get_shadow_logger

        return get_shadow_logger()
    except Exception as e:
        logger.warning("l2_storage.get_shadow_logger_failed", error=e)
        return None


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


def _serialize_record(r) -> dict:
    return {
        "service_name": r.service_name,
        "intended_state": r.intended_state,
        "failure_time": r.failure_time.isoformat(),
        "error_message": r.error_message,
        "l1_state_at_failure": r.l1_state_at_failure,
        "adapter_type": r.adapter_type,
        "operation": r.operation,
        "synced_after_recovery": r.synced_after_recovery,
        "recovery_time": (r.recovery_time.isoformat() if r.recovery_time else None),
    }


def shadow_log_list(ctx: RequestContext) -> ResponseContext:
    """GET /l2-storage/shadow-log — shadow log entries (viewer)."""
    sl = _shadow_logger()
    if sl is None:
        return ResponseContext.server_error("Shadow logger not available")

    unsynced_only = ctx.get_query("unsynced_only", "false").lower() == "true"
    try:
        limit = int(ctx.get_query("limit", 100))
    except (TypeError, ValueError):
        limit = 100

    records = sl.get_unsynced_records() if unsynced_only else sl.get_all_records()
    if len(records) > limit:
        records = records[-limit:]

    entries = [_serialize_record(r) for r in records]
    return ResponseContext.json(
        {
            "status": "success",
            "count": len(entries),
            "entries": entries,
            "timestamp": utc_now().isoformat(),
        }
    )


def shadow_log_stats(ctx: RequestContext) -> ResponseContext:
    """GET /l2-storage/shadow-log/stats — shadow log stats (viewer)."""
    sl = _shadow_logger()
    if sl is None:
        return ResponseContext.server_error("Shadow logger not available")

    return ResponseContext.json(
        {
            "status": "success",
            "stats": sl.get_stats(),
            "timestamp": utc_now().isoformat(),
        }
    )


def shadow_log_clear(ctx: RequestContext) -> ResponseContext:
    """POST /l2-storage/shadow-log/clear — clear shadow log (admin)."""
    sl = _shadow_logger()
    if sl is None:
        return ResponseContext.server_error("Shadow logger not available")

    stats_before = sl.get_stats()
    sl.clear()

    actor = resolve_actor(ctx)
    logger.warning(
        "l2_storage_api.shadow_log_cleared_cleared",
        request_user=actor,
        stats_before=stats_before["total_records"],
    )

    return ResponseContext.json(
        {
            "status": "success",
            "message": "Shadow log cleared",
            "cleared_count": stats_before["total_records"],
            "timestamp": utc_now().isoformat(),
        }
    )


def shadow_log_analyze(ctx: RequestContext) -> ResponseContext:
    """GET /l2-storage/shadow-log/analyze — analyze L2 failures (viewer)."""
    sl = _shadow_logger()
    if sl is None:
        return ResponseContext.server_error("Shadow logger not available")

    return ResponseContext.json(
        {
            "status": "success",
            "analysis": sl.analyze_l2_failures(),
            "timestamp": utc_now().isoformat(),
        }
    )


def shadow_log_replay(ctx: RequestContext) -> ResponseContext:
    """POST /l2-storage/shadow-log/replay — replay unsynced records (operator)."""
    sl = _shadow_logger()
    repo = _layered_repo()

    if sl is None:
        return ResponseContext.server_error("Shadow logger not available")
    if repo is None:
        return ResponseContext.bad_request("Layered storage not configured")

    body = ctx.json_body or {}
    service_name = body.get("service_name")
    mark_synced = body.get("mark_synced", True)

    if service_name:
        records = sl.get_records_by_service(service_name)
        records = [r for r in records if not r.synced_after_recovery]
    else:
        records = sl.get_unsynced_records()

    if not records:
        return ResponseContext.json(
            {
                "status": "success",
                "message": "No unsynced records to replay",
                "replayed": 0,
                "failed": 0,
                "timestamp": utc_now().isoformat(),
            }
        )

    result = repo.force_sync_to_l2()

    marked_count = 0
    if mark_synced and result.get("success", False):
        if service_name:
            marked_count = sl.mark_as_synced(service_name)
        else:
            marked_count = sl.mark_all_as_synced()

    actor = resolve_actor(ctx)
    logger.info(
        "l2_storage_api.shadow_log_replay",
        request_user=actor,
        synced_count=result.get("synced", 0),
        failed=result.get("failed", 0),
        marked_count=marked_count,
    )

    return ResponseContext.json(
        {
            "status": "success" if result.get("success", False) else "partial",
            "message": "Shadow log replay completed",
            "records_found": len(records),
            "sync_result": result,
            "marked_as_synced": marked_count,
            "timestamp": utc_now().isoformat(),
        }
    )


def shadow_log_by_service(ctx: RequestContext) -> ResponseContext:
    """GET /l2-storage/shadow-log/service/{service_name} — logs by service (viewer)."""
    service_name = ctx.get_path_param("service_name")
    sl = _shadow_logger()
    if sl is None:
        return ResponseContext.server_error("Shadow logger not available")

    records = sl.get_records_by_service(service_name)
    entries = [_serialize_record(r) for r in records]

    return ResponseContext.json(
        {
            "status": "success",
            "service_name": service_name,
            "count": len(entries),
            "entries": entries,
            "timestamp": utc_now().isoformat(),
        }
    )
