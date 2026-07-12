"""
Circuit-breaker middleware helpers — framework-free.

Two distinct phases:

- ``check_cb_open(req, service_name)`` — pre-flight: returns 503 when the CB
  for the inferred (or explicit) service is OPEN / HALF_OPEN, ``None`` to
  allow through. Mirrors the preemptive-503 behavior in
  ``api/django/middleware/baldur.py:195-232`` minus the DLQ-storage side
  effect (which stays Django-coupled in PR4 — see Part 3 scope discipline).

- ``record_cb_observation(req, status_code)`` — post-response: records the
  observed HTTP status as a CB success (2xx/3xx) or failure (5xx). Pure
  side-effect, returns ``None``. Splitting this out of ``check_cb_open``
  keeps the rejection-decision signature honest.

Domain inference is a no-op default: ``check_cb_open`` only checks the CB
when ``service_name`` is explicitly supplied. Path-based domain inference
(``BALDUR_DOMAIN_MAPPING``) remains in ``BaldurMiddleware`` for now to avoid
silently changing inference behavior across frameworks before the central
domain-mapping settings move to ``settings/``.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any

import structlog

from baldur.interfaces.web_framework import ResponseContext
from baldur.utils.time import utc_now

if TYPE_CHECKING:
    from baldur.interfaces.web_framework import RequestContext

logger = structlog.get_logger()


__all__ = [
    "check_cb_open",
    "record_cb_observation",
]


# Status codes that count as upstream/server failures (matches the default
# in ``baldur.settings.middleware.MiddlewareSettings.cb_status_codes``).
_DEFAULT_CB_FAILURE_CODES = frozenset({500, 502, 503, 504})

# Minimum Retry-After advertised on a CB rejection — a smaller value would
# invite clients to hammer a dependency the breaker just judged unhealthy.
_RETRY_AFTER_FLOOR_SECONDS = 1


def _compute_retry_after(service: Any, service_name: str, state_data: Any) -> int:
    """Remaining recovery window in whole seconds (floored to >= 1).

    OPEN with a known ``opened_at``: ``recovery_timeout - elapsed``, so the
    client backs off until the breaker can transition to HALF_OPEN.
    HALF_OPEN (or a missing ``opened_at``): the full effective
    ``recovery_timeout`` as the conservative bound. Falls back to the base
    config when the override-aware lookup fails.
    """
    try:
        recovery_timeout = float(
            service.get_effective_config(service_name).recovery_timeout
        )
    except Exception:
        recovery_timeout = float(service.config.recovery_timeout)
    remaining = recovery_timeout
    opened_at = getattr(state_data, "opened_at", None)
    if opened_at is not None and state_data.state.lower() == "open":
        remaining = recovery_timeout - (utc_now() - opened_at).total_seconds()
    return max(_RETRY_AFTER_FLOOR_SECONDS, math.ceil(remaining))


def _try_get_cb_service():
    """Return the CB service singleton or ``None`` on import/init failure.

    Resolved lazily so this module imports cleanly even when CB
    infrastructure is unavailable, and so unit tests can monkeypatch
    ``get_circuit_breaker_service`` without the helper holding a stale
    reference.
    """
    try:
        from baldur.services.circuit_breaker.convenience import (
            get_circuit_breaker_service,
        )
    except ImportError:
        return None
    try:
        service = get_circuit_breaker_service()
    except Exception as exc:
        logger.warning("middleware.cb_service_init_failed", error=exc)
        return None
    return service


def _failure_status_codes() -> frozenset[int]:
    """Read the configured CB-failure status set.

    Falls back to the conservative default (5xx server errors) when the
    settings layer is unavailable so an isolated import does not break the
    helper.
    """
    try:
        from baldur.settings.middleware import get_middleware_settings

        return frozenset(get_middleware_settings().cb_status_codes)
    except Exception:
        return _DEFAULT_CB_FAILURE_CODES


def check_cb_open(
    request: RequestContext,
    service_name: str | None = None,
) -> ResponseContext | None:
    """Reject the request with 503 when the CB for ``service_name`` is open.

    Returns ``None`` when the CB is closed, the service name was not
    supplied, or the CB infrastructure is unavailable (fail-open — a broken
    health check should never block legitimate traffic).
    """
    if service_name is None:
        return None

    service = _try_get_cb_service()
    if service is None:
        return None

    try:
        if not service.is_enabled:
            return None
        state_data = service.get_or_create_state(service_name)
        state = state_data.state
    except Exception as exc:
        logger.warning(
            "middleware.cb_state_check_failed",
            service_name=service_name,
            error=exc,
        )
        return None

    if not state or state.lower() not in ("open", "half_open"):
        return None

    logger.warning(
        "middleware.request_blocked_cb_open",
        service_name=service_name,
        state=state,
        path=request.path,
    )

    return ResponseContext(
        status_code=503,
        body={
            "error": "service_unavailable",
            "message": "Upstream service is currently unavailable",
            "service": service_name,
            "code": "CIRCUIT_BREAKER_OPEN",
        },
        headers={
            "Retry-After": str(_compute_retry_after(service, service_name, state_data)),
            "X-Baldur-Circuit-Breaker": state.lower(),
        },
    )


def record_cb_observation(
    request: RequestContext,
    status_code: int,
    service_name: str | None = None,
) -> None:
    """Record the response as a CB success or failure observation.

    No-op when ``service_name`` is not supplied so callers without a known
    upstream identity cannot accidentally pollute a CB bucket. The observed
    status is bucketed via the configured ``cb_status_codes`` set so an
    operator who whitelists 502 only (for example) gets consistent behavior
    across frameworks.
    """
    if service_name is None:
        return

    service = _try_get_cb_service()
    if service is None:
        return

    try:
        if not service.is_enabled:
            return
        if status_code in _failure_status_codes():
            service.record_failure(
                service_name,
                error_context={
                    "error_type": f"HTTP_{status_code}",
                    "path": request.path,
                    "method": (
                        request.method.value
                        if hasattr(request.method, "value")
                        else str(request.method)
                    ),
                },
            )
        elif 200 <= status_code < 400:
            service.record_success(service_name)
    except Exception as exc:
        logger.warning(
            "middleware.cb_observation_failed",
            service_name=service_name,
            status_code=status_code,
            error=exc,
        )
