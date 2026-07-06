"""
Framework-agnostic Emergency Mode handlers.

Extracted from api/django/views/emergency.py. Covers manual activation,
release, gradual recovery, history, config, and level introspection.

Endpoints:
    GET  /emergency/status/             Current state
    POST /emergency/trigger/            Manual activation (admin)
    POST /emergency/release/            Deactivate (admin)
    POST /emergency/gradual-recovery/   Start recovery (admin)
    POST /emergency/stop-recovery/      Stop recovery (admin)
    GET  /emergency/history/            Change history
    GET  /emergency/config/             Recovery gate config
    PUT  /emergency/config/             Update config (admin)
    GET  /emergency/levels/             Level definitions
"""

from __future__ import annotations

import structlog

from baldur.api.handlers._common import resolve_actor
from baldur.interfaces.web_framework import RequestContext, ResponseContext
from baldur.utils.time import utc_now

logger = structlog.get_logger()

__all__ = [
    "emergency_status",
    "emergency_trigger",
    "emergency_release",
    "gradual_recovery_start",
    "gradual_recovery_stop",
    "emergency_history",
    "emergency_config_get",
    "emergency_config_update",
    "emergency_levels",
]


def _manager():
    """Return the emergency manager instance (519 D-c2)."""
    from baldur.factory.registry import ProviderRegistry

    manager = ProviderRegistry.emergency_manager.safe_get()
    if manager is None:
        raise RuntimeError("Emergency handlers require baldur_pro EmergencyModeManager")
    return manager


def _levels():
    """Return (EMERGENCY_LEVEL_RULES, EmergencyLevel) lazily."""
    try:
        from baldur_pro.services.emergency_mode.enums import (
            EMERGENCY_LEVEL_RULES,
            EmergencyLevel,
        )
    except ImportError:
        EMERGENCY_LEVEL_RULES = None  # type: ignore[assignment,misc]
        EmergencyLevel = None  # type: ignore[assignment,misc]

    return EMERGENCY_LEVEL_RULES, EmergencyLevel


def _exceptions():
    """Return (EmergencyStateError, RecoveryNotAllowedError) lazily."""
    try:
        from baldur_pro.services.emergency_mode.exceptions import (
            EmergencyStateError,
            RecoveryNotAllowedError,
        )
    except ImportError:
        EmergencyStateError = None  # type: ignore[assignment,misc]
        RecoveryNotAllowedError = None  # type: ignore[assignment,misc]

    return EmergencyStateError, RecoveryNotAllowedError


def _recovery_gate_config_cls():
    """Return the RecoveryGateConfig class lazily."""
    from baldur.models.recovery import RecoveryGateConfig

    return RecoveryGateConfig


def emergency_status(ctx: RequestContext) -> ResponseContext:
    """GET /emergency/status/ — current state (viewer+)."""
    EMERGENCY_LEVEL_RULES, EmergencyLevel = _levels()

    manager = _manager()
    state = manager.get_state()

    tier_multipliers = EMERGENCY_LEVEL_RULES.get(
        state.level, EMERGENCY_LEVEL_RULES[EmergencyLevel.NORMAL]
    )

    return ResponseContext.json(
        {
            "is_active": state.is_active,
            "level": state.level.value,
            "activated_at": state.activated_at,
            "activated_by": state.activated_by,
            "activation_reason": state.activation_reason,
            "expires_at": state.expires_at,
            "is_auto_triggered": state.is_auto_triggered,
            "is_recovering": state.is_recovering,
            "recovery_started_at": state.recovery_started_at,
            "target_level": state.target_level.value if state.target_level else None,
            "deactivated_at": state.deactivated_at,
            "deactivated_by": state.deactivated_by,
            "tier_multipliers": tier_multipliers,
            "available_levels": [
                {"name": level.value, "multipliers": EMERGENCY_LEVEL_RULES[level]}
                for level in EmergencyLevel
            ],
            "timestamp": utc_now().isoformat(),
        }
    )


def emergency_trigger(ctx: RequestContext) -> ResponseContext:
    """POST /emergency/trigger/ — manual activation (admin)."""
    EMERGENCY_LEVEL_RULES, EmergencyLevel = _levels()

    body = ctx.json_body or {}
    level_name = body.get("level", "LEVEL_1")
    reason = body.get("reason", "")
    duration_minutes = body.get("duration_minutes")
    override_kill_switch = body.get("override_kill_switch", False)

    if not reason:
        return ResponseContext.json(
            {
                "success": False,
                "error": "reason is required",
                "message": "Please provide a reason for emergency activation",
            },
            status_code=400,
        )

    try:
        level = EmergencyLevel[level_name]
    except KeyError:
        return ResponseContext.json(
            {
                "success": False,
                "error": "invalid_level",
                "message": (
                    f"Invalid level: {level_name}. Available: LEVEL_1, LEVEL_2, LEVEL_3"
                ),
            },
            status_code=400,
        )

    if level == EmergencyLevel.NORMAL:
        return ResponseContext.json(
            {
                "success": False,
                "error": "invalid_level",
                "message": (
                    "NORMAL is not an emergency level. Use /release/ to deactivate."
                ),
            },
            status_code=400,
        )

    actor = resolve_actor(ctx)
    manager = _manager()
    state = manager.activate_manual(
        level=level,
        reason=reason,
        activated_by=actor,
        duration_minutes=int(duration_minutes) if duration_minutes else None,
        override_kill_switch=bool(override_kill_switch),
    )

    logger.warning(
        "emergency_api.emergency_mode_activated",
        level=level.value,
        actor_id=actor,
        reason=reason,
    )

    return ResponseContext.json(
        {
            "success": True,
            "status": "activated",
            "level": state.level.value,
            "activated_by": actor,
            "expires_at": state.expires_at,
            "tier_multipliers": EMERGENCY_LEVEL_RULES[state.level],
            "timestamp": utc_now().isoformat(),
        }
    )


def emergency_release(ctx: RequestContext) -> ResponseContext:
    """POST /emergency/release/ — deactivate (admin)."""
    _, RecoveryNotAllowedError = _exceptions()

    body = ctx.json_body or {}
    reason = body.get("reason", "")
    force = body.get("force", False)

    manager = _manager()
    current_state = manager.get_state()
    if not current_state.is_active:
        return ResponseContext.json(
            {
                "success": False,
                "error": "not_active",
                "message": "Emergency mode is not active.",
            },
            status_code=400,
        )

    actor = resolve_actor(ctx)
    previous_level = current_state.level.value

    try:
        manager.deactivate(
            deactivated_by=actor,
            reason=reason,
            force=force,
        )
    except RecoveryNotAllowedError as e:
        return ResponseContext.json(
            {
                "success": False,
                "error": "recovery_blocked",
                "message": str(e),
                "hint": "Use force=true to override the recovery gate.",
            },
            status_code=409,
        )

    logger.info(
        "emergency_api.emergency_mode_deactivated",
        previous_level=previous_level,
        actor_id=actor,
    )

    return ResponseContext.json(
        {
            "success": True,
            "status": "deactivated",
            "previous_level": previous_level,
            "deactivated_by": actor,
            "forced": force,
            "timestamp": utc_now().isoformat(),
        }
    )


def gradual_recovery_start(ctx: RequestContext) -> ResponseContext:
    """POST /emergency/gradual-recovery/ — start recovery (admin)."""
    _, EmergencyLevel = _levels()
    EmergencyStateError, _ = _exceptions()

    body = ctx.json_body or {}
    target_level_name = body.get("target_level", "NORMAL")

    try:
        target_level = EmergencyLevel[target_level_name]
    except KeyError:
        return ResponseContext.json(
            {
                "success": False,
                "error": "invalid_level",
                "message": f"Invalid level: {target_level_name}",
            },
            status_code=400,
        )

    actor = resolve_actor(ctx)
    manager = _manager()

    try:
        state = manager.start_gradual_recovery(
            initiated_by=actor,
            target_level=target_level,
        )
    except EmergencyStateError as e:
        return ResponseContext.json(
            {
                "success": False,
                "error": "invalid_request",
                "message": str(e),
            },
            status_code=400,
        )

    return ResponseContext.json(
        {
            "success": True,
            "status": "recovery_started",
            "current_level": state.level.value,
            "target_level": target_level.value,
            "initiated_by": actor,
            "timestamp": utc_now().isoformat(),
        }
    )


def gradual_recovery_stop(ctx: RequestContext) -> ResponseContext:
    """POST /emergency/stop-recovery/ — stop recovery (admin)."""
    body = ctx.json_body or {}
    reason = body.get("reason", "")
    actor = resolve_actor(ctx)

    manager = _manager()
    state = manager.stop_gradual_recovery(stopped_by=actor, reason=reason)

    return ResponseContext.json(
        {
            "success": True,
            "status": "recovery_stopped",
            "current_level": state.level.value,
            "stopped_by": actor,
            "timestamp": utc_now().isoformat(),
        }
    )


def emergency_history(ctx: RequestContext) -> ResponseContext:
    """GET /emergency/history/ — change history (viewer+)."""
    try:
        limit = int(ctx.get_query("limit", 50))
    except (TypeError, ValueError):
        limit = 50

    manager = _manager()
    history = manager.get_history(limit=limit)

    return ResponseContext.json(
        {
            "history": history,
            "count": len(history),
            "timestamp": utc_now().isoformat(),
        }
    )


def emergency_config_get(ctx: RequestContext) -> ResponseContext:
    """GET /emergency/config/ — recovery gate config."""
    manager = _manager()
    config = manager.get_recovery_gate_config()

    return ResponseContext.json(
        {"config": config.to_dict(), "timestamp": utc_now().isoformat()}
    )


def emergency_config_update(ctx: RequestContext) -> ResponseContext:
    """PUT /emergency/config/ — update config (admin)."""
    RecoveryGateConfig = _recovery_gate_config_cls()

    actor = resolve_actor(ctx)
    body = ctx.json_body or {}

    try:
        config = RecoveryGateConfig.from_dict(body)
    except (TypeError, ValueError, KeyError) as e:
        return ResponseContext.json(
            {
                "success": False,
                "error": "invalid_config",
                "message": str(e),
            },
            status_code=400,
        )

    manager = _manager()
    manager.set_recovery_gate_config(config, changed_by=actor)

    return ResponseContext.json(
        {
            "success": True,
            "config": config.to_dict(),
            "changed_by": actor,
            "timestamp": utc_now().isoformat(),
        }
    )


def emergency_levels(ctx: RequestContext) -> ResponseContext:
    """GET /emergency/levels/ — level definitions (viewer+)."""
    EMERGENCY_LEVEL_RULES, EmergencyLevel = _levels()

    descriptions = {
        EmergencyLevel.NORMAL: "Normal operation (all traffic allowed)",
        EmergencyLevel.LEVEL_1: "Minor incident — non-essential APIs blocked",
        EmergencyLevel.LEVEL_2: "Moderate incident — standard APIs at 10% capacity",
        EmergencyLevel.LEVEL_3: "Severe incident — critical APIs at 50% capacity",
    }

    levels = [
        {
            "name": level.value,
            "description": descriptions.get(level, ""),
            "multipliers": EMERGENCY_LEVEL_RULES[level],
        }
        for level in EmergencyLevel
    ]

    return ResponseContext.json({"levels": levels, "timestamp": utc_now().isoformat()})
