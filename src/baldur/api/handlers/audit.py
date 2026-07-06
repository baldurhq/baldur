"""
Framework-agnostic audit resilience handlers.

Extracted from api/django/views/audit_resilience.py — pure functions with
no Django/DRF imports.
"""

from __future__ import annotations

import structlog

from baldur.interfaces.web_framework import RequestContext, ResponseContext

logger = structlog.get_logger()

__all__ = [
    "audit_health",
    "circuit_breaker_status",
]


def audit_health(ctx: RequestContext) -> ResponseContext:
    """Get audit system health status.

    416 (D8): the rich per-backend health table from the deleted
    ``CompositeBackend`` is replaced with the resolved adapter name
    from ``ProviderRegistry``. If richer health is needed later, add
    ``health_check()`` to the ``AuditLogAdapter`` ABC.
    """
    try:
        from baldur.audit import get_degraded_mode_manager
        from baldur.audit.resilience import CircuitBreakerRegistry
        from baldur.factory import ProviderRegistry

        degraded_manager = get_degraded_mode_manager()
        circuit_registry = CircuitBreakerRegistry.get_instance()

        backend_health = {"adapter": ProviderRegistry.audit.get_default_name()}
        degraded_status = degraded_manager.get_status()
        circuit_stats = circuit_registry.get_all_stats()
        open_circuits = circuit_registry.get_open_circuits()

        if degraded_status["degraded"]:
            status = "degraded"
        elif open_circuits:
            status = "warning"
        else:
            status = "healthy"

        return ResponseContext.json(
            {
                "status": status,
                "backend": backend_health,
                "degraded_mode": degraded_status,
                "circuit_breakers": {
                    "open_count": len(open_circuits),
                    "open_backends": open_circuits,
                    "total_count": len(circuit_stats),
                },
            }
        )

    except Exception as e:
        logger.exception("audit_health_handler.error", error=e)
        return ResponseContext.json(
            {"status": "error", "error": str(e)},
            status_code=500,
        )


def circuit_breaker_status(ctx: RequestContext) -> ResponseContext:
    """Get audit circuit breaker status (list or detail)."""
    name = ctx.path_params.get("name")

    try:
        from baldur.audit.resilience import CircuitBreakerRegistry

        registry = CircuitBreakerRegistry.get_instance()

        if name:
            cb = registry.get(name)
            if not cb:
                return ResponseContext.not_found(f"Circuit breaker '{name}' not found")
            return ResponseContext.json(cb.get_stats())
        return ResponseContext.json(
            {
                "circuit_breakers": registry.get_all_stats(),
                "open_circuits": registry.get_open_circuits(),
            }
        )

    except Exception as e:
        logger.exception("circuit_breaker_status_handler.error", error=e)
        return ResponseContext.json({"error": str(e)}, status_code=500)
