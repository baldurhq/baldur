"""
HandlerAPIView — DRF APIView base that dispatches to framework-agnostic handlers.

Wraps framework-independent handler functions (RequestContext -> ResponseContext)
while preserving the full DRF dispatch() pipeline: authentication, permissions,
throttling, content negotiation, and exception handling.

View declarations use only framework-independent types (HandlerFunc, HttpMethod,
PermissionLevel) — no DRF imports needed in subclasses.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

import structlog
from rest_framework.response import Response
from rest_framework.views import APIView

from baldur.api.django.permissions import get_permission_instances
from baldur.interfaces.web_framework import (
    HandlerFunc,
    HttpMethod,
    PermissionLevel,
)

if TYPE_CHECKING:
    from rest_framework.request import Request

logger = structlog.get_logger()

__all__ = ["HandlerAPIView"]


class HandlerAPIView(APIView):
    """DRF APIView base that dispatches to framework-agnostic handlers.

    Subclasses declare handlers and permissions using framework-independent
    types only. DRF pipeline (auth, permissions, throttle, exception handling)
    is 100% preserved via APIView inheritance.

    Attributes:
        handler: Single handler for all HTTP methods.
        handler_map: Per-method handlers (takes precedence over handler).
        permission_level: Default permission for all methods.
        permission_map: Per-method permissions (takes precedence over
            permission_level).

    Example (single handler)::

        class LivenessView(HandlerAPIView):
            permission_level = PermissionLevel.PUBLIC
            handler = liveness_check

    Example (multi-method)::

        class ConfigView(HandlerAPIView):
            handler_map = {
                HttpMethod.GET: read_config,
                HttpMethod.PUT: update_config,
            }
            permission_map = {
                HttpMethod.GET: PermissionLevel.VIEWER,
                HttpMethod.PUT: PermissionLevel.ADMIN,
            }
    """

    handler: ClassVar[HandlerFunc | None] = None
    handler_map: ClassVar[dict[HttpMethod, HandlerFunc] | None] = None
    permission_level: ClassVar[PermissionLevel] = PermissionLevel.AUTHENTICATED
    permission_map: ClassVar[dict[HttpMethod, PermissionLevel] | None] = None

    def get_permissions(self):
        """Convert permission_map/permission_level to DRF permission instances."""
        if self.permission_map:
            try:
                method = HttpMethod(self.request.method)
            except ValueError:
                method = None
            level = (
                self.permission_map.get(method, self.permission_level)
                if method
                else self.permission_level
            )
        else:
            level = self.permission_level
        return get_permission_instances(level)

    def _get_adapter(self):
        """Get DjangoFrameworkAdapter from ProviderRegistry (lazy)."""
        from baldur.factory import ProviderRegistry

        return ProviderRegistry.web_framework.get()

    def _dispatch_handler(self, request: Request, *args, **kwargs) -> Response:
        """Resolve handler, convert request/response via adapter."""
        method = HttpMethod(request.method)

        # handler_map takes precedence over handler
        # Access via type(self) to bypass descriptor protocol — class-level
        # function attributes would otherwise become bound methods via self.
        cls = type(self)
        handler_map = cls.__dict__.get("handler_map") or getattr(
            cls, "handler_map", None
        )
        handler = cls.__dict__.get("handler") or getattr(cls, "handler", None)

        if handler_map and method in handler_map:
            func = handler_map[method]
        elif handler:
            func = handler
        else:
            return Response({"error": "Method not allowed"}, status=405)

        adapter = self._get_adapter()
        ctx = adapter.to_request_context(request)
        ctx.path_params = kwargs
        response_ctx = func(ctx)
        return adapter.from_response_context(response_ctx)

    def get(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        return self._dispatch_handler(request, *args, **kwargs)

    def post(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        return self._dispatch_handler(request, *args, **kwargs)

    def put(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        return self._dispatch_handler(request, *args, **kwargs)

    def patch(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        return self._dispatch_handler(request, *args, **kwargs)

    def delete(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        return self._dispatch_handler(request, *args, **kwargs)
