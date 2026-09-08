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

from baldur.core.execution_mode import intervention_suppressed
from baldur.interfaces.web_framework import ResponseContext
from baldur.services.circuit_breaker.rate_limit_tracker import get_rate_limit_tracker
from baldur.services.retry_handler.rate_limit_detection import (
    failure_status_codes,
    rate_limit_status_codes,
)
from baldur.utils.time import utc_now

if TYPE_CHECKING:
    from baldur.interfaces.web_framework import RequestContext

logger = structlog.get_logger()


__all__ = [
    "check_cb_open",
    "record_cb_observation",
]


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


def check_cb_open(
    request: RequestContext,
    service_name: str | None = None,
) -> ResponseContext | None:
    """Reject the request with 503 when the CB for ``service_name`` is open.

    Returns ``None`` when the CB is closed, the service name was not
    supplied, or the CB infrastructure is unavailable (fail-open — a broken
    health check should never block legitimate traffic).

    Observe-only (dry-run / shadow / evaluation) also returns ``None``: the
    rejection is this seam's only intervention, so a mode that promises to
    decide without intervening must report the 503 rather than send it. The
    Django middleware gates its own preemptive branch the same way, and the
    breaker policy gates the outbound one — this is the third seam of the same
    decision, not a new posture.
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

    # Resolved before the reject is built, not after: the ResponseContext IS
    # the intervention, and the WARNING below announces a block that observe-only
    # never performs.
    if intervention_suppressed(
        service_name=service_name,
        action="circuit_breaker_reject",
        would_reject=True,
        path=request.path,
    ):
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
    """Record the response as a CB success, failure, or rate-limit observation.

    No-op when ``service_name`` is not supplied so callers without a known
    upstream identity cannot accidentally pollute a CB bucket. The observed
    status is bucketed via the configured ``cb_status_codes`` and
    ``rate_limit_codes`` sets, which the Django middleware and the outbound
    breaker stage read too, so an operator who whitelists 502 only (for
    example) gets consistent behavior across frameworks and directions.

    The two sets are not exclusive: a relayed 429 records a counted failure
    *and* feeds the rate-limit cascade, and a status listed in both does both.
    Every observed response also writes one request to the cascade rate's
    denominator - without it the rate would read 100% on any framework whose
    only writer is this helper.
    """
    if service_name is None:
        return

    service = _try_get_cb_service()
    if service is None:
        return

    try:
        if not service.is_enabled:
            return

        # After the guards above: an unnamed or disabled observation records
        # nothing at all, denominator included.
        get_rate_limit_tracker().record_request(service_name)

        is_failure = status_code in failure_status_codes()
        is_rate_limited = status_code in rate_limit_status_codes()

        if is_failure or is_rate_limited:
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

        if is_rate_limited:
            service.record_rate_limit_response(service_name)
    except Exception as exc:
        logger.warning(
            "middleware.cb_observation_failed",
            service_name=service_name,
            status_code=status_code,
            error=exc,
        )
