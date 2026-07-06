"""
Framework-agnostic Tiering handlers for Criticality-Based Load Shedding.

Extracted from api/django/views/tiering.py (Phase 2a). Replaces 4 inline
DRF serializers with ``_validate_*`` helper functions; all business logic
preserved.

Endpoints:
    GET  /config/tiers/              Get tier definitions
    PUT  /config/tiers/              Update tier definitions
    GET  /config/tier-mappings/      Get tier mappings
    PUT  /config/tier-mappings/      Update tier mappings
    GET  /config/tier-overrides/     Get tier overrides
    PUT  /config/tier-overrides/     Update tier overrides
    POST /config/tiers/dry-run/      Simulate tier changes
    POST /config/tiers/reset/        Reset to defaults
    GET  /config/tiers/export/       Export current configuration
    POST /config/tiers/import/       Import configuration
    GET  /config/tiers/resolve/      Resolve tier for a path
"""

from __future__ import annotations

from typing import Any

import structlog

from baldur.api.handlers._common import resolve_actor
from baldur.interfaces.web_framework import RequestContext, ResponseContext
from baldur.utils.time import utc_now

logger = structlog.get_logger()

__all__ = [
    "tier_definitions_get",
    "tier_definitions_update",
    "tier_mappings_get",
    "tier_mappings_update",
    "tier_overrides_get",
    "tier_overrides_update",
    "tier_dry_run",
    "tier_reset",
    "tier_export",
    "tier_import",
    "tier_resolve_lookup",
]


# =============================================================================
# Lazy imports
# =============================================================================


def _get_registry():
    from baldur.scaling.tiering.registry import get_tier_registry

    return get_tier_registry()


def _get_defaults():
    from baldur.scaling.tiering.defaults import (
        DEFAULT_TIER_DEFINITIONS,
        DEFAULT_TIER_MAPPINGS,
        DEFAULT_TIER_OVERRIDES,
    )

    return DEFAULT_TIER_DEFINITIONS, DEFAULT_TIER_MAPPINGS, DEFAULT_TIER_OVERRIDES


def _get_models():
    from baldur.scaling.tiering.models import (
        TierDefinition,
        TierMapping,
        TierOverride,
    )

    return TierDefinition, TierMapping, TierOverride


def _get_enums():
    from baldur.scaling.tiering.enums import (
        OverrideIdentifierType,
        PatternType,
    )

    return PatternType, OverrideIdentifierType


# =============================================================================
# Validation helpers (replace DRF serializers)
# =============================================================================


def _validate_tier_definition(item: dict) -> tuple[dict | None, str | None]:
    """Validate a single tier definition dict.

    Returns (validated_data, error_message).
    """
    tid = item.get("id")
    if not tid or not isinstance(tid, str) or len(tid) > 50:
        return None, "id is required and must be a string (max 50 chars)"

    name = item.get("name")
    if not name or not isinstance(name, str) or len(name) > 100:
        return None, "name is required and must be a string (max 100 chars)"

    multiplier = item.get("multiplier")
    if multiplier is None or not isinstance(multiplier, (int, float)):
        return None, "multiplier is required and must be a number"
    multiplier = float(multiplier)
    if multiplier < 0.0 or multiplier > 1.0:
        return None, "multiplier must be between 0.0 and 1.0"

    priority = item.get("priority", 0)
    if not isinstance(priority, int) or isinstance(priority, bool):
        return None, "priority must be an integer"

    description = item.get("description", "")
    if not isinstance(description, str):
        return None, "description must be a string"

    color = item.get("color", "#000000")
    if not isinstance(color, str) or len(color) > 7:
        return None, "color must be a string (max 7 chars)"

    return {
        "id": tid,
        "name": name,
        "multiplier": multiplier,
        "priority": priority,
        "description": description,
        "color": color,
    }, None


def _validate_tier_definitions(items: list) -> tuple[list[dict] | None, list | None]:
    """Validate a list of tier definition dicts.

    Returns (validated_list, errors).
    """
    if not isinstance(items, list):
        return None, [{"error": "Expected a list of tier definitions"}]

    validated: list[dict] = []
    errors: list = []
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            errors.append({f"item_{i}": "Must be a dict"})
            continue
        data, err = _validate_tier_definition(item)
        if err or data is None:
            errors.append({f"item_{i}": err})
        else:
            validated.append(data)

    if errors:
        return None, errors
    return validated, None


def _validate_tier_mapping(item: dict) -> tuple[dict | None, str | None]:
    """Validate a single tier mapping dict."""
    PatternType, _ = _get_enums()

    pattern = item.get("pattern")
    if not pattern or not isinstance(pattern, str) or len(pattern) > 500:
        return None, "pattern is required and must be a string (max 500 chars)"

    tier_id = item.get("tier_id")
    if not tier_id or not isinstance(tier_id, str) or len(tier_id) > 50:
        return None, "tier_id is required and must be a string (max 50 chars)"

    pattern_type_val = item.get("pattern_type", PatternType.EXACT.value)
    valid_pattern_types = [pt.value for pt in PatternType]
    if pattern_type_val not in valid_pattern_types:
        return None, f"pattern_type must be one of {valid_pattern_types}"

    priority = item.get("priority", 0)
    if not isinstance(priority, int) or isinstance(priority, bool):
        return None, "priority must be an integer"

    description = item.get("description", "")
    if not isinstance(description, str):
        return None, "description must be a string"

    return {
        "pattern": pattern,
        "tier_id": tier_id,
        "pattern_type": pattern_type_val,
        "priority": priority,
        "description": description,
    }, None


def _validate_tier_mappings(items: list) -> tuple[list[dict] | None, list | None]:
    """Validate a list of tier mapping dicts."""
    if not isinstance(items, list):
        return None, [{"error": "Expected a list of tier mappings"}]

    validated: list[dict] = []
    errors: list = []
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            errors.append({f"item_{i}": "Must be a dict"})
            continue
        data, err = _validate_tier_mapping(item)
        if err or data is None:
            errors.append({f"item_{i}": err})
        else:
            validated.append(data)

    if errors:
        return None, errors
    return validated, None


def _validate_tier_override(item: dict) -> tuple[dict | None, str | None]:
    """Validate a single tier override dict."""
    _, OverrideIdentifierType = _get_enums()

    identifier = item.get("identifier")
    if not identifier or not isinstance(identifier, str) or len(identifier) > 200:
        return None, "identifier is required and must be a string (max 200 chars)"

    identifier_type_val = item.get("identifier_type")
    valid_id_types = [it.value for it in OverrideIdentifierType]
    if identifier_type_val not in valid_id_types:
        return None, f"identifier_type is required and must be one of {valid_id_types}"

    tier_id = item.get("tier_id")
    if not tier_id or not isinstance(tier_id, str) or len(tier_id) > 50:
        return None, "tier_id is required and must be a string (max 50 chars)"

    reason = item.get("reason", "")
    if not isinstance(reason, str):
        return None, "reason must be a string"

    expires_at = item.get("expires_at")
    # expires_at is optional and can be None

    return {
        "identifier": identifier,
        "identifier_type": identifier_type_val,
        "tier_id": tier_id,
        "reason": reason,
        "expires_at": expires_at,
    }, None


def _validate_tier_overrides(items: list) -> tuple[list[dict] | None, list | None]:
    """Validate a list of tier overrides dicts."""
    if not isinstance(items, list):
        return None, [{"error": "Expected a list of tier overrides"}]

    validated: list[dict] = []
    errors: list = []
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            errors.append({f"item_{i}": "Must be a dict"})
            continue
        data, err = _validate_tier_override(item)
        if err or data is None:
            errors.append({f"item_{i}": err})
        else:
            validated.append(data)

    if errors:
        return None, errors
    return validated, None


def _validate_dry_run_request(body: dict) -> tuple[dict | None, list | None]:  # noqa: C901, PLR0912
    """Validate dry run request body."""
    errors = []
    result: dict[str, Any] = {}

    tiers_raw = body.get("tiers")
    if tiers_raw is not None:
        validated, tier_errors = _validate_tier_definitions(tiers_raw)
        if tier_errors:
            errors.extend(tier_errors)
        else:
            result["tiers"] = validated

    mappings_raw = body.get("mappings")
    if mappings_raw is not None:
        validated, mapping_errors = _validate_tier_mappings(mappings_raw)
        if mapping_errors:
            errors.extend(mapping_errors)
        else:
            result["mappings"] = validated

    test_paths = body.get("test_paths")
    if test_paths is not None:
        if not isinstance(test_paths, list):
            errors.append({"test_paths": "Must be a list of strings"})
        else:
            for i, p in enumerate(test_paths):
                if not isinstance(p, str) or len(p) > 500:
                    errors.append(
                        {f"test_paths[{i}]": "Must be a string (max 500 chars)"}
                    )
            if not errors:
                result["test_paths"] = test_paths

    if errors:
        return None, errors
    return result, None


# =============================================================================
# Audit helper
# =============================================================================


def _log_change(actor: str, config_type: str, config_key: str, changes: Any) -> None:
    """Log configuration change via audit subsystem."""
    try:
        from baldur.audit import log_config_change

        log_config_change(
            config_type=f"tiering_{config_type}",
            config_key=config_key,
            old_value=None,
            new_value=changes,
            user=actor,
        )
    except Exception as e:
        logger.warning(
            "tier_api.log_change_failed",
            error=e,
        )


# =============================================================================
# Handlers
# =============================================================================


def tier_definitions_get(ctx: RequestContext) -> ResponseContext:
    """GET /config/tiers/ — get all tier definitions."""
    DEFAULT_TIER_DEFINITIONS, _, _ = _get_defaults()

    registry = _get_registry()
    tiers = registry.get_all_tiers()

    return ResponseContext.json(
        {
            "status": "success",
            "tiers": [t.to_dict() for t in tiers],
            "defaults": [t.to_dict() for t in DEFAULT_TIER_DEFINITIONS],
            "timestamp": utc_now().isoformat(),
        }
    )


def tier_definitions_update(ctx: RequestContext) -> ResponseContext:
    """PUT /config/tiers/ — update tier definitions."""
    TierDefinition, _, _ = _get_models()

    body = ctx.json_body or {}
    validated, errors = _validate_tier_definitions(body.get("tiers", []))
    if errors or validated is None:
        return ResponseContext.json(
            {"status": "error", "errors": errors},
            status_code=400,
        )

    tiers = [TierDefinition(**item) for item in validated]

    registry = _get_registry()
    result = registry.set_tiers(tiers)

    if not result.is_valid:
        return ResponseContext.json(
            {
                "status": "error",
                "validation": result.to_dict(),
            },
            status_code=400,
        )

    actor = resolve_actor(ctx)
    _log_change(actor, "tiers", "tier_config", [t.to_dict() for t in tiers])

    return ResponseContext.json(
        {
            "status": "success",
            "tiers": [t.to_dict() for t in tiers],
            "validation": result.to_dict(),
            "changed_by": actor,
            "timestamp": utc_now().isoformat(),
        }
    )


def tier_mappings_get(ctx: RequestContext) -> ResponseContext:
    """GET /config/tier-mappings/ — get all tier mappings."""
    _, DEFAULT_TIER_MAPPINGS, _ = _get_defaults()

    registry = _get_registry()
    mappings = registry.get_all_mappings()

    return ResponseContext.json(
        {
            "status": "success",
            "mappings": [m.to_dict() for m in mappings],
            "defaults": [m.to_dict() for m in DEFAULT_TIER_MAPPINGS],
            "timestamp": utc_now().isoformat(),
        }
    )


def tier_mappings_update(ctx: RequestContext) -> ResponseContext:
    """PUT /config/tier-mappings/ — update tier mappings."""
    PatternType, _ = _get_enums()
    _, TierMapping, _ = _get_models()

    body = ctx.json_body or {}
    validated, errors = _validate_tier_mappings(body.get("mappings", []))
    if errors or validated is None:
        return ResponseContext.bad_request(
            "Validation failed", errors={"mappings": errors}
        )

    mappings = []
    for item in validated:
        item["pattern_type"] = PatternType(item["pattern_type"])
        mappings.append(TierMapping(**item))

    registry = _get_registry()
    result = registry.set_mappings(mappings)

    if not result.is_valid:
        return ResponseContext.json(
            {
                "status": "error",
                "validation": result.to_dict(),
            },
            status_code=400,
        )

    actor = resolve_actor(ctx)
    _log_change(actor, "mappings", "tier_mappings", [m.to_dict() for m in mappings])

    return ResponseContext.json(
        {
            "status": "success",
            "mappings": [m.to_dict() for m in mappings],
            "validation": result.to_dict(),
            "changed_by": actor,
            "timestamp": utc_now().isoformat(),
        }
    )


def tier_overrides_get(ctx: RequestContext) -> ResponseContext:
    """GET /config/tier-overrides/ — get all tier overrides."""
    _, _, DEFAULT_TIER_OVERRIDES = _get_defaults()

    registry = _get_registry()
    overrides = registry.get_all_overrides()

    return ResponseContext.json(
        {
            "status": "success",
            "overrides": [o.to_dict() for o in overrides],
            "defaults": [o.to_dict() for o in DEFAULT_TIER_OVERRIDES],
            "timestamp": utc_now().isoformat(),
        }
    )


def tier_overrides_update(ctx: RequestContext) -> ResponseContext:
    """PUT /config/tier-overrides/ — update tier overrides."""
    _, OverrideIdentifierType = _get_enums()
    _, _, TierOverride = _get_models()

    body = ctx.json_body or {}
    validated, errors = _validate_tier_overrides(body.get("overrides", []))
    if errors or validated is None:
        return ResponseContext.json(
            {"status": "error", "errors": errors},
            status_code=400,
        )

    overrides = []
    for item in validated:
        item["identifier_type"] = OverrideIdentifierType(item["identifier_type"])
        overrides.append(TierOverride(**item))

    registry = _get_registry()
    result = registry.set_overrides(overrides)

    if not result.is_valid:
        return ResponseContext.json(
            {
                "status": "error",
                "validation": result.to_dict(),
            },
            status_code=400,
        )

    actor = resolve_actor(ctx)
    _log_change(actor, "overrides", "tier_overrides", [o.to_dict() for o in overrides])

    return ResponseContext.json(
        {
            "status": "success",
            "overrides": [o.to_dict() for o in overrides],
            "validation": result.to_dict(),
            "changed_by": actor,
            "timestamp": utc_now().isoformat(),
        }
    )


def tier_dry_run(ctx: RequestContext) -> ResponseContext:
    """POST /config/tiers/dry-run/ — simulate tier changes."""
    PatternType, _ = _get_enums()
    TierDefinition, TierMapping, _ = _get_models()

    body = ctx.json_body or {}
    validated, errors = _validate_dry_run_request(body)
    if errors or validated is None:
        return ResponseContext.json(
            {"status": "error", "errors": errors},
            status_code=400,
        )

    registry = _get_registry()

    if validated.get("tiers"):
        tiers = [TierDefinition(**item) for item in validated["tiers"]]
    else:
        tiers = registry.get_all_tiers()

    if validated.get("mappings"):
        mappings = []
        for item in validated["mappings"]:
            item["pattern_type"] = PatternType(item["pattern_type"])
            mappings.append(TierMapping(**item))
    else:
        mappings = registry.get_all_mappings()

    result = registry.simulate(
        tiers=tiers,
        mappings=mappings,
        test_paths=validated.get("test_paths"),
    )

    return ResponseContext.json(
        {
            "status": "success",
            "simulation_result": result,
            "warning": "This is a dry run. No changes have been applied.",
            "timestamp": utc_now().isoformat(),
        }
    )


def tier_reset(ctx: RequestContext) -> ResponseContext:
    """POST /config/tiers/reset/ — reset to default configuration."""
    registry = _get_registry()
    registry.reset_to_defaults()

    actor = resolve_actor(ctx)

    try:
        from baldur.audit import log_config_change

        log_config_change(
            config_type="tiering_reset",
            config_key="tier_config",
            old_value=None,
            new_value={"action": "reset_to_defaults"},
            user=actor,
        )
    except Exception as e:
        logger.warning(
            "tier_api.log_reset_failed",
            error=e,
        )

    return ResponseContext.json(
        {
            "status": "success",
            "message": "Tier configuration reset to defaults",
            "tiers": [t.to_dict() for t in registry.get_all_tiers()],
            "mappings": [m.to_dict() for m in registry.get_all_mappings()],
            "overrides": [o.to_dict() for o in registry.get_all_overrides()],
            "changed_by": actor,
            "timestamp": utc_now().isoformat(),
        }
    )


def tier_export(ctx: RequestContext) -> ResponseContext:
    """GET /config/tiers/export/ — export current configuration."""
    registry = _get_registry()
    config = registry.export_config()

    return ResponseContext.json(
        {
            "status": "success",
            "config": config,
            "timestamp": utc_now().isoformat(),
        }
    )


def tier_import(ctx: RequestContext) -> ResponseContext:
    """POST /config/tiers/import/ — import configuration."""
    body = ctx.json_body or {}
    config = body.get("config", {})

    if not config:
        return ResponseContext.bad_request("config is required")

    registry = _get_registry()
    result = registry.import_config(config)

    if not result.is_valid:
        return ResponseContext.json(
            {
                "status": "error",
                "validation": result.to_dict(),
            },
            status_code=400,
        )

    actor = resolve_actor(ctx)

    try:
        from baldur.audit import log_config_change

        log_config_change(
            config_type="tiering_import",
            config_key="tier_config",
            old_value=None,
            new_value=config,
            user=actor,
        )
    except Exception as e:
        logger.warning(
            "tier_api.log_import_failed",
            error=e,
        )

    return ResponseContext.json(
        {
            "status": "success",
            "message": "Tier configuration imported successfully",
            "validation": result.to_dict(),
            "config": registry.export_config(),
            "changed_by": actor,
            "timestamp": utc_now().isoformat(),
        }
    )


def tier_resolve_lookup(ctx: RequestContext) -> ResponseContext:
    """GET /config/tiers/resolve/ — resolve tier for a path."""
    path = ctx.get_query("path")
    if not path:
        return ResponseContext.bad_request("path query parameter is required")

    client_ip = ctx.get_query("client_ip")
    user_id = ctx.get_query("user_id")

    registry = _get_registry()

    override_tier = registry.get_override_tier(
        client_ip=client_ip,
        user_id=user_id,
    )

    path_tier = registry.get_tier_for_path(path)

    resolved_tier = registry.resolve_tier(
        path=path,
        client_ip=client_ip,
        user_id=user_id,
    )

    return ResponseContext.json(
        {
            "status": "success",
            "path": path,
            "resolved_tier": resolved_tier.to_dict() if resolved_tier else None,
            "path_based_tier": path_tier.to_dict() if path_tier else None,
            "override_tier": override_tier.to_dict() if override_tier else None,
            "has_override": override_tier is not None,
            "timestamp": utc_now().isoformat(),
        }
    )
