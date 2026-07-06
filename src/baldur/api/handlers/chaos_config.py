"""
Framework-agnostic Chaos Configuration handlers.

Extracted from api/django/views/chaos/config_views.py (Phase 2b).

Endpoints:
    GET   /chaos/config/safety-guard     Safety guard config
    PATCH /chaos/config/safety-guard     Update safety guard config
    GET   /chaos/config/blast-radius     Blast radius policy
    PATCH /chaos/config/blast-radius     Update blast radius policy
    GET   /chaos/config/scheduler        Scheduler config
    PATCH /chaos/config/scheduler        Update scheduler config
    GET   /chaos/config/report           Report config
    PATCH /chaos/config/report           Update report config
"""

from __future__ import annotations

import structlog

from baldur.interfaces.web_framework import RequestContext, ResponseContext

logger = structlog.get_logger()

__all__ = [
    "safety_guard_config_get",
    "safety_guard_config_update",
    "chaos_blast_radius_policy_get",
    "chaos_blast_radius_policy_update",
    "scheduler_config_get",
    "scheduler_config_update",
    "report_config_get",
    "report_config_update",
]


def _resolve(slot_name: str, label: str):
    from baldur.factory.registry import ProviderRegistry

    instance = getattr(ProviderRegistry, slot_name).safe_get()
    if instance is None:
        raise RuntimeError(f"Chaos config handlers require baldur_pro {label}")
    return instance


def _safety_guard():
    return _resolve("safety_guard", "SafetyGuard")


def _blast_radius_manager():
    return _resolve("blast_radius_manager", "BlastRadiusManager")


def _scheduler():
    return _resolve("chaos_scheduler", "ChaosScheduler")


def _report_generator():
    return _resolve("report_generator", "ReportGenerator")


def safety_guard_config_get(ctx: RequestContext) -> ResponseContext:
    """GET /chaos/config/safety-guard — safety guard config (viewer)."""
    guard = _safety_guard()
    config = guard.get_config()
    return ResponseContext.json({"status": "success", "data": config.to_dict()})


def safety_guard_config_update(ctx: RequestContext) -> ResponseContext:
    """PATCH /chaos/config/safety-guard — update safety guard config (admin)."""
    body = ctx.json_body or {}
    if not body:
        return ResponseContext.bad_request("Request body is required")

    guard = _safety_guard()
    updated = guard.update_config(**body)
    logger.info("chaos_api.safetyguard_config_updated")
    return ResponseContext.json({"status": "success", "data": updated.to_dict()})


def chaos_blast_radius_policy_get(ctx: RequestContext) -> ResponseContext:
    """GET /chaos/config/blast-radius — blast radius policy (viewer)."""
    manager = _blast_radius_manager()
    policy = manager.get_policy()
    return ResponseContext.json({"status": "success", "data": policy.to_dict()})


def chaos_blast_radius_policy_update(ctx: RequestContext) -> ResponseContext:
    """PATCH /chaos/config/blast-radius — update blast radius policy (admin)."""
    body = ctx.json_body or {}
    if not body:
        return ResponseContext.bad_request("Request body is required")

    manager = _blast_radius_manager()
    updated = manager.update_policy(**body)
    logger.info("chaos_api.blastradius_policy_updated")
    return ResponseContext.json({"status": "success", "data": updated.to_dict()})


def scheduler_config_get(ctx: RequestContext) -> ResponseContext:
    """GET /chaos/config/scheduler — scheduler config (viewer)."""
    scheduler = _scheduler()
    config = scheduler.get_config()
    return ResponseContext.json({"status": "success", "data": config.to_dict()})


def scheduler_config_update(ctx: RequestContext) -> ResponseContext:
    """PATCH /chaos/config/scheduler — update scheduler config (admin)."""
    body = ctx.json_body or {}
    if not body:
        return ResponseContext.bad_request("Request body is required")

    scheduler = _scheduler()
    updated = scheduler.update_config(**body)
    logger.info("chaos_api.scheduler_config_updated")
    return ResponseContext.json({"status": "success", "data": updated.to_dict()})


def report_config_get(ctx: RequestContext) -> ResponseContext:
    """GET /chaos/config/report — report config (viewer)."""
    generator = _report_generator()
    config = generator.get_config()
    return ResponseContext.json({"status": "success", "data": config.to_dict()})


def report_config_update(ctx: RequestContext) -> ResponseContext:
    """PATCH /chaos/config/report — update report config (admin)."""
    body = ctx.json_body or {}
    if not body:
        return ResponseContext.bad_request("Request body is required")

    generator = _report_generator()
    updated = generator.update_config(**body)
    logger.info("chaos_api.report_config_updated")
    return ResponseContext.json({"status": "success", "data": updated.to_dict()})
