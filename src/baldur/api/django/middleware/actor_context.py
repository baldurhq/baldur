"""
Actor Context Middleware.

Automatically tracks "who" performs an operation on every HTTP request.

With this middleware:
1. actor_id and actor_type are filled in automatically on every AuditEntry
2. Config changes made from the admin pages record who made them
3. API calls record which user made them
4. Security audit data such as IP address and session ID is collected too

Usage in settings.py:
    MIDDLEWARE = [
        ...
        'baldur.api.django.middleware.actor_context.ActorContextMiddleware',
        ...
    ]

To disable:
    BALDUR_ACTOR_MIDDLEWARE_ENABLED = False (settings.py)
    or
    BALDUR_ACTOR_MIDDLEWARE_ENABLED=false (environment variable)

Once configured, from anywhere:
    from baldur.context import ActorContext

    actor = ActorContext.get_current()
    print(f"Current user: {actor.actor_id}")  # admin@example.com
    print(f"IP: {actor.ip_address}")  # 192.168.1.1
"""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from django.http import HttpRequest, HttpResponse

logger = structlog.get_logger()


class ActorContextMiddleware:
    """
    Django Middleware for automatic actor context tracking.

    Extracts user information from request and makes it available
    throughout the request lifecycle for audit logging.
    """

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]):
        self.get_response = get_response
        self._enabled = self._check_enabled()

        status = "enabled" if self._enabled else "DISABLED"
        logger.info(
            "actor_context_middleware.initialized",
            status=status,
        )

    def _check_enabled(self) -> bool:
        """Check whether the middleware is enabled."""
        try:
            from django.conf import settings

            return getattr(settings, "BALDUR_ACTOR_MIDDLEWARE_ENABLED", True)
        except Exception:
            # Fall back to the environment variable if settings are unreachable
            return os.getenv("BALDUR_ACTOR_MIDDLEWARE_ENABLED", "true").lower() in (
                "true",
                "1",
                "yes",
            )

    def __call__(self, request: HttpRequest) -> HttpResponse:
        # Bypass when the middleware is disabled
        if not self._enabled:
            return self.get_response(request)

        from baldur.context.actor_context import ActorContext

        # Use context manager to set actor for this request
        # Fail-Open: keep serving the request if actor setup fails (avoids 500)
        try:
            with ActorContext.set_actor_from_django_request(request):
                response = self.get_response(request)
        except Exception as e:
            logger.warning(
                "actor_context_middleware.actor_context_setup_failed",
                error=e,
            )
            response = self.get_response(request)

        return response
