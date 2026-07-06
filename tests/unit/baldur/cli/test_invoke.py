"""Unit tests for baldur.cli._invoke (CLI handler bridge helpers)."""

from __future__ import annotations

import io
import json

import pytest

from baldur.cli._invoke import (
    build_request_context,
    exit_code_for,
    print_response,
    run_handler,
)
from baldur.interfaces.web_framework import (
    HttpMethod,
    ResponseContext,
)

# =============================================================================
# Behavior — build_request_context
# =============================================================================


class TestBuildRequestContextBehavior:
    """RequestContext assembled for CLI dispatch must look 'authenticated' to
    handlers and carry the X-Baldur-Actor header so audit logs attribute the
    action to the CLI user rather than 'unknown'."""

    def test_defaults_produce_get_root_authenticated_cli_actor(self):
        ctx = build_request_context()

        assert ctx.method is HttpMethod.GET
        assert ctx.path == "/"
        assert ctx.is_authenticated is True
        assert ctx.client_ip == "127.0.0.1"
        assert ctx.user_agent == "baldur-cli"
        assert ctx.headers.get("X-Baldur-Actor") == "cli"

    def test_method_string_normalized_to_enum(self):
        ctx = build_request_context(method="post")
        assert ctx.method is HttpMethod.POST

    def test_query_defaults_to_empty_dict(self):
        ctx = build_request_context()
        assert ctx.query_params == {}
        assert ctx.path_params == {}
        assert ctx.json_body is None

    def test_query_and_path_params_propagated(self):
        ctx = build_request_context(
            path="/dlq/list/",
            query={"page": "2"},
            path_params={"id": "42"},
        )
        assert ctx.query_params == {"page": "2"}
        assert ctx.path_params == {"id": "42"}

    def test_json_body_propagated(self):
        ctx = build_request_context(json_body={"reason": "ci"})
        assert ctx.json_body == {"reason": "ci"}

    def test_custom_actor_recorded_in_header(self):
        ctx = build_request_context(actor="operator-42")
        assert ctx.headers["X-Baldur-Actor"] == "operator-42"

    def test_path_params_empty_dict_isolated_between_calls(self):
        """Mutable defaults must not leak between ctx instances."""
        ctx1 = build_request_context()
        ctx2 = build_request_context()
        ctx1.path_params["x"] = "y"
        assert "x" not in ctx2.path_params


# =============================================================================
# Behavior — run_handler exception wrapping
# =============================================================================


class TestRunHandlerBehavior:
    """run_handler converts unexpected exceptions to 500 ResponseContext."""

    def test_returns_handler_response_on_success(self):
        expected = ResponseContext.json({"ok": True}, status_code=200)

        def handler(_ctx):
            return expected

        ctx = build_request_context()
        response = run_handler(handler, ctx)

        assert response is expected

    def test_handler_exception_converted_to_500_with_type_and_message(self):
        class OhNo(RuntimeError):
            pass

        def handler(_ctx):
            raise OhNo("kaboom")

        response = run_handler(handler, build_request_context())

        assert response.status_code == 500
        assert response.body["status"] == "error"
        assert response.body["error"] == "kaboom"
        assert response.body["error_type"] == "OhNo"

    def test_handler_exception_does_not_propagate(self):
        """The CLI must never crash with a bare traceback on handler gaps."""

        def handler(_ctx):
            raise ImportError("missing module")

        run_handler(handler, build_request_context())  # must not raise

    def test_handler_exception_is_logged_before_conversion(self):
        """Handler gaps must produce an ERROR-level log entry, not a silent 500.

        `_invoke.run_handler` previously swallowed exceptions without logging,
        which made handler gaps invisible in production. The wrapper now emits
        a structured log at ERROR before returning the 500 ResponseContext.
        """
        import structlog

        def boom(_ctx):
            raise RuntimeError("kaboom")

        ctx = build_request_context(method="POST", path="/dlq/replay/")

        with structlog.testing.capture_logs() as logs:
            response = run_handler(boom, ctx)

        assert response.status_code == 500
        error_logs = [
            e for e in logs if e.get("event") == "cli.handler_unhandled_error"
        ]
        assert len(error_logs) == 1
        entry = error_logs[0]
        assert entry["log_level"] == "error"
        assert entry["handler"] == "boom"
        assert entry["path"] == "/dlq/replay/"
        assert entry["method"] == "POST"
        assert entry["error_type"] == "RuntimeError"

    def test_handler_success_emits_no_error_log(self):
        """Happy path must not log cli.handler_unhandled_error."""
        import structlog

        def ok(_ctx):
            return ResponseContext.json({"ok": True})

        with structlog.testing.capture_logs() as logs:
            run_handler(ok, build_request_context())

        assert not [e for e in logs if e.get("event") == "cli.handler_unhandled_error"]


# =============================================================================
# Contract — exit_code_for HTTP→process mapping
# =============================================================================


class TestExitCodeForContract:
    """Matches the mapping documented in the docstring:
    2xx/3xx -> 0, 4xx -> 2, 5xx -> 1."""

    @pytest.mark.parametrize("code", [200, 201, 204, 301, 307, 399])
    def test_success_and_redirects_map_to_zero(self, code):
        assert exit_code_for(ResponseContext(status_code=code)) == 0

    @pytest.mark.parametrize("code", [400, 401, 403, 404, 422, 499])
    def test_client_errors_map_to_two(self, code):
        assert exit_code_for(ResponseContext(status_code=code)) == 2

    @pytest.mark.parametrize("code", [500, 502, 503, 504, 599])
    def test_server_errors_map_to_one(self, code):
        assert exit_code_for(ResponseContext(status_code=code)) == 1

    def test_boundary_200_is_success(self):
        assert exit_code_for(ResponseContext(status_code=200)) == 0

    def test_boundary_399_is_success(self):
        assert exit_code_for(ResponseContext(status_code=399)) == 0

    def test_boundary_400_is_client_error(self):
        assert exit_code_for(ResponseContext(status_code=400)) == 2

    def test_boundary_500_is_server_error(self):
        assert exit_code_for(ResponseContext(status_code=500)) == 1


# =============================================================================
# Behavior — print_response rendering
# =============================================================================


class TestPrintResponseBehavior:
    """Dict/list bodies render as JSON (human + CI-friendly), strings are raw."""

    def test_dict_body_renders_as_indented_json(self):
        stream = io.StringIO()
        response = ResponseContext.json({"status": "ok", "count": 3})

        print_response(response, stream=stream)

        parsed = json.loads(stream.getvalue())
        assert parsed == {"status": "ok", "count": 3}
        assert stream.getvalue().endswith("\n")

    def test_list_body_renders_as_json(self):
        stream = io.StringIO()
        response = ResponseContext(status_code=200, body=[1, 2, 3])

        print_response(response, stream=stream)

        assert json.loads(stream.getvalue()) == [1, 2, 3]

    def test_string_body_renders_as_plain_text(self):
        stream = io.StringIO()
        response = ResponseContext(status_code=200, body="plain text")

        print_response(response, stream=stream)

        assert stream.getvalue() == "plain text\n"

    def test_none_body_renders_no_content_marker(self):
        stream = io.StringIO()
        response = ResponseContext(status_code=204, body=None)

        print_response(response, stream=stream)

        assert stream.getvalue() == "(no content)\n"

    def test_json_output_flag_forces_json_on_string_body(self):
        """`--json` from the CLI must emit JSON even when body is a scalar."""
        stream = io.StringIO()
        response = ResponseContext(status_code=200, body="raw")

        print_response(response, json_output=True, stream=stream)

        # The body was a string, so json.dumps emits a quoted string.
        assert stream.getvalue().strip() == '"raw"'

    def test_ensure_ascii_false_preserves_unicode(self):
        stream = io.StringIO()
        response = ResponseContext.json({"msg": "안녕"})

        print_response(response, stream=stream)

        assert "안녕" in stream.getvalue()

    def test_non_serializable_body_uses_default_str(self):
        """`default=str` fallback prevents crashes on datetime/custom objects."""
        from datetime import datetime

        class Exotic:
            def __str__(self):
                return "EXOTIC"

        stream = io.StringIO()
        response = ResponseContext.json({"t": datetime(2026, 4, 16), "x": Exotic()})

        print_response(response, stream=stream)

        output = stream.getvalue()
        assert "EXOTIC" in output
        assert "2026-04-16" in output
