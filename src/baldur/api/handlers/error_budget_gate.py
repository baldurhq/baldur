"""
Framework-agnostic Error Budget Gate handlers.

Extracted from api/django/views/health.py — health / config / reset for
the baldur_pro Error Budget Gate.

Endpoints:
    GET  /health/gate/        Gate health status
    GET  /config/gate/        Gate configuration
    PUT  /config/gate/        Update configuration (authenticated)
    POST /gate/reset/         Reset components (authenticated)
"""

from __future__ import annotations

import structlog

from baldur.interfaces.web_framework import RequestContext, ResponseContext

logger = structlog.get_logger()

__all__ = [
    "gate_health",
    "gate_config_get",
    "gate_config_update",
    "gate_reset",
]


_ALLOWED_CONFIG_FIELDS = {
    "enabled",
    "critical_threshold_percent",
    "warning_threshold_percent",
    "fail_open",
    "cache_ttl_seconds",
    "fail_open_rate_limit_enabled",
    "fail_open_rate_limit_per_minute",
    "fail_open_rate_limit_window_seconds",
    "circuit_breaker_enabled",
    "circuit_breaker_failure_threshold",
    "circuit_breaker_recovery_timeout",
    "alert_on_fail_open",
    "alert_cooldown_seconds",
}


def _get_gate():
    from baldur.factory.registry import ProviderRegistry

    gate = ProviderRegistry.error_budget_gate.safe_get()
    if gate is None:
        raise RuntimeError(
            "Error budget gate handlers require baldur_pro ErrorBudgetGate"
        )
    return gate


def gate_health(ctx: RequestContext) -> ResponseContext:
    """GET /health/gate/ — Error Budget Gate health status."""
    gate = _get_gate()
    health = gate.get_health_status()

    if health.get("healthy"):
        return ResponseContext.json(health)
    return ResponseContext.json(health, status_code=503)


def gate_config_get(ctx: RequestContext) -> ResponseContext:
    """GET /config/gate/ — current gate configuration."""
    gate = _get_gate()
    config = gate.get_config()
    return ResponseContext.json({"status": "success", "config": config.to_dict()})


def gate_config_update(ctx: RequestContext) -> ResponseContext:
    """PUT /config/gate/ — update configuration."""
    body = ctx.json_body or {}
    updates = {k: v for k, v in body.items() if k in _ALLOWED_CONFIG_FIELDS}

    if not updates:
        return ResponseContext.json(
            {
                "status": "error",
                "error": "No valid configuration fields provided",
                "allowed_fields": sorted(_ALLOWED_CONFIG_FIELDS),
            },
            status_code=400,
        )

    gate = _get_gate()
    config = gate.update_config(**updates)

    return ResponseContext.json(
        {
            "status": "success",
            "message": f"Updated {len(updates)} configuration field(s)",
            "updated_fields": list(updates.keys()),
            "config": config.to_dict(),
        }
    )


def gate_reset(ctx: RequestContext) -> ResponseContext:
    """POST /gate/reset/ — reset gate components."""
    body = ctx.json_body or {}
    component = body.get("component", "all")
    reset_actions: list[str] = []

    gate = _get_gate()

    if component in ("all", "cache"):
        gate.clear_cache()
        reset_actions.append("cache")

    if component in ("all", "rate_limiter"):
        gate.reset_rate_limiter()
        reset_actions.append("rate_limiter")

    # Use reset_fault_detector() instead of deprecated reset_circuit_breaker()
    if component in ("all", "circuit_breaker"):
        gate.reset_fault_detector()
        reset_actions.append("circuit_breaker")

    if component in ("all", "alerts"):
        gate.reset_alert_cooldowns()
        reset_actions.append("alerts")

    if not reset_actions:
        return ResponseContext.json(
            {"status": "error", "error": f"Unknown component: {component}"},
            status_code=400,
        )

    return ResponseContext.json(
        {
            "status": "success",
            "message": f"Reset completed for: {', '.join(reset_actions)}",
            "reset_components": reset_actions,
        }
    )
