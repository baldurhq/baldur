"""
Backpressure Middleware for Django.

Returns a 503 response under overload and adds custom headers.
Integrates RateController and GracefulDegradation.

Header contract:
- X-Baldur-Backpressure-Level: current Backpressure level
- X-Baldur-Degraded-Features: list of disabled features (comma-separated)
- Retry-After: recommended retry delay (seconds)
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast

import structlog
from django.http import HttpRequest, HttpResponse

from baldur.settings.backpressure import get_backpressure_settings

try:
    from baldur.scaling.graceful_degradation import get_graceful_degradation
    from baldur.scaling.rate_controller import get_rate_controller

    _SCALING_AVAILABLE = True
except ImportError:
    _SCALING_AVAILABLE = False

logger = structlog.get_logger()


class BackpressureMiddleware:
    """
    Backpressure middleware.

    Features:
    - Returns a 503 response under overload
    - Custom message support (localization/branding)
    - Passes the list of disabled features via a header
    - Provides a Retry-After header

    Configuration (environment variables):
        BALDUR_BACKPRESSURE_ENABLED=true
        BALDUR_BACKPRESSURE_REJECT_MESSAGE="..."
        BALDUR_BACKPRESSURE_REJECT_RETRY_AFTER_SECONDS=5

    Usage (settings.py):
        MIDDLEWARE = [
            ...
            'baldur.api.django.middleware.backpressure.BackpressureMiddleware',
            ...
        ]
    """

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]):
        """
        Args:
            get_response: callable invoking the next middleware/view
        """
        self.get_response = get_response
        if _SCALING_AVAILABLE:
            self._controller = get_rate_controller()
            self._degradation = get_graceful_degradation()

    def __call__(self, request: HttpRequest) -> HttpResponse:
        """
        Handle the request.

        Returns 503 under overload; otherwise adds headers to the response.
        """
        if not _SCALING_AVAILABLE:
            return cast(HttpResponse, self.get_response(request))

        settings = get_backpressure_settings()
        if not settings.backpressure_enabled:
            return cast(HttpResponse, self.get_response(request))

        # Rate check
        if not self._controller.should_process():
            return self._create_overload_response()

        # Normal processing
        response: HttpResponse = self.get_response(request)

        # Add the disabled-features header
        disabled_features = self._degradation.get_disabled_features()
        if disabled_features:
            response["X-Baldur-Degraded-Features"] = ",".join(disabled_features)

        # Add the current-level header
        current_level = self._controller.get_state().level
        response["X-Baldur-Backpressure-Level"] = current_level.value

        return response

    def _create_overload_response(self) -> HttpResponse:
        """Build the 503 overload response."""
        settings = get_backpressure_settings()
        current_level = self._controller.get_state().level

        logger.warning(
            "backpressure_middleware.request_rejected",
            current_level=current_level.value,
        )

        return HttpResponse(
            content=settings.reject_message,
            status=503,
            content_type="text/plain; charset=utf-8",
            headers={
                "Retry-After": str(settings.reject_retry_after_seconds),
                "X-Baldur-Backpressure-Level": current_level.value,
            },
        )


class AsyncBackpressureMiddleware:
    """
    Asynchronous Backpressure middleware.

    For use in ASGI environments.

    Usage (settings.py):
        MIDDLEWARE = [
            ...
            'baldur.api.django.middleware.backpressure.AsyncBackpressureMiddleware',
            ...
        ]
    """

    async_capable = True
    sync_capable = False

    def __init__(self, get_response: Callable[[HttpRequest], Any]):
        """
        Args:
            get_response: callable invoking the next middleware/view (async)
        """
        self.get_response = get_response
        if _SCALING_AVAILABLE:
            self._controller = get_rate_controller()
            self._degradation = get_graceful_degradation()

    async def __call__(self, request: HttpRequest) -> HttpResponse:
        """Handle the request (async)."""
        if not _SCALING_AVAILABLE:
            return cast(HttpResponse, await self.get_response(request))

        settings = get_backpressure_settings()
        if not settings.backpressure_enabled:
            return cast(HttpResponse, await self.get_response(request))

        # Rate check (a sync call, but fast)
        if not self._controller.should_process():
            return self._create_overload_response()

        # Normal processing
        response: HttpResponse = await self.get_response(request)

        # Add the disabled-features header
        disabled_features = self._degradation.get_disabled_features()
        if disabled_features:
            response["X-Baldur-Degraded-Features"] = ",".join(disabled_features)

        # Add the current-level header
        current_level = self._controller.get_state().level
        response["X-Baldur-Backpressure-Level"] = current_level.value

        return response

    def _create_overload_response(self) -> HttpResponse:
        """Build the 503 overload response."""
        settings = get_backpressure_settings()
        current_level = self._controller.get_state().level

        logger.warning(
            "async_backpressure_middleware.request_rejected",
            current_level=current_level.value,
        )

        return HttpResponse(
            content=settings.reject_message,
            status=503,
            content_type="text/plain; charset=utf-8",
            headers={
                "Retry-After": str(settings.reject_retry_after_seconds),
                "X-Baldur-Backpressure-Level": current_level.value,
            },
        )
