"""
Framework-agnostic Error Budget Status handlers.

Extracted from api/django/views/error_budget/status.py — budget status,
history, error recording, exhaustion simulation, and simulation reset.

Endpoints:
    GET  /error-budget/status/            Get Error Budget status (V3: cached)
    GET  /error-budget/history/           Get budget consumption history
    POST /error-budget/record/            Record errors (Chaos Engineering)
    POST /error-budget/exhaust/           Simulate budget exhaustion (Test)
    POST /error-budget/reset-simulation/  Reset simulation stats

FAIL-SAFE DESIGN:
    Error Budget system failure -> default PROCEED (fail-open).
    System availability is more important than blocking deployments.

V3 Optimization:
    L1 In-process cache (2s TTL) + L2 Redis cache (15s TTL).
    Target: P95 < 20ms for /error-budget/status/.
"""

from __future__ import annotations

import structlog

from baldur.interfaces.web_framework import RequestContext, ResponseContext
from baldur.utils.time import utc_now

logger = structlog.get_logger()

__all__ = [
    "budget_status",
    "budget_history",
    "budget_record",
    "budget_exhaust",
    "budget_reset_simulation",
]


def _service():
    from baldur.factory.registry import ProviderRegistry

    service = ProviderRegistry.error_budget_service.safe_get()
    if service is None:
        raise RuntimeError(
            "Error budget handlers require baldur_pro ErrorBudgetService"
        )
    return service


def _failsafe_status(error_str: str):
    from baldur_pro.services.error_budget import get_failsafe_status_response

    return get_failsafe_status_response(error_str)


def budget_status(ctx: RequestContext) -> ResponseContext:
    """GET /error-budget/status/ — current Error Budget status.

    Query Parameters:
        slo_name: SLO name to check (default: "availability")
        nocache:  Set to "true" to bypass cache (V3)

    V3 Optimization: Uses multi-tier cache for P95 < 20ms target.
    """
    try:
        slo_name = ctx.get_query("slo_name", "availability")
        use_cache = ctx.get_query("nocache", "").lower() != "true"

        # V3: Use cached response for default SLO
        if use_cache and slo_name == "availability":
            try:
                from baldur.services.precomputed_cache import (
                    get_cached_error_budget,
                )

                return ResponseContext.json(get_cached_error_budget())
            except ImportError:
                pass  # Fall through to direct computation

        # Direct computation for non-default SLO or if cache unavailable
        service = _service()
        budget_status_data = service.get_budget_status(slo_name)

        return ResponseContext.json(
            {
                "status": "success",
                "data": budget_status_data.to_dict(),
                "timestamp": utc_now().isoformat(),
            }
        )

    except Exception as e:
        logger.exception(
            "error_budget_api.status_failed",
            error=e,
        )
        # FAIL-SAFE: provide default response on system failure (200 OK)
        return ResponseContext.json(_failsafe_status(str(e)))


def budget_history(ctx: RequestContext) -> ResponseContext:
    """GET /error-budget/history/ — budget decision history.

    Query Parameters:
        limit:         Maximum records to return (default: 50)
        decision_type: Filter by type (freeze_acknowledged, override_approved, freeze_lifted)
    """
    limit = int(ctx.get_query("limit", 50))
    decision_type = ctx.get_query("decision_type")

    service = _service()
    history = service.get_decision_history(limit=limit, decision_type=decision_type)

    return ResponseContext.json(
        {
            "status": "success",
            "data": {
                "records": [r.to_dict() for r in history],
                "count": len(history),
            },
            "timestamp": utc_now().isoformat(),
        }
    )


def budget_record(ctx: RequestContext) -> ResponseContext:
    """POST /error-budget/record/ — record errors for budget consumption.

    Request Body:
        error_count:  Number of errors to record (default: 1)
        error_type:   Error type (default: "simulated")
        service_name: Service name (default: "test")
        domain:       Domain name (default: service_name)
        severity:     low / medium / high / critical (default: "medium")
        multiplier:   Weight multiplier (default: 1.0)
        reason:       Reason for recording (optional)

    WARNING: This API should only be used in test/Chaos environments.
    """
    body = ctx.json_body or {}

    error_count = int(body.get("error_count", 1))
    error_type = body.get("error_type", "simulated")
    service_name = body.get("service_name", "test")

    # Extended format: domain, severity, multiplier
    domain = body.get("domain", service_name)
    severity = body.get("severity", "medium")
    multiplier = float(body.get("multiplier", 1.0))
    reason = body.get("reason", "")

    # Severity-based weight adjustment
    severity_weights = {
        "low": 1,
        "medium": 3,
        "high": 5,
        "critical": 10,
    }
    base_errors = severity_weights.get(severity, 1)
    effective_errors = int(base_errors * multiplier * error_count)

    if effective_errors < 1:
        effective_errors = 1

    service = _service()
    result = service.record_error(
        error_count=effective_errors,
        error_type=error_type,
        service_name=domain,
    )

    # Add extended info
    result["severity"] = severity
    result["multiplier"] = multiplier
    result["domain"] = domain
    result["effective_errors"] = effective_errors
    if reason:
        result["reason"] = reason

    logger.info(
        "error_budget_api.recorded_errors",
        effective_errors=effective_errors,
        healing_domain=domain,
        severity=severity,
        multiplier=multiplier,
    )

    return ResponseContext.json(
        {
            "status": "success",
            "message": f"{effective_errors} error(s) recorded for budget consumption",
            "data": result,
            "remaining_percent": result.get("budget_remaining_percent"),
            "timestamp": utc_now().isoformat(),
        }
    )


def budget_exhaust(ctx: RequestContext) -> ResponseContext:
    """POST /error-budget/exhaust/ — simulate budget exhaustion.

    Request Body:
        target_remaining_percent: Target remaining percentage (default: 0.0 = full exhaustion)

    WARNING: This API should only be used in test/Chaos environments.
    """
    body = ctx.json_body or {}
    target = float(body.get("target_remaining_percent", 0.0))

    if target < 0 or target > 100:
        return ResponseContext.bad_request(
            "target_remaining_percent must be between 0 and 100"
        )

    service = _service()
    result = service.simulate_budget_exhaustion(target_remaining_percent=target)

    logger.warning(
        "error_budget_api.budget_exhaustion_simulated",
        target=target,
    )

    return ResponseContext.json(
        {
            "status": "success",
            "message": f"Budget exhaustion simulated to {target}%",
            "data": result,
            "timestamp": utc_now().isoformat(),
        }
    )


def budget_reset_simulation(ctx: RequestContext) -> ResponseContext:
    """POST /error-budget/reset-simulation/ — reset simulation stats.

    Resets error/request statistics recorded through simulation.
    """
    service = _service()
    result = service.reset_simulated_stats()

    logger.info("error_budget_api.simulation_stats_reset")

    return ResponseContext.json(
        {
            "status": "success",
            "message": "Simulation stats reset",
            "data": result,
            "timestamp": utc_now().isoformat(),
        }
    )
