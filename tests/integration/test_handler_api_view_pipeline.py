"""Integration test: HandlerAPIView full DRF pipeline.

Tests the end-to-end flow:
  DRF dispatch → authentication → permissions → handler → adapter conversion → response

Services composed:
- HandlerAPIView (DRF APIView base)
- DjangoFrameworkAdapter (request/response conversion)
- ProviderRegistry.web_framework (adapter lookup)
- Permission resolution (PermissionLevel → DRF classes)

No real infrastructure required — mock-based integration test.
"""

from __future__ import annotations

import json

import pytest

pytest.importorskip("django")

import django

django.setup()

from django.test import RequestFactory

from baldur.interfaces.web_framework import (
    HttpMethod,
    PermissionLevel,
    RequestContext,
    ResponseContext,
)


@pytest.fixture(autouse=True)
def _bypass_auth(monkeypatch):
    """Bypass Baldur auth for integration tests."""
    monkeypatch.setenv("DISABLE_BALDUR_AUTH", "true")


class TestHandlerAPIViewPipelineIntegration:
    """Full DRF pipeline integration with HandlerAPIView."""

    def test_single_handler_get_returns_json(self):
        """GET request through full pipeline returns JSON response."""
        from baldur.api.django.base import HandlerAPIView

        def my_handler(ctx: RequestContext) -> ResponseContext:
            return ResponseContext.json({"message": "hello"})

        class TestView(HandlerAPIView):
            handler = my_handler
            permission_level = PermissionLevel.PUBLIC

        factory = RequestFactory()
        request = factory.get("/test/")
        response = TestView.as_view()(request)

        assert response.status_code == 200
        body = json.loads(response.content)
        assert body["message"] == "hello"

    def test_handler_map_routes_by_method(self):
        """handler_map dispatches different handlers per HTTP method."""
        from baldur.api.django.base import HandlerAPIView

        def get_handler(ctx):
            return ResponseContext.json({"action": "read"})

        def post_handler(ctx):
            return ResponseContext.json({"action": "create"})

        class TestView(HandlerAPIView):
            handler_map = {
                HttpMethod.GET: get_handler,
                HttpMethod.POST: post_handler,
            }
            permission_level = PermissionLevel.PUBLIC

        factory = RequestFactory()

        # GET
        get_resp = TestView.as_view()(factory.get("/test/"))
        get_body = json.loads(get_resp.content)
        assert get_body["action"] == "read"

        # POST
        post_resp = TestView.as_view()(
            factory.post("/test/", data="{}", content_type="application/json")
        )
        post_body = json.loads(post_resp.content)
        assert post_body["action"] == "create"

    def test_permission_level_viewer_blocks_unauthenticated(self, monkeypatch):
        """VIEWER permission blocks request when auth is enforced."""
        monkeypatch.setenv("DISABLE_BALDUR_AUTH", "false")

        from baldur.api.django.base import HandlerAPIView

        def my_handler(ctx):
            return ResponseContext.json({"data": "secret"})

        class ProtectedView(HandlerAPIView):
            handler = my_handler
            permission_level = PermissionLevel.VIEWER

        factory = RequestFactory()
        request = factory.get("/test/")
        response = ProtectedView.as_view()(request)

        # AnonymousUser should be denied (403)
        assert response.status_code == 403

    def test_path_params_forwarded_to_handler(self):
        """URL kwargs are available in ctx.path_params."""
        from baldur.api.django.base import HandlerAPIView

        captured = {}

        def detail_handler(ctx: RequestContext) -> ResponseContext:
            captured["name"] = ctx.path_params.get("name")
            return ResponseContext.json({"name": ctx.path_params.get("name")})

        class DetailView(HandlerAPIView):
            handler = detail_handler
            permission_level = PermissionLevel.PUBLIC

        factory = RequestFactory()
        request = factory.get("/cb/my-breaker/")
        response = DetailView.as_view()(request, name="my-breaker")

        assert response.status_code == 200
        assert captured["name"] == "my-breaker"

    def test_query_params_forwarded_to_handler(self):
        """Query parameters are available in ctx.query_params."""
        from baldur.api.django.base import HandlerAPIView

        captured = {}

        def search_handler(ctx: RequestContext) -> ResponseContext:
            captured["q"] = ctx.get_query("q")
            return ResponseContext.json({"q": ctx.get_query("q")})

        class SearchView(HandlerAPIView):
            handler = search_handler
            permission_level = PermissionLevel.PUBLIC

        factory = RequestFactory()
        request = factory.get("/search/?q=test")
        response = SearchView.as_view()(request)

        assert response.status_code == 200
        # DRF QueryDict wraps values as lists
        q_val = captured["q"]
        assert q_val == "test" or q_val == ["test"]

    def test_raw_response_returns_non_json_content(self):
        """Handler returning raw() produces non-JSON HTTP response."""
        from baldur.api.django.base import HandlerAPIView

        def metrics_handler(ctx: RequestContext) -> ResponseContext:
            return ResponseContext.raw(
                body="# HELP total\ntotal 42\n",
                content_type="text/plain; version=0.0.4",
            )

        class MetricsView(HandlerAPIView):
            handler = metrics_handler
            permission_level = PermissionLevel.PUBLIC

        factory = RequestFactory()
        response = MetricsView.as_view()(factory.get("/metrics/"))

        assert response.status_code == 200
        assert "text/plain" in response["Content-Type"]
        assert b"total 42" in response.content

    def test_error_response_returns_correct_status(self):
        """Handler returning error() produces correct HTTP status code."""
        from baldur.api.django.base import HandlerAPIView

        def fail_handler(ctx: RequestContext) -> ResponseContext:
            return ResponseContext.not_found("resource missing")

        class FailView(HandlerAPIView):
            handler = fail_handler
            permission_level = PermissionLevel.PUBLIC

        factory = RequestFactory()
        response = FailView.as_view()(factory.get("/missing/"))

        assert response.status_code == 404
        body = json.loads(response.content)
        assert body["error"] == "resource missing"

    def test_permission_map_per_method_enforcement(self, monkeypatch):
        """permission_map enforces different levels per HTTP method."""
        monkeypatch.setenv("DISABLE_BALDUR_AUTH", "false")

        from baldur.api.django.base import HandlerAPIView

        def mixed_handler(ctx):
            return ResponseContext.json({"ok": True})

        class MixedPermView(HandlerAPIView):
            handler = mixed_handler
            permission_map = {
                HttpMethod.GET: PermissionLevel.PUBLIC,
                HttpMethod.POST: PermissionLevel.ADMIN,
            }

        factory = RequestFactory()

        # GET with PUBLIC should succeed (no auth needed)
        get_response = MixedPermView.as_view()(factory.get("/test/"))
        assert get_response.status_code == 200

        # POST with ADMIN should fail for anonymous user
        post_response = MixedPermView.as_view()(
            factory.post("/test/", data="{}", content_type="application/json")
        )
        assert post_response.status_code == 403
