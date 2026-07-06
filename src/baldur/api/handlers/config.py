"""
Framework-agnostic Runtime Configuration handlers.

Extracted from api/django/views/config.py. The serializer-based validation
previously done at the DRF layer is replaced by the RuntimeConfigManager's
own field filtering + safe_defaults validator, so handlers can be called
from any framework (Django, FastAPI, Flask, admin server, CLI).

Endpoints:
    GET  /config/                       All configs
    POST /config/reset/                 Reset all to defaults (admin)
    GET  /config/pending/               Pending changes
    POST /config/pending/{id}/cancel/   Cancel pending change (admin)
    GET  /config/{config_name}/         Single config section
    PUT  /config/{config_name}/         Update config section (admin)

Special-case handlers exist for SLO (custom multi-field update + delete by
name) and Logging (runtime log-level hot reload after update).
"""

from __future__ import annotations

from typing import Any

import structlog

from baldur.api.handlers._common import resolve_actor
from baldur.core.exceptions import ConfigVersionConflictError
from baldur.interfaces.web_framework import RequestContext, ResponseContext
from baldur.utils.time import utc_now

logger = structlog.get_logger()

__all__ = [
    "all_config_get",
    "editable_config_get",
    "config_reset",
    "pending_changes_get",
    "cancel_pending_change",
    "config_get",
    "config_update",
    "logging_config_update",
    "slo_config_update",
    "slo_config_delete",
]


_APPLY_OPTION_FIELDS = {
    "apply_strategy",
    "delay_seconds",
    "grace_timeout_seconds",
    "reason",
    "expected_version",
}


def _get_manager():
    from baldur.factory.registry import ProviderRegistry

    manager = ProviderRegistry.runtime_config_manager.safe_get()
    if manager is None:
        raise RuntimeError("Config handlers require baldur_pro RuntimeConfigManager")
    return manager


def _client_ip(ctx: RequestContext) -> str:
    if ctx.client_ip:
        return ctx.client_ip
    xff = ctx.get_header("X-Forwarded-For")
    if xff:
        return xff.split(",")[0].strip()
    return "unknown"


def _split_apply_options(body: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Split request body into apply options + config changes."""
    apply_options: dict[str, Any] = {
        "strategy": body.get("apply_strategy"),
        "delay_seconds": body.get("delay_seconds"),
        "grace_timeout_seconds": body.get("grace_timeout_seconds"),
        "reason": body.get("reason", ""),
        "expected_version": body.get("expected_version"),
    }
    changes = {
        k: v for k, v in body.items() if k not in _APPLY_OPTION_FIELDS and v is not None
    }
    return apply_options, changes


def _status_to_http(result_status: str) -> int:
    if result_status == "applied":
        return 200
    if result_status in ("scheduled", "waiting"):
        return 202
    return 400


def all_config_get(ctx: RequestContext) -> ResponseContext:
    """GET /config/ — all configuration with default strategies."""
    manager = _get_manager()
    config = manager.get_all_config()

    config_with_strategies = {
        config_type: {
            "values": values,
            "default_strategy": manager.get_default_strategy(config_type),
        }
        for config_type, values in config.items()
    }

    return ResponseContext.json(
        {
            "status": "success",
            "config": config_with_strategies,
            "pending_changes": manager.get_pending_changes(),
            "timestamp": utc_now().isoformat(),
        }
    )


def _rollout_locks(domains) -> dict[str, str]:
    """Map each projected config domain to its owning canary rollout id, if locked.

    Backs the console editor's per-domain "under active rollout" affordance (685
    D6): a locked domain shows a badge + a disabled Apply so the operator learns
    the block before submitting (not on the 409). Reads the canary rollout store
    via the ProviderRegistry — OSS-reachable (the store interface + memory
    adapter are OSS; registered in bootstrap), no PRO import. An unregistered
    store degrades to ``{}``; a per-domain read failure omits that domain rather
    than failing the whole panel. A page-load snapshot: a lock acquired after
    load leaves a stale-enabled Apply, which the D5 409 banner then converges.
    """
    try:
        from baldur.factory.registry import ProviderRegistry

        store = ProviderRegistry.canary_rollout_store.safe_get()
    except Exception:  # noqa: BLE001
        return {}
    if store is None:
        return {}
    locks: dict[str, str] = {}
    for domain in domains:
        try:
            owner = store.get_config_lock_owner(domain)
        except Exception:  # noqa: BLE001
            continue
        if owner:
            locks[domain] = owner
    return locks


def _audit_enabled() -> bool:
    """Whether the Audit Trail master switch (BALDUR_AUDIT_ENABLED) is on.

    Drives the console editor's audit-status badge: when OFF, a change "reason"
    lands only in stdout app logs (not durably retained), so the panel warns
    instead of implying the rationale is preserved. Best-effort — any failure
    degrades to ``False`` (the safe, warning-shown state).
    """
    try:
        from baldur.settings.audit import get_audit_settings

        return bool(get_audit_settings().enabled)
    except Exception:  # noqa: BLE001
        return False


def editable_config_get(ctx: RequestContext) -> ResponseContext:
    """GET /config/editable — operator-editable config projection (viewer).

    Backs the console Runtime Config editor panel: the tier-filtered, allowlisted
    editable-field projection (value + widget + advisory clamp range) plus the
    per-domain default apply strategy, the audit-status badge state, and current
    pending changes.
    """
    manager = _get_manager()
    editable = manager.get_editable_config()

    config = {
        domain: {
            "fields": fields,
            "default_strategy": manager.get_default_strategy(domain),
            # Per-section OCC version: the console echoes it on PUT so a stale
            # edit is rejected (409). A single response-level ETag cannot
            # represent N per-section versions, so it is a per-domain body field.
            "version": manager.get_section_version(domain),
        }
        for domain, fields in editable.items()
    }

    return ResponseContext.json(
        {
            "status": "success",
            "config": config,
            # 685 D6: per-domain canary lock owners so the console flags a domain
            # under an active rollout before the operator edits (badge + disabled
            # Apply), converging with the D5 409 for a lock taken after page load.
            "rollout_locks": _rollout_locks(config.keys()),
            "audit_enabled": _audit_enabled(),
            "pending_changes": manager.get_pending_changes(),
            "timestamp": utc_now().isoformat(),
        }
    )


def config_reset(ctx: RequestContext) -> ResponseContext:
    """POST /config/reset/ — reset all to defaults (admin)."""
    manager = _get_manager()
    config = manager.reset_to_defaults()

    logger.info("config_api.all_config_reset_defaults", request_user=resolve_actor(ctx))

    return ResponseContext.json(
        {
            "status": "success",
            "message": "All configuration reset to defaults",
            "config": config,
            "timestamp": utc_now().isoformat(),
        }
    )


def pending_changes_get(ctx: RequestContext) -> ResponseContext:
    """GET /config/pending/ — pending configuration changes."""
    manager = _get_manager()
    config_type = ctx.get_query("config_type")
    pending = manager.get_pending_changes(config_type)

    return ResponseContext.json(
        {
            "status": "success",
            "pending_changes": pending,
            "count": len(pending),
            "timestamp": utc_now().isoformat(),
        }
    )


def cancel_pending_change(ctx: RequestContext) -> ResponseContext:
    """POST /config/pending/{pending_id}/cancel/ — cancel a pending change (admin)."""
    pending_id = ctx.get_path_param("pending_id", "")
    if not pending_id:
        return ResponseContext.json(
            {"error": "pending_id path parameter is required"}, status_code=400
        )

    manager = _get_manager()
    result = manager.cancel_pending_change(pending_id, cancelled_by=resolve_actor(ctx))

    if result.get("status") == "cancelled":
        return ResponseContext.json(
            {
                **result,
                "message": f"Pending change {pending_id} cancelled",
                "timestamp": utc_now().isoformat(),
            }
        )
    return ResponseContext.json(
        {"error": f"Pending change {pending_id} not found"}, status_code=404
    )


def config_get(ctx: RequestContext, *, config_name: str) -> ResponseContext:
    """GET /config/{config_name}/ — single config section."""
    manager = _get_manager()
    config = manager.get_config(config_name)
    default_strategy = manager.get_default_strategy(config_name)
    pending = manager.get_pending_changes(config_name)

    return ResponseContext.json(
        {
            "status": "success",
            "config": config,
            "config_type": config_name,
            "default_strategy": default_strategy,
            "pending_changes": pending,
            # Per-section OCC version for the read→echo→check round-trip.
            "version": manager.get_section_version(config_name),
            "timestamp": utc_now().isoformat(),
        }
    )


def config_update(ctx: RequestContext, *, config_name: str) -> ResponseContext:
    """PUT /config/{config_name}/ — update config with apply strategy (admin)."""
    body = ctx.json_body or {}
    apply_options, changes = _split_apply_options(body)

    if not changes:
        logger.warning(
            "config_audit.validation_failed",
            config_name=config_name,
            serializer="no_valid_fields",
            request_user=resolve_actor(ctx),
            client_ip=_client_ip(ctx),
        )
        return ResponseContext.json(
            {
                "status": "error",
                "error": "No valid configuration values provided",
                "config_type": config_name,
            },
            status_code=400,
        )

    manager = _get_manager()
    reason = apply_options.pop("reason", "") or f"API update: {list(changes.keys())}"

    try:
        result = manager.update_with_strategy(
            config_type=config_name,
            changes=changes,
            changed_by=resolve_actor(ctx),
            reason=reason,
            **apply_options,
        )
    except ConfigVersionConflictError as conflict:
        # Optimistic-concurrency conflict → 409 with a usable retry token plus
        # the fresh current_config (fetched here, not carried on the scalar
        # exception) so the console can render a merge/diff without a re-GET.
        logger.warning(
            "config_api.config_version_conflict",
            config_name=config_name,
            request_user=resolve_actor(ctx),
            expected_version=conflict.expected_version,
            actual_version=conflict.actual_version,
        )
        try:
            current_config = manager.get_config(config_name)
        except Exception:  # noqa: BLE001
            current_config = None
        return ResponseContext.json(
            {
                "status": "conflict",
                "error": (
                    "Config version conflict: your view is stale — "
                    "reload the current values and retry"
                ),
                "config_type": config_name,
                "expected_version": conflict.expected_version,
                "actual_version": conflict.actual_version,
                "current_config": current_config,
                "timestamp": utc_now().isoformat(),
            },
            status_code=409,
        )

    logger.info(
        "config_api.config_updated",
        config_name=config_name,
        request_user=resolve_actor(ctx),
        config_changes=list(changes.keys()),
        applied_strategy=result.get("applied_strategy"),
    )

    result["timestamp"] = utc_now().isoformat()
    result["config_type"] = config_name
    return ResponseContext.json(
        result, status_code=_status_to_http(result.get("status", ""))
    )


def logging_config_update(ctx: RequestContext) -> ResponseContext:
    """PUT /config/logging/ — logging update + runtime logger hot reload (283)."""
    response = config_update(ctx, config_name="logging")

    if response.status_code in (200, 202):
        try:
            from baldur.observability.structlog_config import (
                _COMPONENT_LOGGER_MAP,
                _apply_component_log_levels,
            )
            from baldur.settings.logging_settings import (
                get_logging_settings,
                reset_logging_settings,
            )

            reset_logging_settings()
            settings = get_logging_settings()
            _apply_component_log_levels(settings)

            logger.info(
                "config_api.logging_levels_applied_runtime",
                applied_levels={
                    k: getattr(settings, k, "INFO") for k in _COMPONENT_LOGGER_MAP
                },
                changed_by=resolve_actor(ctx),
            )
        except Exception as exc:
            logger.warning(
                "config_api.logging_runtime_apply_failed",
                error=str(exc),
            )

    return response


def slo_config_update(ctx: RequestContext) -> ResponseContext:
    """PUT /config/slo/ — add/update SLO definitions (admin)."""
    body = ctx.json_body or {}
    manager = _get_manager()

    result = manager.update_slo_config(
        default_window_days=body.get("default_window_days"),
        default_target=body.get("default_target"),
        default_fast_burn_rate=body.get("default_fast_burn_rate"),
        default_slow_burn_rate=body.get("default_slow_burn_rate"),
        slo=body.get("slo"),
        slos=body.get("slos"),
    )

    logger.info("config_api.slo_config_updated", request_user=resolve_actor(ctx))

    return ResponseContext.json(
        {
            "status": "success",
            "config": result,
            "config_type": "slo",
            "timestamp": utc_now().isoformat(),
        }
    )


def slo_config_delete(ctx: RequestContext) -> ResponseContext:
    """DELETE /config/slo/?name=<slo> — delete a specific SLO (admin)."""
    slo_name = ctx.get_query("name")
    if not slo_name:
        return ResponseContext.json(
            {"error": "Query parameter 'name' is required"}, status_code=400
        )

    manager = _get_manager()
    result = manager.delete_slo(slo_name)

    if result.get("status") == "deleted":
        logger.info(
            "config_api.slo_deleted", slo_name=slo_name, request_user=resolve_actor(ctx)
        )
        return ResponseContext.json({**result, "timestamp": utc_now().isoformat()})
    return ResponseContext.json(
        {"error": f"SLO '{slo_name}' not found"}, status_code=404
    )
