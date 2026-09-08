"""
Tiering Middleware.

Thin Django wrapper around the framework-free ``check_emergency_shedding``
helper (``api/middleware/emergency_shedding.py``). It classifies the request
path into a tier and sheds it with 503 according to the merged emergency /
backpressure per-tier multiplier.

The shared core — the PRO gate, the two level reads, tier classification, the
Most Restrictive Wins merge, the probabilistic decision and the 503 body —
lives in the helper so Django / Flask / FastAPI share one implementation (no
drift).

Capability ladder: the emergency level requires ``baldur_pro``. With it absent
(or unentitled) the helper is a clean no-op and this middleware passes the
request straight through. No self-disable, no per-request log record.
"""

from __future__ import annotations

import structlog

from baldur.api.middleware import check_emergency_shedding
from baldur.interfaces.web_framework import HttpMethod, RequestContext
from baldur.utils.network import extract_client_ip

logger = structlog.get_logger()


class TieringMiddleware:
    """
    Django Middleware for Emergency Mode Traffic Control.

    Controls traffic by API Tier in Emergency Mode.

    How it works:
    1. Check the current emergency mode level from EmergencyManager
    2. Check the Tier of the request path (using TierRegistry)
    3. Probabilistically allow/block the request according to the Tier multiplier
    4. Respond with 503 Service Unavailable when blocked

    Behavior per Emergency Level:
    - NORMAL (0): allow all requests
    - LEVEL_1 (1): block non_essential
    - LEVEL_2 (2): block 90% of standard, block 100% of non_essential
    - LEVEL_3 (3): block 50% of critical, block 100% of standard/non_essential

    Configuration:
        # settings.py
        MIDDLEWARE = [
            ...
            'baldur.api.django.tiering.TieringMiddleware',
            ...
        ]

        # Optional: Disable the middleware install (Django-only switch)
        BALDUR_TIERING_MIDDLEWARE_ENABLED = True

        # Optional: disable the decision on every framework
        BALDUR_EMERGENCY_MODE_SHEDDING_ENABLED=false

    Tier rules and multipliers:
        BACKPRESSURE_TIER_RULES (baldur.scaling.tiering.defaults),
        TierRegistry (baldur.scaling.tiering.registry)

    Prior art:
    - Netflix Hystrix Load Shedding
    - Google SRE "Handling Overload"
    """

    def __init__(self, get_response):
        """
        Initialize middleware.

        Args:
            get_response: Django's get_response callable
        """
        self.get_response = get_response

        self._enabled = self._check_enabled()

        if self._enabled:
            logger.info("tiering_middleware.initialized_enabled")
        else:
            logger.info("tiering_middleware.initialized_disabled")

    def _check_enabled(self) -> bool:
        """Check if middleware is enabled via settings."""
        try:
            from django.conf import settings

            return getattr(settings, "BALDUR_TIERING_MIDDLEWARE_ENABLED", True)
        except Exception:
            return True

    def __call__(self, request):
        """
        Process the request.

        Args:
            request: Django HttpRequest

        Returns:
            HttpResponse
        """
        if not self._enabled:
            return self.get_response(request)

        # CORS Preflight Bypass — OPTIONS is excluded from Load Shedding.
        # The helper repeats this check; keeping it here avoids the context
        # build for a preflight.
        if request.method == "OPTIONS":
            return self.get_response(request)

        try:
            ctx = self._build_request_context(request)
            rejection = check_emergency_shedding(ctx)
        except Exception as e:
            # Guards the context build and the conversion only — the decision
            # itself is fail-open inside the helper.
            logger.exception(
                "tiering_middleware.error_allowing_request",
                error=e,
            )
            return self.get_response(request)

        if rejection is not None:
            return self._to_django_response(rejection)

        return self.get_response(request)

    # =========================================================================
    # Django-only wrappers
    # =========================================================================

    def _build_request_context(self, request) -> RequestContext:
        """Snapshot Django's HttpRequest into Baldur's ``RequestContext``."""
        try:
            method = HttpMethod(request.method)
        except ValueError:
            method = HttpMethod.GET
        user = getattr(request, "user", None)
        is_authenticated = bool(getattr(user, "is_authenticated", False))
        return RequestContext(
            method=method,
            path=request.path,
            headers=dict(request.headers.items()),
            client_ip=self._get_client_ip(request),
            user=user if is_authenticated else None,
            is_authenticated=is_authenticated,
        )

    def _get_client_ip(self, request) -> str | None:
        """Extract client IP (canonical resolution: XFF -> X-Real-IP -> REMOTE_ADDR)."""
        return extract_client_ip(request)

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
