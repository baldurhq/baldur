"""
DRF Throttle Adapter for Baldur System.

Bridges the gap between DRF's throttle interface and our
baldur throttle implementation.

Usage in DRF views:
    from baldur.api.django.throttle_adapter import AdaptiveDRFThrottle

    class MyView(APIView):
        throttle_classes = [AdaptiveDRFThrottle]
"""

from __future__ import annotations

import time
from typing import Any, cast

import structlog

from baldur.factory import ProviderRegistry
from baldur.interfaces.throttle import AdaptiveThrottle

# ThrottleConfig is an alias for ThrottleSettings — OSS canonical home.
from baldur.settings.throttle import ThrottleSettings as ThrottleConfig
from baldur.utils.network import extract_client_ip

logger = structlog.get_logger()


class AdaptiveDRFThrottle:
    """
    DRF-compatible Adaptive Throttle using Netflix Gradient algorithm.

    This is a bridge between DRF's throttle interface and our
    baldur adaptive throttle implementation.

    Features:
    - Netflix Gradient-based adaptive rate limiting
    - SLA-aware threshold adjustments
    - Response time (RTT) tracking
    - Automatic limit adjustment based on system load

    Usage:
        class MyView(APIView):
            throttle_classes = [AdaptiveDRFThrottle]

    Configuration via settings.py:
        BALDUR_THROTTLE = {
            "initial_limit": 100,
            "min_limit": 10,
            "max_limit": 1000,
            "sla_warning_ms": 200,
            "sla_critical_ms": 500,
        }
    """

    scope = "adaptive"

    def __init__(self):
        self._throttle: AdaptiveThrottle | None = None
        self._request_start_time: float | None = None

    @property
    def throttle(self) -> AdaptiveThrottle:
        """Get the underlying adaptive throttle (lazy initialization)."""
        if self._throttle is None:
            self._throttle = ProviderRegistry.adaptive_throttle.safe_get()
            if self._throttle is None:
                raise RuntimeError(
                    "AdaptiveDRFThrottle requires baldur_pro AdaptiveThrottle"
                )
        return self._throttle

    def _get_config(self) -> ThrottleConfig:
        """Get throttle config from Django settings."""
        try:
            from django.conf import settings

            config_dict = getattr(settings, "BALDUR_THROTTLE", {})
            return ThrottleConfig.from_dict(config_dict)
        except ImportError:
            return ThrottleConfig()

    def allow_request(self, request: Any, view: Any) -> bool:
        """
        Check if the request should be allowed.

        This is called by DRF for each request.
        """
        self._request_start_time = time.time()

        # Get identifier for rate limiting
        ident = self.get_ident(request)

        # Check if allowed
        result = self.throttle.allow_request(ident)

        if not result.allowed:
            logger.info(
                "adaptive_drf_throttle.request_throttled",
                result=result.allowed,
                throttle_limit=result.limit,
                current_count=result.current_count,
            )

        return bool(result.allowed)

    def get_ident(self, request: Any) -> str:
        """
        Get unique identifier for the request.

        Uses X-Forwarded-For, X-Real-IP, or REMOTE_ADDR.
        """
        return cast(str, extract_client_ip(request, default="unknown"))

    def wait(self) -> float | None:
        """
        Return the recommended wait time before next request.

        Called by DRF when request is throttled.
        """
        # Calculate based on current window reset time
        result = self.throttle.get_status("")
        if result.reset_at:
            wait_time = float(result.reset_at) - time.time()
            return max(0.0, wait_time)
        return 1.0  # Default wait

    def finalize_response(self, request: Any, response: Any) -> None:
        """
        Called after request is completed to record RTT.

        This should be called from a DRF middleware or manually.
        """
        if self._request_start_time is not None:
            rtt_ms = (time.time() - self._request_start_time) * 1000
            self.throttle.record_response(rtt_ms)
            self._request_start_time = None


class CorruptionShieldDRFValidator:
    """
    DRF-compatible Corruption Shield validator.

    Can be used as a DRF permission class or called manually in views.

    Usage as permission:
        class MyView(APIView):
            permission_classes = [CorruptionShieldDRFValidator]

    Usage in serializer:
        class MySerializer(serializers.Serializer):
            def validate(self, data):
                validator = CorruptionShieldDRFValidator()
                validator.validate_data(data, self.context)
                return data
    """

    def __init__(self):
        self._shield = None

    @property
    def shield(self):
        """Get corruption shield instance (lazy).

        OSS install without baldur_pro returns ``None``; callers using
        ``has_permission`` / ``validate_data`` must None-guard before
        invoking ``self.shield.validate(...)`` or be deployed alongside
        the PRO bundle that registers the shield.
        """
        if self._shield is None:
            try:
                from baldur_pro.services.corruption_shield.shield import (
                    get_corruption_shield,
                )
            except ImportError:
                return None

            self._shield = get_corruption_shield()
        return self._shield

    def has_permission(self, request: Any, view: Any) -> bool:
        """
        DRF permission check.

        Validates request data through corruption shield.
        """
        data = self._extract_data(request)
        context = self._build_context(request, view)

        result = self.shield.validate(data, context)

        if not result.is_valid:
            # Store violations for error response
            request._corruption_violations = result.violations

        return not result.blocked

    def validate_data(
        self,
        data: dict,
        context: dict | None = None,
    ) -> None:
        """
        Validate data and raise exception on critical violations.

        For use in serializers or views.
        """
        from rest_framework import serializers

        result = self.shield.validate(data, context)

        if result.blocked:
            errors: dict[str, list[str]] = {}
            for v in result.violations:
                field = v.field or "non_field_errors"
                if field not in errors:
                    errors[field] = []
                errors[field].append(f"[{v.layer}] {v.message}")

            raise serializers.ValidationError(errors)

    def _extract_data(self, request: Any) -> dict:
        """Extract data from request for validation."""
        data = {}

        # From request body
        if hasattr(request, "data") and isinstance(request.data, dict):
            data.update(request.data)

        # From query params (selected fields)
        if hasattr(request, "query_params"):
            for key in ["amount", "order_id", "status"]:
                if key in request.query_params:
                    data[key] = request.query_params[key]

        return data

    def _build_context(self, request: Any, view: Any) -> dict:
        """Build validation context from request."""
        context = {}

        # Add user info if authenticated
        if hasattr(request, "user") and request.user.is_authenticated:
            context["user_id"] = request.user.id

        # Add view-specific context
        if hasattr(view, "get_corruption_shield_context"):
            context.update(view.get_corruption_shield_context(request))

        return context


# =============================================================================
# Helper functions for integration
# =============================================================================


def record_response_time(request: Any) -> None:
    """
    Record response time for adaptive throttling.

    Call this from DRF middleware after response is generated.
    """
    if hasattr(request, "_throttle_start_time"):
        rtt_ms = (time.time() - request._throttle_start_time) * 1000
        if (throttle := ProviderRegistry.adaptive_throttle.safe_get()) is not None:
            throttle.record_response(rtt_ms)


def mark_request_start(request: Any) -> None:
    """
    Mark request start time for RTT calculation.

    Call this from DRF middleware before view processing.
    """
    request._throttle_start_time = time.time()
