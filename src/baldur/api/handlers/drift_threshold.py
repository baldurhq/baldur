"""
Framework-agnostic Drift Threshold handlers.

Extracted from api/django/views/drift_threshold.py (Phase 2b).

Endpoints:
    GET  /config/drift-thresholds        Get drift threshold config
    PUT  /config/drift-thresholds        Update drift threshold config
    POST /config/drift-thresholds/reset  Reset to defaults
"""

from __future__ import annotations

from typing import Any

import structlog

from baldur.api.handlers._common import resolve_actor
from baldur.interfaces.web_framework import RequestContext, ResponseContext
from baldur.utils.time import utc_now

logger = structlog.get_logger()

__all__ = [
    "drift_threshold_config_get",
    "drift_threshold_config_update",
    "drift_threshold_reset",
]


def _manager():
    from baldur.factory.registry import ProviderRegistry

    manager = ProviderRegistry.runtime_config_manager.safe_get()
    if manager is None:
        raise RuntimeError(
            "Drift threshold configuration requires baldur_pro RuntimeConfigManager"
        )
    return manager


def _threshold_percent_display(config: dict[str, Any]) -> dict[str, str]:
    return {
        "warning": f"{config.get('warning_threshold', 0.05) * 100:.1f}%",
        "critical": f"{config.get('critical_threshold', 0.20) * 100:.1f}%",
        "incident": f"{config.get('incident_threshold', 0.50) * 100:.1f}%",
    }


def drift_threshold_config_get(ctx: RequestContext) -> ResponseContext:
    """GET /config/drift-thresholds — current drift threshold config (viewer)."""
    manager = _manager()
    config = manager.get_drift_threshold_config()

    return ResponseContext.json(
        {
            "status": "success",
            "config": config,
            "thresholds_percent": _threshold_percent_display(config),
            "timestamp": utc_now().isoformat(),
        }
    )


def drift_threshold_config_update(ctx: RequestContext) -> ResponseContext:
    """PUT /config/drift-thresholds — update drift threshold config (admin)."""
    actor = resolve_actor(ctx)
    body = ctx.json_body or {}
    update_fields: dict[str, Any] = {}

    for field in ["warning_threshold", "critical_threshold", "incident_threshold"]:
        if field in body:
            value = body[field]
            if not isinstance(value, (int, float)):
                return ResponseContext.bad_request(f"{field} must be a number")
            if not 0 < value <= 1.0:
                return ResponseContext.bad_request(f"{field} must be between 0 and 1.0")
            update_fields[field] = float(value)

    for field in ["alert_enabled", "incident_auto_create"]:
        if field in body:
            update_fields[field] = bool(body[field])

    if not update_fields:
        return ResponseContext.bad_request("No valid fields provided for update")

    manager = _manager()
    new_config = manager.update_drift_threshold_config(
        changed_by=actor,
        reason=f"API update: {list(update_fields.keys())}",
        **update_fields,
    )

    logger.info(
        "drift_threshold_api.config_updated",
        actor_id=actor,
        value=list(update_fields.keys()),
    )

    return ResponseContext.json(
        {
            "status": "updated",
            "config": new_config,
            "thresholds_percent": _threshold_percent_display(new_config),
            "updated_by": actor,
            "timestamp": utc_now().isoformat(),
        }
    )


def drift_threshold_reset(ctx: RequestContext) -> ResponseContext:
    """POST /config/drift-thresholds/reset — reset to defaults (admin)."""
    actor = resolve_actor(ctx)
    manager = _manager()
    default_config = manager.reset_drift_threshold_config(changed_by=actor)

    logger.info("drift_threshold_api.config_reset", actor_id=actor)

    return ResponseContext.json(
        {
            "status": "reset",
            "config": default_config,
            "thresholds_percent": _threshold_percent_display(default_config),
            "reset_by": actor,
            "timestamp": utc_now().isoformat(),
        }
    )
