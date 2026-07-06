"""
Admission Control Middleware (Django).

Thin Django wrapper around the framework-free ``check_admission`` helper
(``api/middleware/admission.py``). It classifies the request path into a tier
(TierRegistry) and runs the TrafficGate pipeline (per-tier Bulkhead ->
CascadeLoadShedding -> RateController) for priority-based admission control,
rejecting shed requests with 503 + Retry-After.

The shared core — classification, degraded-tier forced deadline, cell-aware
bulkhead naming, the TrafficGate decision, and bulkhead release — lives in
``check_admission`` so Django / Flask / FastAPI share one implementation (no
drift). The two inbound-deadline extras are likewise framework-free helpers
(``check_deadline`` / ``record_rtt_sample``) composed identically across all
three frameworks:

- inbound ``X-Deadline-Remaining`` header fast-fail (pre-step, before
  ``check_admission`` so the inbound deadline is set before admission's
  degraded-tier forced-deadline decision reads it)
- RTT gradient sampling on a successful response (post-step; feeds the dynamic
  fast-fail estimate)

Capability ladder: per-tier Bulkhead isolation requires ``baldur_pro``. With
``baldur_pro`` absent, ``check_admission`` is a clean no-op (``active=False``)
and this middleware passes the request straight through — the OSS baseline
backpressure middleware remains the active rate gate. No self-disable.

Middleware order:
    AdmissionControlMiddleware -> TieringMiddleware -> Application
    (AdmissionControlMiddleware supersedes BackpressureMiddleware when active.)

Configuration:
    # settings.py
    MIDDLEWARE = [
        ...
        'baldur.api.django.admission_control.AdmissionControlMiddleware',
        ...
    ]
"""

from __future__ import annotations

import time

import structlog

from baldur.api.middleware import check_admission, check_deadline, record_rtt_sample
from baldur.interfaces.web_framework import HttpMethod, RequestContext

logger = structlog.get_logger()


class AdmissionControlMiddleware:
    """
    HTTP admission-control middleware.

    Composes the framework-free helpers: ``check_deadline`` (inbound deadline-
    header fast-fail, pre-step), ``check_admission`` (the core decision), and
    ``record_rtt_sample`` (RTT sampling, post-step). Rejected requests get a 503
    response.
    """

    def __init__(self, get_response):
        self.get_response = get_response
        self._enabled = True
        self._settings = None

        try:
            from baldur.settings.admission_control import (
                get_admission_control_settings,
            )

            self._settings = get_admission_control_settings()
            self._enabled = self._settings.enabled
        except Exception as e:
            logger.warning(
                "admission_control_middleware.settings_load_failed",
                error=str(e),
            )

        if self._enabled:
            logger.info("admission_control_middleware.initialized_enabled")
        else:
            logger.info("admission_control_middleware.initialized_disabled")

    def __call__(self, request):
        if not self._enabled:
            return self.get_response(request)

        try:
            return self._process_request(request)
        except Exception as e:
            logger.exception(
                "admission_control_middleware.request_error",
                error=str(e),
            )
            return self.get_response(request)
        finally:
            # Clear the request-scoped degraded deadline so it cannot leak onto
            # the next request on a reused sync (WSGI gthread) worker and
            # false-reject a later `critical` request at should_allow's
            # deadline-expiry step. One always-runs site for allow/reject/error.
            self._clear_deadline()

    def _process_request(self, request):
        """OPTIONS passthrough -> deadline-header fast-fail -> check_admission."""

        # CORS preflight is excluded from tier classification (always allow):
        # OPTIONS carries no body (negligible load), and rejecting it would
        # break the subsequent POST/DELETE with a CORS error.
        if request.method == "OPTIONS":
            return self.get_response(request)

        # Build a framework-free RequestContext. ``headers`` is populated so the
        # framework-free ``check_deadline`` can read X-Deadline-Remaining via
        # ``RequestContext.get_header`` (case-insensitive); ``check_admission``
        # ignores headers, so the addition is harmless to it.
        try:
            method = HttpMethod(request.method)
        except ValueError:
            method = HttpMethod.GET
        user = getattr(request, "user", None)
        is_authenticated = bool(getattr(user, "is_authenticated", False))
        ctx = RequestContext(
            method=method,
            path=request.path,
            headers=dict(request.headers.items()),
            client_ip=self._get_client_ip(request),
            user=user if is_authenticated else None,
            is_authenticated=is_authenticated,
        )

        # Inbound deadline-header fast-fail (pre-step) — MUST precede
        # check_admission so the inbound deadline is set before admission's
        # degraded-tier forced-deadline decision reads it.
        deadline_rejection = check_deadline(ctx)
        if deadline_rejection is not None:
            return self._to_django_response(deadline_rejection)

        decision = check_admission(ctx)

        if decision.rejection is not None:
            return self._to_django_response(decision.rejection)

        # Allow: run downstream, release the acquired bulkhead, then sample RTT.
        start_time = time.perf_counter()
        try:
            response = self.get_response(request)
        finally:
            if decision.release is not None:
                decision.release()

        if decision.active and decision.tier_id is not None:
            elapsed_ms = (time.perf_counter() - start_time) * 1000
            record_rtt_sample(decision.tier_id, response.status_code, elapsed_ms)

        return response

    # =========================================================================
    # Django-only wrappers
    # =========================================================================

    def _clear_deadline(self) -> None:
        """Clear the request-scoped degraded deadline (idempotent set(None))."""
        try:
            from baldur.scaling.deadline_context import (
                DEADLINE_ENABLED,
                clear_deadline,
            )
        except ImportError:
            return
        if DEADLINE_ENABLED:
            clear_deadline()

    def _get_client_ip(self, request) -> str | None:
        """Extract client IP from X-Forwarded-For or REMOTE_ADDR."""
        x_forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR")
        if x_forwarded_for:
            return str(x_forwarded_for.split(",")[0].strip())
        remote_addr = request.META.get("REMOTE_ADDR")
        return str(remote_addr) if remote_addr is not None else None

    def _to_django_response(self, response_ctx):
        """Convert a framework-free ``ResponseContext`` to a Django response."""
        from django.http import JsonResponse

        response = JsonResponse(
            response_ctx.body,
            status=response_ctx.status_code,
            safe=False,
        )
        for key, value in response_ctx.headers.items():
            response[key] = value
        return response
