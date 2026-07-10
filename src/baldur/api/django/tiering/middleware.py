"""
Tiering Middleware.

Django Middleware for Emergency Mode Traffic Control.
Controls traffic based on API tier during emergency mode.
"""

from __future__ import annotations

import random

import structlog

from baldur.scaling.tiering.defaults import BACKPRESSURE_TIER_RULES
from baldur.scaling.tiering.registry import get_tier_registry
from baldur.settings.backpressure import BackpressureLevel
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

        # Optional: Disable middleware
        BALDUR_TIERING_MIDDLEWARE_ENABLED = True

    Reference:
    - docs/baldur/16_GOVERNANCE_IMPLEMENTATION_PART1A.md (Section 3)
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
        self._registry = get_tier_registry()
        self._random = random.Random()

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

        Most Restrictive Wins merge strategy that applies the lower of the
        per-tier multipliers from Emergency Mode and Backpressure Level.

        Args:
            request: Django HttpRequest

        Returns:
            HttpResponse
        """
        if not self._enabled:
            return self.get_response(request)

        # CORS Preflight Bypass — OPTIONS is excluded from Load Shedding
        if request.method == "OPTIONS":
            return self.get_response(request)

        try:
            from baldur.factory.registry import ProviderRegistry
            from baldur.scaling.rate_controller import get_rate_controller

            try:
                from baldur_pro.services.emergency_mode.enums import (
                    EMERGENCY_LEVEL_RULES,
                    EmergencyLevel,
                )
            except ImportError:
                EMERGENCY_LEVEL_RULES = None  # type: ignore[assignment,misc]
                EmergencyLevel = None  # type: ignore[assignment,misc]

            manager = ProviderRegistry.emergency_manager.safe_get()
            if manager is None:
                raise RuntimeError("baldur_pro EmergencyManager not registered")
            controller = get_rate_controller()

            emergency_active = manager.is_active()
            emergency_level = (
                manager.get_current_level()
                if emergency_active
                else EmergencyLevel.NORMAL
            )
            bp_level = controller.get_state().level

            # Pass if both Emergency and Backpressure are normal
            if (
                emergency_level == EmergencyLevel.NORMAL
                and bp_level == BackpressureLevel.NONE
            ):
                return self.get_response(request)

            path = request.path
            client_ip = self._get_client_ip(request)
            user_id = self._get_user_id(request)
            method = request.method

            tier_result = self._registry.resolve_tier_with_fallback(
                path=path,
                client_ip=client_ip,
                user_id=str(user_id) if user_id else None,
                method=method,
            )

            # Most Restrictive Wins: apply the lower multiplier of the two rules
            emergency_multiplier = EMERGENCY_LEVEL_RULES.get(
                emergency_level,
                {},
            ).get(tier_result.tier_id, 1.0)

            backpressure_multiplier = BACKPRESSURE_TIER_RULES.get(
                bp_level,
                {},
            ).get(tier_result.tier_id, 1.0)

            final_multiplier = min(emergency_multiplier, backpressure_multiplier)

            if not self._should_allow_request(final_multiplier):
                return self._create_load_shedding_response(
                    request=request,
                    tier_id=tier_result.tier_id,
                    multiplier=final_multiplier,
                    emergency_level=emergency_level,
                )

            return self.get_response(request)

        except Exception as e:
            logger.exception(
                "tiering_middleware.error_allowing_request",
                error=e,
            )
            return self.get_response(request)

    def _get_client_ip(self, request) -> str | None:
        """Extract client IP (canonical resolution: XFF -> X-Real-IP -> REMOTE_ADDR)."""
        return extract_client_ip(request)

    def _get_user_id(self, request) -> int | None:
        """Extract user ID from request."""
        if hasattr(request, "user") and request.user.is_authenticated:
            return int(request.user.id)
        return None

    def _should_allow_request(self, multiplier: float) -> bool:
        """
        Determine if request should be allowed based on multiplier.

        Args:
            multiplier: Traffic multiplier (0.0 = block all, 1.0 = allow all)

        Returns:
            True if request should be allowed
        """
        if multiplier >= 1.0:
            return True
        if multiplier <= 0.0:
            return False

        return bool(self._random.random() < multiplier)

    def _create_load_shedding_response(
        self,
        request,
        tier_id: str,
        multiplier: float,
        emergency_level,
    ):
        """
        Create a 503 Load Shedding response.
        """
        from django.http import JsonResponse

        logger.warning(
            "tiering_middleware.load_shedding",
            request_path=request.path,
            tier_id=tier_id,
            multiplier=multiplier,
            emergency_level=emergency_level.value,
        )

        self._record_load_shedding_metrics(tier_id, emergency_level)

        response = JsonResponse(
            {
                "error": "Service Temporarily Unavailable",
                "code": "LOAD_SHEDDING",
                "message": (
                    "The request was temporarily throttled for system load management. "
                    "Please try again shortly."
                ),
                "tier": tier_id,
                "emergency_level": emergency_level.value,
                "retry_after": 30,
            },
            status=503,
        )
        response["Retry-After"] = "30"

        return response

    def _record_load_shedding_metrics(self, tier_id: str, emergency_level):
        """Record load shedding metrics to Prometheus."""
        try:
            from prometheus_client import Counter

            counter = Counter(
                "baldur_tiering_load_shedding_total",
                "Total load shedding events by tier and level",
                ["tier_id", "emergency_level"],
                registry=None,
            )
            counter.labels(
                tier_id=tier_id,
                emergency_level=emergency_level.value,
            ).inc()
        except Exception:
            pass  # Best-effort metrics
