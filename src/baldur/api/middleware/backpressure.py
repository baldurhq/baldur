"""
Backpressure middleware helpers — framework-free.

Wraps ``baldur.scaling.rate_controller.RateController`` and
``baldur.scaling.graceful_degradation.GracefulDegradation`` (both already
framework-free) into pure functions that adapters can call.

When ``baldur.scaling`` is unavailable (e.g. a slim install without the
graceful-degradation extras), every helper degrades to a no-op rather than
raising — backpressure is fail-open by design (an overload check that itself
fails should never block traffic).
"""

from __future__ import annotations

from collections.abc import MutableMapping
from typing import TYPE_CHECKING

import structlog

from baldur.interfaces.web_framework import ResponseContext
from baldur.settings.backpressure import get_backpressure_settings

if TYPE_CHECKING:
    from baldur.interfaces.web_framework import RequestContext

logger = structlog.get_logger()


__all__ = [
    "check_backpressure",
    "apply_backpressure_headers",
]


def _try_get_controllers():
    """Return ``(controller, degradation)`` or ``(None, None)`` on import failure.

    Resolved lazily so unit tests can monkeypatch the singletons without the
    helper holding a stale reference, and so a missing scaling module does
    not break import of this file.
    """
    try:
        from baldur.scaling.graceful_degradation import get_graceful_degradation
        from baldur.scaling.rate_controller import get_rate_controller
    except ImportError:
        return None, None
    return get_rate_controller(), get_graceful_degradation()


def check_backpressure(request: RequestContext) -> ResponseContext | None:
    """Reject the request with 503 when the rate controller is overloaded.

    Returns ``None`` to allow the request through. Returns a 503
    ``ResponseContext`` carrying ``Retry-After`` and
    ``X-Baldur-Backpressure-Level`` headers when overloaded — the
    Retry-After value scales with the current backpressure level so clients
    back off harder under heavier load (see
    ``BackpressureSettings.get_retry_after_for_level``).
    """
    settings = get_backpressure_settings()
    if not settings.backpressure_enabled:
        return None

    controller, _ = _try_get_controllers()
    if controller is None:
        return None

    if controller.should_process():
        return None

    current_level = controller.get_state().level
    retry_after = settings.get_retry_after_for_level(current_level)

    logger.info(
        "backpressure.request_rejected",
        current_level=current_level.value,
    )

    return ResponseContext(
        status_code=503,
        body={
            "error": "service_unavailable",
            "message": settings.reject_message,
            "level": current_level.value,
            "retry_after": retry_after,
        },
        headers={
            "Retry-After": str(retry_after),
            "X-Baldur-Backpressure-Level": current_level.value,
        },
    )


def apply_backpressure_headers(headers: MutableMapping[str, str]) -> None:
    """Add backpressure headers to a successful response in-place.

    Mutates ``headers`` to add ``X-Baldur-Backpressure-Level`` and
    ``X-Baldur-Degraded-Features`` (when any features are degraded). Adapters
    call this after their downstream returns a successful response so clients
    can observe degradation without parsing 503s.
    """
    settings = get_backpressure_settings()
    if not settings.backpressure_enabled:
        return

    controller, degradation = _try_get_controllers()
    if controller is None:
        return

    headers["X-Baldur-Backpressure-Level"] = controller.get_state().level.value

    if degradation is not None:
        disabled = degradation.get_disabled_features()
        if disabled:
            headers["X-Baldur-Degraded-Features"] = ",".join(disabled)
