"""
Django Framework Adapter — WebFrameworkInterface implementation for Django/DRF.

Converts between Django/DRF request/response objects and the
framework-independent RequestContext/ResponseContext DTOs.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast

import structlog
from django.http import (
    HttpResponse,
    HttpResponseBase,
    JsonResponse,
    StreamingHttpResponse,
)

from baldur.interfaces.web_framework import (
    ContentType,
    HandlerFunc,
    HttpMethod,
    PermissionLevel,
    RequestContext,
    ResponseContext,
    WebFrameworkInterface,
)

logger = structlog.get_logger()

__all__ = ["DjangoFrameworkAdapter"]


class DjangoFrameworkAdapter(WebFrameworkInterface):
    """WebFrameworkInterface implementation for Django REST Framework.

    Handles conversion between DRF Request/Response objects and the
    framework-agnostic RequestContext/ResponseContext dataclasses.
    """

    @property
    def framework_name(self) -> str:
        return "django"

    # =========================================================================
    # Routing
    # =========================================================================

    def create_router(
        self,
        prefix: str = "",
        tags: list[str] | None = None,
    ) -> Any:
        """Create a Django URL pattern list."""
        return []

    def add_route(
        self,
        router: Any,
        path: str,
        method: HttpMethod,
        handler: HandlerFunc,
        permission_level: PermissionLevel = PermissionLevel.AUTHENTICATED,
        custom_permissions: list[str] | None = None,
        response_model: type | None = None,
        summary: str | None = None,
        description: str | None = None,
        deprecated: bool = False,
    ) -> None:
        """Add a route to Django URL patterns.

        In the HandlerAPIView pattern, routing is handled by urls.py
        directly. This method exists for programmatic route registration.
        """
        from django.urls import path as django_path

        wrapped = self.wrap_handler(handler)
        route_path = path.lstrip("/")
        router.append(django_path(route_path, wrapped))

    def include_router(
        self,
        parent: Any,
        child: Any,
        prefix: str = "",
    ) -> None:
        """Include child URL patterns in parent."""
        from django.urls import include, path

        parent.append(path(prefix, include(child)))

    # =========================================================================
    # Request/Response Conversion
    # =========================================================================

    def to_request_context(self, request: Any) -> RequestContext:
        """Convert DRF Request to RequestContext.

        The request parameter is always a DRF Request object when called
        from HandlerAPIView, since APIView.dispatch() runs
        initialize_request() first.
        """
        headers = self._extract_headers(request)
        client_ip = self._extract_client_ip(request)

        # DRF Request wraps Django HttpRequest — request.data is already parsed
        json_body = None
        if hasattr(request, "data") and isinstance(request.data, dict):
            json_body = request.data

        return RequestContext(
            method=HttpMethod(request.method),
            path=request.path,
            headers=headers,
            query_params=self._extract_query_params(request),
            body=self._safe_get_body(request),
            json_body=json_body,
            user=getattr(request, "user", None),
            is_authenticated=getattr(
                getattr(request, "user", None), "is_authenticated", False
            ),
            client_ip=client_ip,
            user_agent=headers.get("user-agent"),
            request_id=self._get_request_id(),
            content_type=getattr(request, "content_type", None),
        )

    def from_response_context(self, response: ResponseContext) -> HttpResponse:
        """Convert ResponseContext to Django HttpResponse."""
        # ``HttpResponseBase`` is the common parent of ``HttpResponse`` and
        # ``StreamingHttpResponse``; ``__setitem__`` (header set) is defined
        # on the base, so the for-loop type-checks regardless of branch.
        http_resp: HttpResponseBase
        if response.is_streaming:
            http_resp = StreamingHttpResponse(
                response.body,
                content_type=response.content_type,
                status=response.status_code,
            )
        elif response.content_type == ContentType.JSON.value:
            http_resp = JsonResponse(
                response.body,
                status=response.status_code,
                safe=False,
            )
        else:
            http_resp = HttpResponse(
                response.body,
                content_type=response.content_type,
                status=response.status_code,
            )

        for key, value in response.headers.items():
            http_resp[key] = value

        return cast(HttpResponse, http_resp)

    @staticmethod
    def _extract_query_params(request: Any) -> dict:
        """Extract query params as {str: str} from DRF or Django request.

        QueryDict.dict() flattens multi-value lists to last-value strings,
        which is the correct behavior for handler query parameter access.
        Plain dicts (e.g. in tests) are returned as-is.
        """
        source = getattr(request, "query_params", None) or getattr(request, "GET", {})
        if hasattr(source, "dict"):
            return dict(source.dict())
        return dict(source)

    # =========================================================================
    # Middleware
    # =========================================================================

    def add_middleware(
        self,
        app: Any,
        middleware_class: type,
        **options,
    ) -> None:
        """Django middleware must be declared in settings.MIDDLEWARE.

        Runtime modification of settings.MIDDLEWARE has no effect because
        Django caches the middleware chain at startup via
        BaseHandler.load_middleware().
        """
        raise NotImplementedError(
            "Django middleware must be declared in settings.MIDDLEWARE. "
            "Use AppConfig.ready() for setup-time registration."
        )

    # =========================================================================
    # Authentication
    # =========================================================================

    def get_current_user(self, request: Any) -> Any | None:
        """Get authenticated user from Django request."""
        user = getattr(request, "user", None)
        if user is not None and getattr(user, "is_authenticated", False):
            return user
        return None

    def require_auth(self) -> Callable:
        """Return DRF IsBaldurAuthenticated permission class."""
        from baldur.api.django.permissions import IsBaldurAuthenticated

        return IsBaldurAuthenticated

    def require_permissions(self, permissions: list[str]) -> Callable:
        """Map permission level strings to DRF permission classes.

        Return value is the list of permission classes (each is a callable
        type); typed as ``Callable`` to match the abstract framework interface
        signature even though the runtime value is a list. DRF's permission
        machinery accepts both list and single callable.
        """
        from baldur.api.django.permissions import (
            IsBaldurAdmin,
            IsOperator,
            IsViewer,
        )

        _mapping: dict[str, type] = {
            "viewer": IsViewer,
            "operator": IsOperator,
            "admin": IsBaldurAdmin,
        }
        classes: list[type | Callable] = []
        for perm in permissions:
            cls = _mapping.get(perm.lower())
            if cls:
                classes.append(cls)
        result: list[type | Callable] = classes if classes else [self.require_auth()]
        return cast(Callable, result)

    # =========================================================================
    # Internal helpers
    # =========================================================================

    @staticmethod
    def _safe_get_body(request: Any) -> bytes | None:
        """Safely get raw body bytes, handling DRF's RawPostDataException."""
        try:
            body = request.body
            return bytes(body) if body is not None else None
        except Exception:
            return None

    @staticmethod
    def _extract_headers(request: Any) -> dict[str, str]:
        """Extract and normalize HTTP headers from Django META dict.

        Converts HTTP_* META keys to lowercase header names:
        HTTP_X_FORWARDED_FOR -> x-forwarded-for
        """
        headers: dict[str, str] = {}
        meta = getattr(request, "META", {})
        for key, value in meta.items():
            if key.startswith("HTTP_"):
                header_name = key[5:].lower().replace("_", "-")
                headers[header_name] = value
            elif key == "CONTENT_TYPE":
                headers["content-type"] = value
            elif key == "CONTENT_LENGTH":
                headers["content-length"] = value
        return headers

    @staticmethod
    def _extract_client_ip(request: Any) -> str | None:
        """Extract client IP via canonical utility.

        Delegates to ``utils.network.extract_client_ip`` to ensure
        consistent header resolution (X-Forwarded-For → X-Real-IP →
        REMOTE_ADDR) across all subsystems.
        """
        from baldur.utils.network import extract_client_ip

        return extract_client_ip(request)

    @staticmethod
    def _get_request_id() -> str | None:
        """Get request/trace ID from audit ContextVar if available."""
        try:
            from baldur.audit.trace import get_trace_id

            return get_trace_id()
        except ImportError:
            return None
