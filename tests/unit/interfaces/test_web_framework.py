"""Unit tests for interfaces/web_framework.py — 370 additions.

Tests PermissionLevel enum, ResponseContext streaming/raw/service_unavailable
factory methods, and add_route() signature change.

Verification techniques:
- Contract: enum values, factory output structure, field defaults
- Behavior: factory method output correctness
"""

from __future__ import annotations

from baldur.interfaces.web_framework import (
    PermissionLevel,
    ResponseContext,
    WebFrameworkInterface,
)

# =============================================================================
# PermissionLevel Enum
# =============================================================================


class TestPermissionLevelContract:
    """PermissionLevel enum design contract values (doc §4.2)."""

    def test_permission_level_has_five_values(self):
        """PermissionLevel must have exactly 5 members."""
        assert len(PermissionLevel) == 5

    def test_permission_level_public_value(self):
        """PUBLIC level maps to 'public'."""
        assert PermissionLevel.PUBLIC == "public"
        assert PermissionLevel.PUBLIC.value == "public"

    def test_permission_level_authenticated_value(self):
        """AUTHENTICATED level maps to 'authenticated'."""
        assert PermissionLevel.AUTHENTICATED == "authenticated"

    def test_permission_level_viewer_value(self):
        """VIEWER level maps to 'viewer'."""
        assert PermissionLevel.VIEWER == "viewer"

    def test_permission_level_operator_value(self):
        """OPERATOR level maps to 'operator'."""
        assert PermissionLevel.OPERATOR == "operator"

    def test_permission_level_admin_value(self):
        """ADMIN level maps to 'admin'."""
        assert PermissionLevel.ADMIN == "admin"

    def test_permission_level_is_str_enum(self):
        """PermissionLevel inherits from str for JSON serialization."""
        assert isinstance(PermissionLevel.PUBLIC, str)


# =============================================================================
# ResponseContext — is_streaming field
# =============================================================================


class TestResponseContextStreamingContract:
    """ResponseContext is_streaming field design contract."""

    def test_is_streaming_defaults_to_false(self):
        """is_streaming must default to False."""
        ctx = ResponseContext()
        assert ctx.is_streaming is False

    def test_is_streaming_can_be_set_true(self):
        """is_streaming can be explicitly set to True."""
        ctx = ResponseContext(is_streaming=True)
        assert ctx.is_streaming is True


# =============================================================================
# ResponseContext.streaming() factory
# =============================================================================


class TestResponseContextStreamingFactoryBehavior:
    """ResponseContext.streaming() factory behavior."""

    def test_streaming_sets_is_streaming_true(self):
        """streaming() must set is_streaming=True."""
        ctx = ResponseContext.streaming(
            body_iterator=iter(["chunk1", "chunk2"]),
            content_type="application/x-ndjson",
        )
        assert ctx.is_streaming is True

    def test_streaming_sets_content_type(self):
        """streaming() must set the provided content_type."""
        ct = "text/csv"
        ctx = ResponseContext.streaming(
            body_iterator=iter([]),
            content_type=ct,
        )
        assert ctx.content_type == ct

    def test_streaming_with_filename_sets_content_disposition(self):
        """streaming() with filename must set Content-Disposition header."""
        ctx = ResponseContext.streaming(
            body_iterator=iter([]),
            content_type="application/x-ndjson",
            filename="export.jsonl",
        )
        assert "Content-Disposition" in ctx.headers
        assert 'filename="export.jsonl"' in ctx.headers["Content-Disposition"]

    def test_streaming_without_filename_no_content_disposition(self):
        """streaming() without filename must not set Content-Disposition."""
        ctx = ResponseContext.streaming(
            body_iterator=iter([]),
            content_type="text/plain",
        )
        assert "Content-Disposition" not in ctx.headers

    def test_streaming_custom_status_code(self):
        """streaming() respects custom status_code."""
        ctx = ResponseContext.streaming(
            body_iterator=iter([]),
            content_type="text/plain",
            status_code=206,
        )
        assert ctx.status_code == 206

    def test_streaming_preserves_extra_headers(self):
        """streaming() merges additional headers."""
        ctx = ResponseContext.streaming(
            body_iterator=iter([]),
            content_type="text/plain",
            headers={"X-Custom": "value"},
        )
        assert ctx.headers["X-Custom"] == "value"

    def test_streaming_default_status_code_is_200(self):
        """streaming() defaults to status 200."""
        ctx = ResponseContext.streaming(
            body_iterator=iter([]),
            content_type="text/plain",
        )
        assert ctx.status_code == 200


# =============================================================================
# ResponseContext.raw() factory
# =============================================================================


class TestResponseContextRawFactoryBehavior:
    """ResponseContext.raw() factory behavior."""

    def test_raw_sets_body_and_content_type(self):
        """raw() must set body and content_type correctly."""
        body = b"# HELP metric_total\n"
        ct = "text/plain; version=0.0.4; charset=utf-8"
        ctx = ResponseContext.raw(body=body, content_type=ct)
        assert ctx.body == body
        assert ctx.content_type == ct

    def test_raw_is_not_streaming(self):
        """raw() must not set is_streaming."""
        ctx = ResponseContext.raw(body="text", content_type="text/plain")
        assert ctx.is_streaming is False

    def test_raw_custom_status_code(self):
        """raw() respects custom status_code."""
        ctx = ResponseContext.raw(
            body="error",
            content_type="text/plain",
            status_code=503,
        )
        assert ctx.status_code == 503

    def test_raw_default_status_200(self):
        """raw() defaults to status 200."""
        ctx = ResponseContext.raw(body="ok", content_type="text/plain")
        assert ctx.status_code == 200


# =============================================================================
# ResponseContext.service_unavailable() factory
# =============================================================================


class TestResponseContextServiceUnavailableBehavior:
    """ResponseContext.service_unavailable() factory behavior."""

    def test_service_unavailable_returns_503(self):
        """service_unavailable() must return status 503."""
        ctx = ResponseContext.service_unavailable()
        assert ctx.status_code == 503

    def test_service_unavailable_default_message(self):
        """service_unavailable() has default message."""
        ctx = ResponseContext.service_unavailable()
        assert ctx.body["error"] == "Service not available"

    def test_service_unavailable_custom_message(self):
        """service_unavailable() accepts custom message."""
        ctx = ResponseContext.service_unavailable(message="DB down")
        assert ctx.body["error"] == "DB down"

    def test_service_unavailable_with_error_code(self):
        """service_unavailable() accepts error_code."""
        ctx = ResponseContext.service_unavailable(
            message="overloaded",
            error_code="OVERLOADED",
        )
        assert ctx.body["error_code"] == "OVERLOADED"

    def test_service_unavailable_body_has_success_false(self):
        """service_unavailable() body includes success=False (via error())."""
        ctx = ResponseContext.service_unavailable()
        assert ctx.body["success"] is False


# =============================================================================
# add_route() signature contract
# =============================================================================


class TestAddRouteSignatureContract:
    """add_route() signature must use PermissionLevel (doc §4.3.3)."""

    def test_add_route_has_permission_level_parameter(self):
        """add_route() must accept permission_level parameter."""
        import inspect

        sig = inspect.signature(WebFrameworkInterface.add_route)
        assert "permission_level" in sig.parameters

    def test_add_route_permission_level_default_is_authenticated(self):
        """add_route() permission_level defaults to AUTHENTICATED."""
        import inspect

        sig = inspect.signature(WebFrameworkInterface.add_route)
        param = sig.parameters["permission_level"]
        assert param.default == PermissionLevel.AUTHENTICATED

    def test_add_route_has_custom_permissions_parameter(self):
        """add_route() must accept custom_permissions for edge cases."""
        import inspect

        sig = inspect.signature(WebFrameworkInterface.add_route)
        assert "custom_permissions" in sig.parameters

    def test_add_route_no_auth_required_parameter(self):
        """add_route() must not have old auth_required parameter."""
        import inspect

        sig = inspect.signature(WebFrameworkInterface.add_route)
        assert "auth_required" not in sig.parameters

    def test_add_route_no_permissions_list_parameter(self):
        """add_route() must not have old permissions: list[str] parameter."""
        import inspect

        sig = inspect.signature(WebFrameworkInterface.add_route)
        # custom_permissions exists, but the old 'permissions' does not
        assert "permissions" not in sig.parameters
