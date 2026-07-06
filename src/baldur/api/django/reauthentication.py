"""
Reauthentication Framework for Sensitive Operations.

Provides a hook-based system for enforcing reauthentication on sensitive endpoints.
This is designed to be vendor-neutral - enterprises can implement their own
authentication logic (OAuth, SAML, JWT, etc.) via the provider interface.

Key Features:
- @requires_reauthentication decorator for views
- ReauthenticationProvider interface for custom implementations
- Configurable idle timeout and session limits
- PCI-DSS compliant design (minimum privilege principle)
"""

from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from functools import wraps
from typing import TYPE_CHECKING, Any, TypeVar, cast

import structlog

from baldur.utils.time import utc_now

if TYPE_CHECKING:
    from django.http import HttpRequest, HttpResponse

logger = structlog.get_logger()


# =============================================================================
# Configuration
# =============================================================================


@dataclass
class ReauthenticationConfig:
    """Configuration for reauthentication requirements."""

    # Maximum idle time before requiring reauthentication (minutes)
    max_idle_minutes: int = 15

    # Maximum session duration before requiring reauthentication (minutes)
    max_session_minutes: int = 60

    # Whether to require reauthentication for this endpoint
    enabled: bool = True

    # Custom message for reauthentication prompt
    message: str = "This action requires reauthentication for security purposes."

    # HTTP status code to return when reauthentication is required
    status_code: int = 403


# =============================================================================
# Provider Interface
# =============================================================================


class ReauthenticationProvider(ABC):
    """
    Abstract interface for reauthentication providers.

    Enterprises should implement this interface with their specific
    authentication system (OAuth, SAML, JWT, session-based, etc.).

    Example Implementation:
        class JWTReauthProvider(ReauthenticationProvider):
            def check_reauthentication_required(self, request, config):
                token = self._get_token(request)
                issued_at = self._decode_issued_at(token)
                idle_since = self._get_last_activity(request)

                # Check session age
                session_age = (datetime.now() - issued_at).total_seconds() / 60
                if session_age > config.max_session_minutes:
                    return True

                # Check idle time
                idle_time = (datetime.now() - idle_since).total_seconds() / 60
                if idle_time > config.max_idle_minutes:
                    return True

                return False

            def get_reauthentication_response(self, request, config):
                return JsonResponse({
                    'error': 'reauthentication_required',
                    'message': config.message,
                    'reauthentication_url': '/api/auth/reauth/'
                }, status=config.status_code)
    """

    @abstractmethod
    def check_reauthentication_required(
        self,
        request: HttpRequest,
        config: ReauthenticationConfig,
    ) -> bool:
        """
        Check if the request requires reauthentication.

        Args:
            request: The incoming HTTP request
            config: Reauthentication configuration

        Returns:
            True if reauthentication is required, False otherwise
        """
        pass

    @abstractmethod
    def get_reauthentication_response(
        self,
        request: HttpRequest,
        config: ReauthenticationConfig,
    ) -> HttpResponse:
        """
        Generate the response when reauthentication is required.

        This should return a response that instructs the client
        to reauthenticate (e.g., redirect to login, return 403 with
        reauthentication URL, etc.)

        Args:
            request: The incoming HTTP request
            config: Reauthentication configuration

        Returns:
            HttpResponse to send to the client
        """
        pass

    def on_reauthentication_required(
        self,
        request: HttpRequest,
        config: ReauthenticationConfig,
    ) -> None:
        """
        Hook called when reauthentication is required.

        Override this for custom logging, metrics, or notifications.

        Args:
            request: The incoming HTTP request
            config: Reauthentication configuration
        """
        user = getattr(request, "user", None)
        user_id = getattr(user, "id", "anonymous") if user else "anonymous"
        logger.info(
            "reauth.reauthentication_required",
            user_id=user_id,
            request_path=request.path,
        )


# =============================================================================
# Default Provider (No-op for development)
# =============================================================================


class NoOpReauthenticationProvider(ReauthenticationProvider):
    """
    No-op provider that never requires reauthentication.

    Use this for development/testing or when reauthentication
    is handled by external systems (API Gateway, etc.)
    """

    def check_reauthentication_required(
        self,
        request: HttpRequest,
        config: ReauthenticationConfig,
    ) -> bool:
        """Never requires reauthentication."""
        return False

    def get_reauthentication_response(
        self,
        request: HttpRequest,
        config: ReauthenticationConfig,
    ) -> HttpResponse:
        """Should never be called since check always returns False."""
        from django.http import JsonResponse

        return JsonResponse(
            {"error": "reauthentication_required", "message": config.message},
            status=config.status_code,
        )


# =============================================================================
# Sample Session-Based Provider
# =============================================================================


class SessionBasedReauthProvider(ReauthenticationProvider):
    """
    Sample implementation using Django sessions.

    This is a reference implementation showing how to implement
    idle timeout and session limits using Django's session framework.

    To use this provider:
        1. Configure in settings.py:
            BALDUR_REAUTH_PROVIDER = 'baldur.api.django.reauthentication.SessionBasedReauthProvider'

        2. Update last activity on each request (in middleware):
            request.session['_last_activity'] = datetime.now().isoformat()
    """

    SESSION_KEY_LAST_ACTIVITY = "_baldur_last_activity"
    SESSION_KEY_AUTH_TIME = "_baldur_auth_time"

    def check_reauthentication_required(
        self,
        request: HttpRequest,
        config: ReauthenticationConfig,
    ) -> bool:
        """Check session timestamps for idle/session timeout."""
        if not config.enabled:
            return False

        if not hasattr(request, "session"):
            # No session available, can't check
            logger.warning("reauthentication.no_session_available")
            return False

        now = utc_now()

        # Check idle timeout
        last_activity_str = request.session.get(self.SESSION_KEY_LAST_ACTIVITY)
        if last_activity_str:
            try:
                last_activity = datetime.fromisoformat(last_activity_str)
                idle_minutes = (now - last_activity).total_seconds() / 60
                if idle_minutes > config.max_idle_minutes:
                    logger.info(
                        "reauth.idle_timeout_exceeded",
                        idle_minutes=idle_minutes,
                        max_idle_minutes=config.max_idle_minutes,
                    )
                    return True
            except (ValueError, TypeError):
                pass

        # Check session age
        auth_time_str = request.session.get(self.SESSION_KEY_AUTH_TIME)
        if auth_time_str:
            try:
                auth_time = datetime.fromisoformat(auth_time_str)
                session_minutes = (now - auth_time).total_seconds() / 60
                if session_minutes > config.max_session_minutes:
                    logger.info(
                        "reauth.session_timeout_exceeded",
                        session_minutes=session_minutes,
                        max_session_minutes=config.max_session_minutes,
                    )
                    return True
            except (ValueError, TypeError):
                pass

        return False

    def get_reauthentication_response(
        self,
        request: HttpRequest,
        config: ReauthenticationConfig,
    ) -> HttpResponse:
        """Return JSON response with reauthentication requirement."""
        from django.http import JsonResponse

        return JsonResponse(
            {
                "error": "reauthentication_required",
                "message": config.message,
                "code": "REAUTH_REQUIRED",
                "reauthentication_url": "/api/auth/reauthenticate/",
            },
            status=config.status_code,
        )


# =============================================================================
# Provider Registry
# =============================================================================


_provider_instance: ReauthenticationProvider | None = None
_provider_instance_lock = threading.Lock()


def get_reauthentication_provider() -> ReauthenticationProvider:
    """
    Get the configured reauthentication provider.

    Loads from Django settings BALDUR_REAUTH_PROVIDER if configured,
    otherwise returns NoOpReauthenticationProvider.

    Returns:
        ReauthenticationProvider instance
    """
    global _provider_instance

    if _provider_instance is None:
        with _provider_instance_lock:
            if _provider_instance is None:
                try:
                    from django.conf import settings
                    from django.utils.module_loading import import_string

                    provider_path = getattr(settings, "BALDUR_REAUTH_PROVIDER", None)
                    if provider_path:
                        provider_class = import_string(provider_path)
                        _provider_instance = provider_class()
                    else:
                        _provider_instance = NoOpReauthenticationProvider()
                except Exception as e:
                    logger.warning(
                        "reauth.load_provider_using_failed",
                        error=e,
                    )
                    _provider_instance = NoOpReauthenticationProvider()
    return _provider_instance


def set_reauthentication_provider(provider: ReauthenticationProvider) -> None:
    """
    Set the reauthentication provider (for testing or programmatic config).

    Args:
        provider: ReauthenticationProvider instance
    """
    global _provider_instance
    _provider_instance = provider


# =============================================================================
# Decorator
# =============================================================================

F = TypeVar("F", bound=Callable[..., Any])


def requires_reauthentication(
    max_idle_minutes: int = 15,
    max_session_minutes: int = 60,
    enabled: bool = True,
    message: str = "This action requires reauthentication for security purposes.",
) -> Callable[[F], F]:
    """
    Decorator to require reauthentication for sensitive operations.

    This decorator is vendor-neutral - it delegates the actual
    reauthentication logic to the configured ReauthenticationProvider.

    Usage:
        @requires_reauthentication(max_idle_minutes=15, max_session_minutes=60)
        def update_config(request, ...):
            # This endpoint requires fresh authentication
            ...

        # For class-based views, use method_decorator:
        from django.utils.decorators import method_decorator

        @method_decorator(requires_reauthentication(max_idle_minutes=10), name='dispatch')
        class ConfigUpdateView(APIView):
            ...

    Args:
        max_idle_minutes: Maximum idle time before requiring reauth (default: 15)
        max_session_minutes: Maximum session age before requiring reauth (default: 60)
        enabled: Whether to enforce reauthentication (default: True)
        message: Custom message for reauthentication prompt

    Returns:
        Decorated function that checks reauthentication before execution
    """
    config = ReauthenticationConfig(
        max_idle_minutes=max_idle_minutes,
        max_session_minutes=max_session_minutes,
        enabled=enabled,
        message=message,
    )

    def decorator(func: F) -> F:
        @wraps(func)
        def wrapper(request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
            # Skip if disabled
            if not config.enabled:
                return cast("HttpResponse", func(request, *args, **kwargs))

            provider = get_reauthentication_provider()

            # Check if reauthentication is required
            try:
                if provider.check_reauthentication_required(request, config):
                    provider.on_reauthentication_required(request, config)
                    return provider.get_reauthentication_response(request, config)
            except Exception as e:
                # FAIL-SECURE: On error checking reauth, deny access
                logger.exception(
                    "reauth.error_checking_reauthentication",
                    error=e,
                )
                from django.http import JsonResponse

                return JsonResponse(
                    {
                        "error": "reauthentication_check_failed",
                        "message": "Security check failed",
                    },
                    status=403,
                )

            return cast("HttpResponse", func(request, *args, **kwargs))

        return wrapper  # type: ignore

    return decorator


# =============================================================================
# DRF Permission Class (Alternative to decorator)
# =============================================================================


class RequiresReauthenticationPermission:
    """
    DRF-compatible permission class for reauthentication.

    Use this for DRF APIViews when you prefer permission-based access control.

    Usage:
        class ConfigUpdateView(APIView):
            permission_classes = [IsAuthenticated, RequiresReauthenticationPermission]

    Configure idle/session limits in settings:
        BALDUR_REAUTH_MAX_IDLE_MINUTES = 15
        BALDUR_REAUTH_MAX_SESSION_MINUTES = 60
    """

    def has_permission(self, request: HttpRequest, view: Any) -> bool:
        """Check if reauthentication is satisfied."""
        try:
            from django.conf import settings

            config = ReauthenticationConfig(
                max_idle_minutes=getattr(
                    settings, "BALDUR_REAUTH_MAX_IDLE_MINUTES", 15
                ),
                max_session_minutes=getattr(
                    settings, "BALDUR_REAUTH_MAX_SESSION_MINUTES", 60
                ),
                enabled=getattr(settings, "BALDUR_REAUTH_ENABLED", True),
            )

            provider = get_reauthentication_provider()

            if provider.check_reauthentication_required(request, config):
                provider.on_reauthentication_required(request, config)
                return False

            return True

        except Exception as e:
            # FAIL-SECURE: On error, deny access
            logger.exception(
                "reauth.permission_check_error",
                error=e,
            )
            return False


# =============================================================================
# Exports
# =============================================================================

__all__ = [
    # Configuration
    "ReauthenticationConfig",
    # Provider Interface
    "ReauthenticationProvider",
    "NoOpReauthenticationProvider",
    "SessionBasedReauthProvider",
    # Provider Registry
    "get_reauthentication_provider",
    "set_reauthentication_provider",
    # Decorator
    "requires_reauthentication",
    # Permission Class
    "RequiresReauthenticationPermission",
]
