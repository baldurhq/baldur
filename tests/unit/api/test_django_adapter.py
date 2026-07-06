"""Unit tests for DjangoFrameworkAdapter, HandlerAPIView, and PermissionLevel mapping.

Tests covering:
- DjangoFrameworkAdapter: to_request_context, from_response_context, header extraction
- HandlerAPIView: dispatch, permission resolution, descriptor bypass
- get_permission_instances: PermissionLevel→DRF mapping

Verification techniques:
- Contract: framework_name, header normalization rules, permission mapping
- Behavior: request/response conversion, handler dispatch, add_middleware error
- Dependency interaction: adapter method calls from HandlerAPIView
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("django")

import django

django.setup()

from baldur.api.django.adapter import DjangoFrameworkAdapter
from baldur.interfaces.web_framework import (
    HttpMethod,
    PermissionLevel,
    RequestContext,
    ResponseContext,
)

# =============================================================================
# DjangoFrameworkAdapter Contract
# =============================================================================


class TestDjangoFrameworkAdapterContract:
    """DjangoFrameworkAdapter design contract verification."""

    def test_framework_name_is_django(self):
        """framework_name must return 'django'."""
        adapter = DjangoFrameworkAdapter()
        assert adapter.framework_name == "django"

    def test_implements_web_framework_interface(self):
        """DjangoFrameworkAdapter must be a WebFrameworkInterface subclass."""
        from baldur.interfaces.web_framework import WebFrameworkInterface

        assert issubclass(DjangoFrameworkAdapter, WebFrameworkInterface)

    def test_create_router_returns_empty_list(self):
        """create_router() returns empty URL pattern list."""
        adapter = DjangoFrameworkAdapter()
        router = adapter.create_router(prefix="/api/v1")
        assert router == []
        assert isinstance(router, list)

    def test_add_middleware_raises_not_implemented(self):
        """add_middleware() must raise NotImplementedError (doc §A-1.2)."""
        adapter = DjangoFrameworkAdapter()
        with pytest.raises(NotImplementedError, match="settings.MIDDLEWARE"):
            adapter.add_middleware(None, type)


# =============================================================================
# Header Extraction
# =============================================================================


class TestHeaderExtractionBehavior:
    """DjangoFrameworkAdapter._extract_headers() normalization behavior."""

    def test_http_prefix_headers_normalized_to_lowercase(self):
        """HTTP_* META keys become lowercase hyphenated headers."""
        request = MagicMock()
        request.META = {
            "HTTP_X_FORWARDED_FOR": "10.0.0.1",
            "HTTP_ACCEPT": "application/json",
        }
        headers = DjangoFrameworkAdapter._extract_headers(request)
        assert headers["x-forwarded-for"] == "10.0.0.1"
        assert headers["accept"] == "application/json"

    def test_content_type_from_meta(self):
        """CONTENT_TYPE META key is extracted as content-type."""
        request = MagicMock()
        request.META = {"CONTENT_TYPE": "application/json"}
        headers = DjangoFrameworkAdapter._extract_headers(request)
        assert headers["content-type"] == "application/json"

    def test_content_length_from_meta(self):
        """CONTENT_LENGTH META key is extracted as content-length."""
        request = MagicMock()
        request.META = {"CONTENT_LENGTH": "42"}
        headers = DjangoFrameworkAdapter._extract_headers(request)
        assert headers["content-length"] == "42"

    def test_non_http_meta_keys_excluded(self):
        """Non-HTTP_ META keys (SERVER_NAME etc.) are excluded."""
        request = MagicMock()
        request.META = {
            "SERVER_NAME": "localhost",
            "PATH_INFO": "/test/",
            "HTTP_HOST": "example.com",
        }
        headers = DjangoFrameworkAdapter._extract_headers(request)
        assert "server-name" not in headers
        assert "path-info" not in headers
        assert headers["host"] == "example.com"

    def test_empty_meta_returns_empty_headers(self):
        """Empty META dict returns empty headers."""
        request = MagicMock()
        request.META = {}
        headers = DjangoFrameworkAdapter._extract_headers(request)
        assert headers == {}


# =============================================================================
# Client IP Extraction
# =============================================================================


class TestClientIpExtractionBehavior:
    """DjangoFrameworkAdapter._extract_client_ip() behavior."""

    def test_prefers_x_forwarded_for_first_ip(self):
        """X-Forwarded-For first entry is used as client IP."""
        request = MagicMock()
        request.META = {
            "HTTP_X_FORWARDED_FOR": "10.0.0.1, 10.0.0.2",
            "REMOTE_ADDR": "172.16.0.1",
        }
        ip = DjangoFrameworkAdapter._extract_client_ip(request)
        assert ip == "10.0.0.1"

    def test_falls_back_to_x_real_ip(self):
        """Falls back to X-Real-IP when X-Forwarded-For absent (nginx convention)."""
        request = MagicMock()
        request.META = {
            "HTTP_X_REAL_IP": "10.0.0.5",
            "REMOTE_ADDR": "172.16.0.1",
        }
        ip = DjangoFrameworkAdapter._extract_client_ip(request)
        assert ip == "10.0.0.5"

    def test_falls_back_to_remote_addr(self):
        """Falls back to REMOTE_ADDR when proxy headers absent."""
        request = MagicMock()
        request.META = {"REMOTE_ADDR": "192.168.1.1"}
        ip = DjangoFrameworkAdapter._extract_client_ip(request)
        assert ip == "192.168.1.1"

    def test_returns_none_when_no_ip_available(self):
        """Returns None when neither header present."""
        request = MagicMock()
        request.META = {}
        ip = DjangoFrameworkAdapter._extract_client_ip(request)
        assert ip is None


# =============================================================================
# to_request_context
# =============================================================================


class TestToRequestContextBehavior:
    """DjangoFrameworkAdapter.to_request_context() conversion behavior."""

    def _make_drf_request(self, method="GET", path="/test/", data=None, **meta):
        """Create a mock DRF Request object."""
        request = MagicMock()
        request.method = method
        request.path = path
        request.query_params = {}
        request.data = data or {}
        request.body = b""
        request.content_type = "application/json"
        request.user = MagicMock()
        request.user.is_authenticated = True
        request.META = {"REMOTE_ADDR": "127.0.0.1", **meta}
        return request

    def test_maps_method_to_http_method_enum(self):
        """request.method maps to HttpMethod enum."""
        adapter = DjangoFrameworkAdapter()
        request = self._make_drf_request(method="POST")
        ctx = adapter.to_request_context(request)
        assert ctx.method == HttpMethod.POST

    def test_maps_path(self):
        """request.path maps to ctx.path."""
        adapter = DjangoFrameworkAdapter()
        request = self._make_drf_request(path="/api/health/")
        ctx = adapter.to_request_context(request)
        assert ctx.path == "/api/health/"

    def test_maps_json_body_from_data_dict(self):
        """DRF request.data dict maps to ctx.json_body."""
        adapter = DjangoFrameworkAdapter()
        request = self._make_drf_request(data={"key": "value"})
        ctx = adapter.to_request_context(request)
        assert ctx.json_body == {"key": "value"}

    def test_json_body_none_when_data_not_dict(self):
        """json_body is None when request.data is not a dict."""
        adapter = DjangoFrameworkAdapter()
        request = self._make_drf_request(data="raw-string")
        ctx = adapter.to_request_context(request)
        assert ctx.json_body is None

    def test_maps_is_authenticated(self):
        """request.user.is_authenticated maps to ctx.is_authenticated."""
        adapter = DjangoFrameworkAdapter()
        request = self._make_drf_request()
        request.user.is_authenticated = True
        ctx = adapter.to_request_context(request)
        assert ctx.is_authenticated is True

    def test_maps_user_agent_from_headers(self):
        """User-Agent header maps to ctx.user_agent."""
        adapter = DjangoFrameworkAdapter()
        request = self._make_drf_request(HTTP_USER_AGENT="TestBot/1.0")
        ctx = adapter.to_request_context(request)
        assert ctx.user_agent == "TestBot/1.0"


# =============================================================================
# from_response_context
# =============================================================================


class TestFromResponseContextBehavior:
    """DjangoFrameworkAdapter.from_response_context() conversion behavior."""

    def test_json_response_returns_json_response_type(self):
        """JSON content_type produces Django JsonResponse."""
        from django.http import JsonResponse

        adapter = DjangoFrameworkAdapter()
        ctx = ResponseContext.json({"status": "ok"}, status_code=200)
        resp = adapter.from_response_context(ctx)
        assert isinstance(resp, JsonResponse)
        assert resp.status_code == 200

    def test_raw_response_returns_http_response(self):
        """Non-JSON content_type produces Django HttpResponse."""
        from django.http import HttpResponse

        adapter = DjangoFrameworkAdapter()
        ctx = ResponseContext.raw(
            body="metric_total 42",
            content_type="text/plain",
        )
        resp = adapter.from_response_context(ctx)
        assert isinstance(resp, HttpResponse)
        assert resp["Content-Type"] == "text/plain"

    def test_streaming_response_returns_streaming_http_response(self):
        """Streaming response produces Django StreamingHttpResponse."""
        from django.http import StreamingHttpResponse

        adapter = DjangoFrameworkAdapter()
        ctx = ResponseContext.streaming(
            body_iterator=iter(["line1\n", "line2\n"]),
            content_type="text/csv",
        )
        resp = adapter.from_response_context(ctx)
        assert isinstance(resp, StreamingHttpResponse)

    def test_custom_headers_applied_to_response(self):
        """Custom headers from ResponseContext are set on Django response."""
        adapter = DjangoFrameworkAdapter()
        ctx = ResponseContext.json(
            {"ok": True},
            headers={"X-Request-Id": "abc123"},
        )
        resp = adapter.from_response_context(ctx)
        assert resp["X-Request-Id"] == "abc123"

    def test_error_response_status_code(self):
        """Error ResponseContext maps to correct HTTP status."""
        adapter = DjangoFrameworkAdapter()
        ctx = ResponseContext.error("not found", status_code=404)
        resp = adapter.from_response_context(ctx)
        assert resp.status_code == 404


# =============================================================================
# get_permission_instances mapping
# =============================================================================


class TestGetPermissionInstancesContract:
    """get_permission_instances() PermissionLevel→DRF mapping (doc §4.2)."""

    def test_public_returns_empty_list(self):
        """PUBLIC maps to no permission classes."""
        from baldur.api.django.permissions import get_permission_instances

        result = get_permission_instances(PermissionLevel.PUBLIC)
        assert result == []

    def test_authenticated_returns_is_baldur_authenticated(self):
        """AUTHENTICATED maps to IsBaldurAuthenticated."""
        from baldur.api.django.permissions import (
            IsBaldurAuthenticated,
            get_permission_instances,
        )

        result = get_permission_instances(PermissionLevel.AUTHENTICATED)
        assert len(result) == 1
        assert isinstance(result[0], IsBaldurAuthenticated)

    def test_viewer_returns_is_viewer(self):
        """VIEWER maps to IsViewer."""
        from baldur.api.django.permissions import (
            IsViewer,
            get_permission_instances,
        )

        result = get_permission_instances(PermissionLevel.VIEWER)
        assert len(result) == 1
        assert isinstance(result[0], IsViewer)

    def test_operator_returns_is_operator(self):
        """OPERATOR maps to IsOperator."""
        from baldur.api.django.permissions import (
            IsOperator,
            get_permission_instances,
        )

        result = get_permission_instances(PermissionLevel.OPERATOR)
        assert len(result) == 1
        assert isinstance(result[0], IsOperator)

    def test_admin_returns_is_baldur_admin(self):
        """ADMIN maps to IsBaldurAdmin."""
        from baldur.api.django.permissions import (
            IsBaldurAdmin,
            get_permission_instances,
        )

        result = get_permission_instances(PermissionLevel.ADMIN)
        assert len(result) == 1
        assert isinstance(result[0], IsBaldurAdmin)

    def test_all_five_levels_covered(self):
        """All 5 PermissionLevel values produce valid results."""
        from baldur.api.django.permissions import get_permission_instances

        for level in PermissionLevel:
            result = get_permission_instances(level)
            assert isinstance(result, list)


# =============================================================================
# HandlerAPIView — dispatch and permissions
# =============================================================================


class TestHandlerAPIViewDispatchBehavior:
    """HandlerAPIView handler dispatch and permission resolution."""

    def test_dispatch_calls_single_handler(self):
        """Single handler attribute is called for any HTTP method."""
        from baldur.api.django.base import HandlerAPIView

        called_with = {}

        def mock_handler(ctx):
            called_with["ctx"] = ctx
            return ResponseContext.json({"ok": True})

        # Given
        class TestView(HandlerAPIView):
            handler = mock_handler
            permission_level = PermissionLevel.PUBLIC

        mock_adapter = MagicMock()
        mock_adapter.to_request_context.return_value = RequestContext(
            method=HttpMethod.GET, path="/test/"
        )
        mock_adapter.from_response_context.return_value = MagicMock(status_code=200)

        # When
        view = TestView()
        view.request = MagicMock()
        view.request.method = "GET"
        with patch.object(view, "_get_adapter", return_value=mock_adapter):
            view._dispatch_handler(view.request)

        # Then
        assert "ctx" in called_with

    def test_dispatch_handler_map_takes_precedence(self):
        """handler_map entries take precedence over handler."""
        from baldur.api.django.base import HandlerAPIView

        map_called = []
        handler_called = []

        def map_handler(ctx):
            map_called.append(True)
            return ResponseContext.json({"source": "map"})

        def fallback_handler(ctx):
            handler_called.append(True)
            return ResponseContext.json({"source": "handler"})

        class TestView(HandlerAPIView):
            handler = fallback_handler
            handler_map = {HttpMethod.GET: map_handler}
            permission_level = PermissionLevel.PUBLIC

        mock_adapter = MagicMock()
        mock_adapter.to_request_context.return_value = RequestContext(
            method=HttpMethod.GET, path="/test/"
        )
        mock_adapter.from_response_context.return_value = MagicMock()

        view = TestView()
        view.request = MagicMock()
        view.request.method = "GET"
        with patch.object(view, "_get_adapter", return_value=mock_adapter):
            view._dispatch_handler(view.request)

        assert len(map_called) == 1
        assert len(handler_called) == 0

    def test_dispatch_returns_405_when_no_handler(self):
        """Returns 405 when neither handler nor handler_map is set."""
        from baldur.api.django.base import HandlerAPIView

        view = HandlerAPIView()
        view.request = MagicMock()
        view.request.method = "GET"
        resp = view._dispatch_handler(view.request)
        assert resp.status_code == 405

    def test_dispatch_passes_path_params_as_kwargs(self):
        """URL kwargs are set as ctx.path_params."""
        from baldur.api.django.base import HandlerAPIView

        captured_ctx = {}

        def capture_handler(ctx):
            captured_ctx["params"] = ctx.path_params
            return ResponseContext.json({})

        class TestView(HandlerAPIView):
            handler = capture_handler
            permission_level = PermissionLevel.PUBLIC

        mock_adapter = MagicMock()
        mock_adapter.to_request_context.return_value = RequestContext(
            method=HttpMethod.GET, path="/test/"
        )
        mock_adapter.from_response_context.return_value = MagicMock()

        view = TestView()
        view.request = MagicMock()
        view.request.method = "GET"
        with patch.object(view, "_get_adapter", return_value=mock_adapter):
            view._dispatch_handler(view.request, name="my-cb")

        assert captured_ctx["params"] == {"name": "my-cb"}

    def test_get_permissions_uses_permission_level_default(self):
        """get_permissions() uses permission_level when no permission_map."""
        from baldur.api.django.base import HandlerAPIView
        from baldur.api.django.permissions import IsViewer

        class TestView(HandlerAPIView):
            permission_level = PermissionLevel.VIEWER

        view = TestView()
        view.request = MagicMock()
        view.request.method = "GET"
        perms = view.get_permissions()
        assert len(perms) == 1
        assert isinstance(perms[0], IsViewer)

    def test_get_permissions_uses_permission_map_per_method(self):
        """get_permissions() uses permission_map for per-method resolution."""
        from baldur.api.django.base import HandlerAPIView
        from baldur.api.django.permissions import IsBaldurAdmin

        class TestView(HandlerAPIView):
            permission_map = {
                HttpMethod.GET: PermissionLevel.VIEWER,
                HttpMethod.PUT: PermissionLevel.ADMIN,
            }

        view = TestView()
        view.request = MagicMock()
        view.request.method = "PUT"
        perms = view.get_permissions()
        assert len(perms) == 1
        assert isinstance(perms[0], IsBaldurAdmin)

    def test_get_permissions_falls_back_to_level_for_unknown_method(self):
        """permission_map falls back to permission_level for unmapped methods."""
        from baldur.api.django.base import HandlerAPIView
        from baldur.api.django.permissions import (
            IsBaldurAuthenticated,
        )

        class TestView(HandlerAPIView):
            permission_level = PermissionLevel.AUTHENTICATED
            permission_map = {
                HttpMethod.GET: PermissionLevel.PUBLIC,
            }

        view = TestView()
        view.request = MagicMock()
        view.request.method = "DELETE"
        perms = view.get_permissions()
        assert len(perms) == 1
        assert isinstance(perms[0], IsBaldurAuthenticated)


# =============================================================================
# Pilot View declarations
# =============================================================================


class TestPilotViewDeclarationsContract:
    """Pilot views must declare correct handler and permission_level (doc §3)."""

    def test_liveness_view_is_public_with_handler(self):
        """LivenessView: PUBLIC + liveness_check handler."""
        from baldur.api.django.base import HandlerAPIView
        from baldur.api.django.views.health import LivenessView
        from baldur.api.handlers.health import liveness_check

        assert issubclass(LivenessView, HandlerAPIView)
        assert LivenessView.permission_level == PermissionLevel.PUBLIC
        assert LivenessView.__dict__["handler"] is liveness_check

    def test_readiness_view_is_public_with_handler(self):
        """ReadinessView: PUBLIC + readiness_check handler."""
        from baldur.api.django.views.health import ReadinessView
        from baldur.api.handlers.health import readiness_check

        assert ReadinessView.permission_level == PermissionLevel.PUBLIC
        assert ReadinessView.__dict__["handler"] is readiness_check

    def test_health_view_is_public_with_handler(self):
        """BaldurHealthView: PUBLIC + health_check handler."""
        from baldur.api.django.views.health import BaldurHealthView
        from baldur.api.handlers.health import health_check

        assert BaldurHealthView.permission_level == PermissionLevel.PUBLIC
        assert BaldurHealthView.__dict__["handler"] is health_check

    def test_bulkhead_status_view_is_public_with_handler(self):
        """BulkheadStatusView: PUBLIC + bulkhead_status handler."""
        from baldur.api.django.views.bulkhead import BulkheadStatusView
        from baldur.api.handlers.bulkhead import bulkhead_status

        assert BulkheadStatusView.permission_level == PermissionLevel.PUBLIC
        assert BulkheadStatusView.__dict__["handler"] is bulkhead_status

    def test_audit_health_view_is_viewer_with_handler(self):
        """AuditHealthView: VIEWER + audit_health handler (doc §4.5 RBAC)."""
        from baldur.api.django.views.audit_resilience import AuditHealthView
        from baldur.api.handlers.audit import audit_health

        assert AuditHealthView.permission_level == PermissionLevel.VIEWER
        assert AuditHealthView.__dict__["handler"] is audit_health

    def test_circuit_breaker_status_view_is_viewer_with_handler(self):
        """CircuitBreakerStatusView: VIEWER + circuit_breaker_status (doc §4.5)."""
        from baldur.api.django.views.audit_resilience import (
            CircuitBreakerStatusView,
        )
        from baldur.api.handlers.audit import circuit_breaker_status

        assert CircuitBreakerStatusView.permission_level == PermissionLevel.VIEWER
        assert CircuitBreakerStatusView.__dict__["handler"] is circuit_breaker_status

    def test_dashboard_summary_view_is_viewer_with_handler(self):
        """DashboardSummaryView: VIEWER + dashboard_summary handler."""
        from baldur.api.django.views.dashboard import DashboardSummaryView
        from baldur.api.handlers.dashboard import dashboard_summary

        assert DashboardSummaryView.permission_level == PermissionLevel.VIEWER
        assert DashboardSummaryView.__dict__["handler"] is dashboard_summary


# =============================================================================
# discover_web_framework_adapters
# =============================================================================


class TestDiscoverWebFrameworkAdaptersBehavior:
    """discover_web_framework_adapters() auto-registration behavior."""

    def test_registers_django_adapter(self):
        """Django adapter is discovered and registered."""
        from baldur.factory import ProviderRegistry

        # Reset to force re-discovery
        ProviderRegistry.web_framework.reset()

        from baldur.factory.adapters import discover_web_framework_adapters

        discover_web_framework_adapters()

        assert ProviderRegistry.web_framework.has_provider("django")

    def test_sets_django_as_default(self):
        """Django is set as default web framework when available."""
        from baldur.factory import ProviderRegistry

        ProviderRegistry.web_framework.reset()

        from baldur.factory.adapters import discover_web_framework_adapters

        discover_web_framework_adapters()

        assert ProviderRegistry.web_framework.get_default_name() == "django"

    def test_get_returns_django_adapter_instance(self):
        """ProviderRegistry.web_framework.get() returns DjangoFrameworkAdapter."""
        from baldur.factory import ProviderRegistry

        ProviderRegistry.web_framework.reset()

        from baldur.factory.adapters import discover_web_framework_adapters

        discover_web_framework_adapters()

        adapter = ProviderRegistry.web_framework.get()
        assert isinstance(adapter, DjangoFrameworkAdapter)
