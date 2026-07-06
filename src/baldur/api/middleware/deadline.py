"""
Deadline middleware helpers — framework-free.

Ports the two formerly Django-only extras of ``AdmissionControlMiddleware`` —
inbound ``X-Deadline-Remaining`` fast-fail and post-response RTT gradient
sampling — into pure functions so Django / Flask / FastAPI handle the inbound
deadline symmetrically. Mirrors the established reject / observation helper
pipeline (``check_backpressure`` / ``check_cb_open`` / ``record_cb_observation``).

- ``check_deadline(request) -> ResponseContext | None`` — OTel-independent and
  OSS (NOT PRO-gated). Reads the explicit ``X-Deadline-Remaining`` header
  (Channel A, case-insensitive), sets the request-scoped deadline, and fast-fails
  with 503 when the remaining time is below the minimum useful threshold. Returns
  ``None`` (pass-through) on a header that is absent, malformed, or above the
  threshold — a broken / misconfigured upstream never blocks production traffic
  (fail-open by design).

- ``record_rtt_sample(tier_id, status_code, elapsed_ms) -> None`` — PRO-gated
  (imports ``baldur_pro...gradient``; ``ImportError`` -> clean no-op). Feeds a
  2xx RTT sample into the per-tier gradient calculator so the fast-fail estimate
  upgrades from static tier defaults to dynamic RTT-based estimates. Takes a
  pre-computed ``elapsed_ms`` so the helper is time-dependency-free and trivially
  unit-testable. Fail-open: RTT collection must never affect the request.
"""

from __future__ import annotations

import random
from typing import TYPE_CHECKING

import structlog

from baldur.interfaces.web_framework import ResponseContext

if TYPE_CHECKING:
    from baldur.interfaces.web_framework import RequestContext

logger = structlog.get_logger()


__all__ = [
    "check_deadline",
    "record_rtt_sample",
]


def check_deadline(request: RequestContext) -> ResponseContext | None:
    """Reject the request with 503 when the inbound deadline is near expiry.

    Reads the explicit ``X-Deadline-Remaining`` header (Channel A, OTel-
    independent, case-insensitive via ``RequestContext.get_header``), sets the
    request-scoped deadline, and fast-fails with a 503 ``ResponseContext``
    carrying ``Retry-After: 0`` and ``X-Baldur-Deadline-Rejected: "true"`` (so an
    L7 proxy / service mesh can distinguish a deadline fast-fail from a generic
    backend 503 without parsing the body) when the remaining time is below
    ``DEFAULT_MINIMUM_USEFUL_TIME_MS``.

    Returns ``None`` to let the request proceed — header absent, malformed
    (``parse_deadline_header`` returns ``None``), above the threshold, the
    deadline feature disabled, or the deadline module unavailable. Fail-open: a
    malformed inbound header never blocks traffic (it is a DEBUG-level event,
    not WARNING, so a chronically misconfigured upstream cannot flood logs).
    """
    try:
        from baldur.scaling.deadline_context import (
            DEADLINE_ENABLED,
            DEADLINE_HEADER,
            DEFAULT_MINIMUM_USEFUL_TIME_MS,
            parse_deadline_header,
            record_fast_fail,
            record_remaining_ms,
            set_deadline,
        )
    except ImportError:
        return None

    if not DEADLINE_ENABLED:
        return None

    header_value = request.get_header(DEADLINE_HEADER)
    if not header_value:
        return None

    remaining_ms = parse_deadline_header(header_value)
    if remaining_ms is None:
        return None

    set_deadline(remaining_ms)
    record_remaining_ms(remaining_ms)

    if remaining_ms < DEFAULT_MINIMUM_USEFUL_TIME_MS:
        path_prefix = (
            request.path.split("/")[1] if "/" in request.path else request.path
        )
        record_fast_fail(path_prefix=path_prefix)
        logger.info(
            "deadline.request_rejected",
            remaining_ms=remaining_ms,
            minimum_useful_ms=DEFAULT_MINIMUM_USEFUL_TIME_MS,
            path_prefix=path_prefix,
        )
        return ResponseContext(
            status_code=503,
            body={
                "error": "Deadline Exceeded",
                "code": "DEADLINE_FAST_FAIL",
                "message": (
                    "Request rejected immediately due to upstream deadline "
                    "approaching expiration."
                ),
                "remaining_ms": remaining_ms,
                "retry_after": 0,
            },
            headers={
                "Retry-After": "0",
                "X-Baldur-Deadline-Rejected": "true",
            },
        )

    return None


def record_rtt_sample(
    tier_id: str | None,
    status_code: int,
    elapsed_ms: float,
) -> None:
    """Feed a 2xx RTT sample into the per-tier gradient calculator.

    Triple filtering (mirrors the former inline Django RTT sampler):

    1. HTTP 2xx success only (requests that ran real business logic).
    2. ``elapsed_ms >= _RTT_MIN_SAMPLE_MS`` (drop health-check noise).
    3. Probabilistic sampling (``random() < _RTT_SAMPLE_RATE``) to reduce lock
       contention; the EMA gradient tracks the trend from a 10% sample.

    PRO-gated: the gradient calculator lives in ``baldur_pro``, so ``ImportError``
    (or any unexpected error) degrades to a clean no-op. ``tier_id`` is ``None``
    for the OSS admission no-op — the helper returns immediately. Fail-open: RTT
    collection failure must not affect the request.
    """
    if tier_id is None:
        return
    try:
        from baldur.scaling.deadline_context import (
            _RTT_MIN_SAMPLE_MS,
            _RTT_SAMPLE_RATE,
        )

        if not (200 <= status_code < 300):
            return
        if elapsed_ms < _RTT_MIN_SAMPLE_MS:
            return
        if random.random() >= _RTT_SAMPLE_RATE:
            return

        from baldur_pro.services.throttle.gradient import get_gradient_calculator

        get_gradient_calculator(f"admission_control:{tier_id}").add_sample(elapsed_ms)
    except Exception:
        pass  # Fail-open: RTT collection failure must not affect the request
