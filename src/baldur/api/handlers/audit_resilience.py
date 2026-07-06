"""
Framework-agnostic audit resilience handlers.

Extracted from api/django/views/audit_resilience.py (Phase 2b).
Covers circuit breaker control, audit metrics, degraded mode control,
and metrics reset.

Endpoints:
    POST /resilience/cb/reset/{name}       Reset a specific CB
    POST /resilience/cb/force-open/{name}  Force open a specific CB
    POST /resilience/cb/reset-all          Reset all CBs
    GET  /resilience/audit-metrics         Audit metrics (Prometheus/JSON)
    GET  /resilience/degraded-mode           Degraded mode status
    POST /resilience/degraded-mode/{action}  Force degraded mode enter/exit
    POST /resilience/metrics/reset         Reset metrics
"""

from __future__ import annotations

import structlog

from baldur.interfaces.web_framework import RequestContext, ResponseContext

logger = structlog.get_logger()

__all__ = [
    "circuit_breaker_reset",
    "circuit_breaker_force_open",
    "circuit_breaker_reset_all",
    "audit_metrics",
    "degraded_mode_status",
    "degraded_mode_force",
    "metrics_reset",
]


def _cb_registry():
    from baldur.audit.resilience import CircuitBreakerRegistry

    return CircuitBreakerRegistry.get_instance()


def _audit_metrics_instance():
    from baldur.audit import get_audit_metrics

    return get_audit_metrics()


def _degraded_mode_manager():
    from baldur.audit import get_degraded_mode_manager

    return get_degraded_mode_manager()


def circuit_breaker_reset(ctx: RequestContext) -> ResponseContext:
    """POST /resilience/cb/reset/{name} — reset a specific CB (admin)."""
    name = ctx.get_path_param("name")
    try:
        registry = _cb_registry()
        cb = registry.get(name)

        if not cb:
            return ResponseContext.not_found(f"Circuit breaker '{name}' not found")

        cb.reset()

        return ResponseContext.json(
            {
                "message": f"Circuit breaker '{name}' reset successfully",
                "state": cb.get_stats(),
            }
        )
    except Exception as e:
        logger.exception("audit_resilience.circuit_breaker_reset_failed", error=e)
        return ResponseContext.server_error(str(e))


def circuit_breaker_force_open(ctx: RequestContext) -> ResponseContext:
    """POST /resilience/cb/force-open/{name} — force open a specific CB (admin)."""
    name = ctx.get_path_param("name")
    try:
        registry = _cb_registry()
        cb = registry.get(name)

        if not cb:
            return ResponseContext.not_found(f"Circuit breaker '{name}' not found")

        cb.force_open()

        return ResponseContext.json(
            {
                "message": f"Circuit breaker '{name}' forced open",
                "state": cb.get_stats(),
            }
        )
    except Exception as e:
        logger.exception("audit_resilience.circuit_breaker_force_open_failed", error=e)
        return ResponseContext.server_error(str(e))


def circuit_breaker_reset_all(ctx: RequestContext) -> ResponseContext:
    """POST /resilience/cb/reset-all — reset all CBs (admin)."""
    try:
        registry = _cb_registry()
        registry.reset_all()

        return ResponseContext.json(
            {
                "message": "All circuit breakers reset",
                "circuit_breakers": registry.get_all_stats(),
            }
        )
    except Exception as e:
        logger.exception("audit_resilience.circuit_breaker_reset_all_failed", error=e)
        return ResponseContext.server_error(str(e))


def audit_metrics(ctx: RequestContext) -> ResponseContext:
    """GET /resilience/audit-metrics — audit metrics in Prometheus or JSON format (viewer)."""
    try:
        metrics = _audit_metrics_instance()
        output_format = ctx.get_query("format", "prometheus")

        if output_format == "json":
            return ResponseContext.json(metrics.get_metrics())
        content = metrics.get_prometheus_format()
        return ResponseContext.raw(
            content,
            content_type="text/plain; version=0.0.4; charset=utf-8",
        )
    except Exception as e:
        logger.exception("audit_resilience.audit_metrics_failed", error=e)
        return ResponseContext.server_error(str(e))


def degraded_mode_status(ctx: RequestContext) -> ResponseContext:
    """GET /resilience/degraded-mode — degraded mode status (viewer)."""
    try:
        manager = _degraded_mode_manager()
        return ResponseContext.json(manager.get_status())
    except Exception as e:
        logger.exception("audit_resilience.degraded_mode_status_failed", error=e)
        return ResponseContext.server_error(str(e))


def degraded_mode_force(ctx: RequestContext) -> ResponseContext:
    """POST /resilience/degraded-mode/{action} — force degraded mode enter/exit (admin)."""
    action = ctx.get_path_param("action")
    try:
        manager = _degraded_mode_manager()

        if action == "enter":
            body = ctx.json_body or {}
            reason = body.get("reason", "Manual override via API")
            manager.force_degraded(reason)
            return ResponseContext.json(
                {
                    "message": "Forced into degraded mode",
                    "status": manager.get_status(),
                }
            )
        if action == "exit":
            manager.force_normal()
            return ResponseContext.json(
                {
                    "message": "Forced exit from degraded mode",
                    "status": manager.get_status(),
                }
            )
        return ResponseContext.bad_request(
            f"Unknown action: {action}. Use 'enter' or 'exit'"
        )
    except Exception as e:
        logger.exception("audit_resilience.degraded_mode_force_failed", error=e)
        return ResponseContext.server_error(str(e))


def metrics_reset(ctx: RequestContext) -> ResponseContext:
    """POST /resilience/metrics/reset — reset all metrics (operator)."""
    try:
        metrics = _audit_metrics_instance()
        metrics.reset()
        return ResponseContext.json({"message": "Metrics reset successfully"})
    except Exception as e:
        logger.exception("audit_resilience.metrics_reset_failed", error=e)
        return ResponseContext.server_error(str(e))
