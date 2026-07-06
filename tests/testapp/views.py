"""Dummy views for middleware integration tests (§7.3).

Each view targets a specific middleware behavior:
- SuccessView:         Normal path — full middleware chain traversal
- ErrorView:           500 raise — CB failure detection, HealthBridge
- SlowView:            Delayed 200 — timeout, pool exhaustion
- RateLimitTestView:   429 response — HybridRateLimitMiddleware L1/L2
- TieredEndpointView:  200 with header branching — TieringMiddleware API tier
"""

import time

from django.http import JsonResponse
from rest_framework.views import APIView

DEFAULT_SLOW_VIEW_DELAY = 2.0


class SuccessView(APIView):
    """Returns 200 OK. Verifies normal middleware chain."""

    authentication_classes = []
    permission_classes = []

    def get(self, request):
        return JsonResponse({"status": "ok"})


class ErrorView(APIView):
    """Raises an unhandled exception to trigger 500.

    Verifies: BaldurMiddleware failure detection,
    CB record_failure(), DLQ auto-storage.
    """

    authentication_classes = []
    permission_classes = []

    def get(self, request):
        raise RuntimeError("Deliberate 500 for middleware testing")


class SlowView(APIView):
    """Delays response by `delay` query param (default 2s).

    Verifies: timeout detection, pool exhaustion scenarios.
    """

    authentication_classes = []
    permission_classes = []

    def get(self, request):
        delay = float(request.GET.get("delay", str(DEFAULT_SLOW_VIEW_DELAY)))
        time.sleep(delay)
        return JsonResponse({"status": "ok", "delay": delay})


class RateLimitTestView(APIView):
    """Returns 429 Too Many Requests.

    Verifies: HybridRateLimitMiddleware L1/L2 transition logic.
    """

    authentication_classes = []
    permission_classes = []

    def get(self, request):
        return JsonResponse(
            {"detail": "Rate limit exceeded"},
            status=429,
        )


class TieredEndpointView(APIView):
    """Returns 200 with tier info derived from request headers.

    Verifies: TieringMiddleware API tier classification.
    Expected header: X-API-Tier (e.g., "free", "pro", "enterprise").
    """

    authentication_classes = []
    permission_classes = []

    def get(self, request):
        tier = request.META.get("HTTP_X_API_TIER", "free")
        return JsonResponse({"status": "ok", "tier": tier})
