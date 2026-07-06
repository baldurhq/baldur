"""
Framework-agnostic Config History handlers.

Extracted from api/django/views/config_history.py (Phase 2b).

Endpoints:
    GET  /config/{config_type}/history                    Config change history
    GET  /config/{config_type}/history/{version}          Version detail
    POST /config/{config_type}/rollback                   Rollback to version
    GET  /config/{config_type}/compare                    Compare versions
"""

from __future__ import annotations

import structlog

from baldur.api.handlers._common import resolve_actor
from baldur.interfaces.web_framework import RequestContext, ResponseContext

logger = structlog.get_logger()

__all__ = [
    "config_history_list",
    "config_version_detail",
    "config_rollback",
    "config_compare",
]


def _service():
    from baldur.services.config_history import get_config_history_service

    return get_config_history_service()


def _validate_config_type(service, config_type: str) -> ResponseContext | None:
    if not service.is_valid_config_type(config_type):
        return ResponseContext.bad_request(f"Invalid config_type: {config_type}")
    return None


def config_history_list(ctx: RequestContext) -> ResponseContext:
    """GET /config/{config_type}/history — config change history (viewer)."""
    config_type = ctx.get_path_param("config_type")
    service = _service()

    error = _validate_config_type(service, config_type)
    if error:
        return error

    try:
        limit = int(ctx.get_query("limit", 10))
        limit = min(max(limit, 1), 50)
    except (TypeError, ValueError):
        limit = 10

    history = service.get_history(config_type, limit=limit)
    current = service.get_current_version(config_type)

    return ResponseContext.json(
        {
            "status": "success",
            "config_type": config_type,
            "current_version": current.version if current else None,
            "count": len(history),
            "versions": [
                {
                    "version": v.version,
                    "timestamp": v.timestamp,
                    "changed_by": v.changed_by,
                    "reason": v.reason,
                    "hash": v.hash,
                }
                for v in history
            ],
        }
    )


def config_version_detail(ctx: RequestContext) -> ResponseContext:
    """GET /config/{config_type}/history/{version} — version detail (viewer)."""
    config_type = ctx.get_path_param("config_type")
    service = _service()

    error = _validate_config_type(service, config_type)
    if error:
        return error

    try:
        version = int(ctx.get_path_param("version"))
    except (TypeError, ValueError):
        return ResponseContext.bad_request("version must be a valid integer")

    version_data = service.get_version(config_type, version)

    if not version_data:
        return ResponseContext.not_found(
            f"Version {version} not found for {config_type}"
        )

    return ResponseContext.json(
        {
            "status": "success",
            "version": version_data.to_dict(),
        }
    )


def config_rollback(ctx: RequestContext) -> ResponseContext:
    """POST /config/{config_type}/rollback — rollback to version (admin)."""
    config_type = ctx.get_path_param("config_type")
    service = _service()

    error = _validate_config_type(service, config_type)
    if error:
        return error

    body = ctx.json_body or {}
    target_version = body.get("version")

    if target_version is None:
        return ResponseContext.bad_request("version is required")

    try:
        target_version = int(target_version)
    except (ValueError, TypeError):
        return ResponseContext.bad_request("version must be a valid integer")

    target = service.get_version(config_type, target_version)
    if not target:
        return ResponseContext.not_found(
            f"Version {target_version} not found for {config_type}"
        )

    actor = resolve_actor(ctx)

    rolled_back = service.rollback(
        config_type=config_type,
        target_version=target_version,
        rolled_back_by=actor,
    )

    if not rolled_back:
        return ResponseContext.server_error(
            "Failed to rollback - see server logs for details"
        )

    _apply_config_values(config_type, target.values, changed_by=actor)

    logger.info(
        "config_rollback.rolled_back_new",
        config_type=config_type,
        target_version=target_version,
        rolled_back=rolled_back.version,
        username=actor,
    )

    return ResponseContext.json(
        {
            "status": "success",
            "message": f"Rolled back {config_type} to version {target_version}",
            "rolled_back_to": target_version,
            "new_version": rolled_back.version,
            "applied_by": actor,
            "applied_values": target.values,
        }
    )


def _apply_config_values(
    config_type: str, values: dict, changed_by: str = "system"
) -> None:
    """Apply a rolled-back config snapshot via the manager's generic full-dict apply.

    Routes the full real-field snapshot through ``apply_config_values`` →
    ``_update_config``'s ``valid_fields`` filter, which faithfully applies every
    real field for ALL domains (Pydantic-class + slo + previously-unmapped
    domains) in one path. This replaces the old typed-method dispatch map whose
    drifted signatures raised ``TypeError`` on the full real-field snapshot for
    every Pydantic-class domain.
    """
    from baldur.factory.registry import ProviderRegistry

    manager = ProviderRegistry.runtime_config_manager.safe_get()
    if manager is None:
        raise RuntimeError(
            "Config history apply requires baldur_pro RuntimeConfigManager"
        )

    manager.apply_config_values(
        config_type,
        values,
        changed_by=changed_by,
        reason=f"Config rollback: {config_type}",
    )
    logger.info(
        "config_rollback.applied_values",
        config_type=config_type,
        values=values,
    )


def config_compare(ctx: RequestContext) -> ResponseContext:
    """GET /config/{config_type}/compare — compare versions (viewer)."""
    config_type = ctx.get_path_param("config_type")
    service = _service()

    error = _validate_config_type(service, config_type)
    if error:
        return error

    version_a_str = ctx.get_query("version_a")
    version_b_str = ctx.get_query("version_b")

    if not version_a_str or not version_b_str:
        return ResponseContext.bad_request("Both version_a and version_b are required")

    try:
        version_a = int(version_a_str)
        version_b = int(version_b_str)
    except ValueError:
        return ResponseContext.bad_request(
            "version_a and version_b must be valid integers"
        )

    comparison = service.compare_versions(config_type, version_a, version_b)

    if not comparison:
        return ResponseContext.not_found("One or both versions not found")

    return ResponseContext.json(
        {
            "status": "success",
            "comparison": comparison,
        }
    )
