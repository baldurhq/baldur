"""Request-tracking middleware (impl 471).

Wraps each request in a :class:`RequestLifecycleContext` so the
graceful-shutdown drain loop can observe HTTP in-flight count via
``coordinator._tracker.get_pending_count()``. Without this middleware the
drain loop sees 0 in-flight HTTP work and declares HTTP drained instantly,
which means a 25s POST during shutdown can be SIGKILLed at gunicorn
``--graceful-timeout``.

Pattern source: ``baldur.api.django.middleware.backpressure`` (single-
purpose middleware shape) + :class:`RequestLifecycleContext` (existing
try/finally context manager wrapping ``start_request`` / ``end_request``).

Request-id source: ``request.trace_id`` set by
``baldur.audit.trace.trace_id_middleware`` at ``DEFAULT_EARLY_GROUP[0]``.
The defensive fallback (``generate_trace_id()``) handles the manual-wiring
edge case where the user removed or reordered ``trace_id_middleware``.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

import structlog

from baldur.audit.trace import generate_trace_id
from baldur.core.request_context import RequestLifecycleContext

if TYPE_CHECKING:
    from django.http import HttpRequest, HttpResponse

logger = structlog.get_logger()


class RequestTrackingMiddleware:
    """Track in-flight HTTP requests for the graceful-shutdown drain loop.

    Two failure paths feed ``end_request(success=False)``:

    1. ``get_response`` raises — :meth:`RequestLifecycleContext.__exit__`
       sees ``exc_type is not None`` and flips ``_success = False``.
    2. ``get_response`` returns 5xx (custom Django 500 handler that
       swallows the exception) — this middleware MUST call
       ``ctx.mark_failed()`` explicitly inside the ``with`` block,
       because ``__exit__`` cannot inspect the response.

    The middleware is a no-op when the coordinator's ``_tracker`` is
    None (deployments that initialize the coordinator without a tracker).
    """

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]):
        self.get_response = get_response
        from baldur.core.shutdown_coordinator import get_shutdown_coordinator

        self._coordinator = get_shutdown_coordinator()

    def __call__(self, request: HttpRequest) -> HttpResponse:
        tracker = self._coordinator._tracker
        if tracker is None:
            return self.get_response(request)

        request_id = getattr(request, "trace_id", None) or generate_trace_id()
        with RequestLifecycleContext(
            tracker=tracker,
            request_id=request_id,
            endpoint=request.path,
            method=request.method or "",
        ) as ctx:
            response = self.get_response(request)
            if response.status_code >= 500:
                ctx.mark_failed()
            return response
