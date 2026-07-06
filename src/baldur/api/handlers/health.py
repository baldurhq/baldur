"""
Framework-agnostic health check handlers.

Extracted from api/django/views/health.py — pure functions with
no Django/DRF imports.
"""

from __future__ import annotations

import structlog

from baldur.interfaces.web_framework import RequestContext, ResponseContext

logger = structlog.get_logger()

__all__ = [
    "liveness_check",
    "readiness_check",
    "health_check",
    "pool_health_check",
    "simple_health_ping",
]


# 473 D6 — HTTP status mapping for the cascade (plan §329 contract).
# - "unhealthy"   — DB severed (D7 axis 1 (b) emission).
# - "error"       — compute_health_status() exception path.
# - "unavailable" — multi-tier cache CB-OPEN static fallback.
# - "healthy" / "degraded" → 200 (plan §329: "LB receives 503 only on
#   unhealthy"). Watchdog dampening keeps "degraded" out of the depool set.
_HEALTH_HTTP_503_STATUSES: frozenset[str] = frozenset(
    {"unhealthy", "error", "unavailable"}
)
_HEALTH_HTTP_200_STATUSES: frozenset[str] = frozenset({"healthy", "degraded"})


def _map_health_status_to_http(data: dict) -> int:
    """Map cascade response body to HTTP status per plan §329.

    Unknown status values default to 200 — silently depooling a healthy
    cluster on a contract violation is worse than leaving the bad row in
    the pool — but a WARNING log surfaces the bug for operators.
    """
    status = data.get("status")
    if (
        status not in _HEALTH_HTTP_503_STATUSES
        and status not in _HEALTH_HTTP_200_STATUSES
    ):
        logger.warning("health_check.unknown_status_emitted", status=status)
    return 503 if status in _HEALTH_HTTP_503_STATUSES else 200


def liveness_check(ctx: RequestContext) -> ResponseContext:
    """Kubernetes-style liveness probe. Always returns 200 if running."""
    return ResponseContext.json({"status": "alive"})


def readiness_check(ctx: RequestContext) -> ResponseContext:
    """Kubernetes-style readiness probe. Checks if app can serve traffic."""
    from baldur.services.health_check import get_health_check_service

    service = get_health_check_service()
    readiness = service.get_readiness()

    response_data = readiness.to_dict()
    del response_data["is_ready"]

    if not readiness.is_ready:
        return ResponseContext.json(response_data, status_code=503)

    return ResponseContext.json(response_data)


def health_check(ctx: RequestContext) -> ResponseContext:
    """Baldur system health check with V3 multi-tier cache.

    Maps cascade ``status`` to HTTP code per plan §329 (473 D6) on both
    the precomputed-cache path and the ImportError fallback path.
    """
    use_cache = ctx.get_query("nocache", "").lower() != "true"

    try:
        from baldur.services.precomputed_cache import (
            compute_health_status,
            get_cached_health,
        )

        if use_cache:
            data = get_cached_health()
        else:
            data = compute_health_status()
            data["_cache"] = {"hit": "BYPASSED"}

        return ResponseContext.json(data, status_code=_map_health_status_to_http(data))

    except ImportError:
        from baldur.services.health_check import get_health_check_service

        service = get_health_check_service()
        health = service.get_overall_health()
        data = health.to_dict()
        return ResponseContext.json(data, status_code=_map_health_status_to_http(data))


def pool_health_check(ctx: RequestContext) -> ResponseContext:
    """Connection pool health status."""
    from baldur.services.health_check import get_health_check_service

    service = get_health_check_service()
    pool_health = service.get_pool_health()

    response_data = pool_health.to_dict()
    if response_data.get("error") is None:
        del response_data["error"]

    if pool_health.status in ("degraded", "error"):
        return ResponseContext.json(response_data, status_code=503)

    return ResponseContext.json(response_data)


def simple_health_ping(ctx: RequestContext) -> ResponseContext:
    """Ultra-lightweight ping for load balancer checks.

    No DB access, no service layer, no authentication. Target <1ms.
    """
    return ResponseContext.json(
        {"ping": "pong", "status": "alive"},
        headers={"Cache-Control": "max-age=1"},
    )
