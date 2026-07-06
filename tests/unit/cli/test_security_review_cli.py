"""Unit tests for security-review CLI subcommand.

Verification techniques:
- Behavior: command construction, handler delegation, exit codes
- Dependency interaction: ensure_init, build_request_context, run_handler calls
- Edge cases: optional flags (--output, --quiet, --json)
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import typer

from baldur.interfaces.web_framework import ResponseContext


def _make_mock_response(status_code=200):
    """Create a mock ResponseContext with a given status code."""
    return ResponseContext.json(
        {"status": "ok", "pass_rate": 100.0},
        status_code=status_code,
    )


# =============================================================================
# Behavior — handler function lazy import
# =============================================================================


class TestSecurityReviewHandlerWiringBehavior:
    """_handler() returns security_review_run from the handler module."""

    def test_handler_returns_callable(self):
        """_handler() returns the security_review_run function."""
        from baldur.cli.commands.security_review import _handler

        handler = _handler()
        assert callable(handler)
        assert handler.__name__ == "security_review_run"


# =============================================================================
# Behavior — request context construction
# =============================================================================


class TestSecurityReviewRequestContextBehavior:
    """CLI builds correct RequestContext from options."""

    def test_default_invocation_passes_no_query_params(self):
        """Without --output/--quiet, query dict is empty (None)."""
        from baldur.cli.commands.security_review import security_review

        mock_response = _make_mock_response(200)

        with (
            patch("baldur.cli.commands.security_review.ensure_init"),
            patch(
                "baldur.cli.commands.security_review.build_request_context",
                return_value=MagicMock(),
            ) as mock_build,
            patch(
                "baldur.cli.commands.security_review.run_handler",
                return_value=mock_response,
            ),
            patch("baldur.cli.commands.security_review.print_response"),
            pytest.raises(typer.Exit),
        ):
            ctx = MagicMock()
            security_review(ctx, output=None, quiet=False, json_output=False)

        call_kwargs = mock_build.call_args[1]
        assert call_kwargs["method"] == "GET"
        assert call_kwargs["path"] == "/security-review/"
        assert call_kwargs["query"] is None

    def test_output_flag_passes_query_param(self):
        """--output adds output key to query."""
        from baldur.cli.commands.security_review import security_review

        mock_response = _make_mock_response(200)

        with (
            patch("baldur.cli.commands.security_review.ensure_init"),
            patch(
                "baldur.cli.commands.security_review.build_request_context",
                return_value=MagicMock(),
            ) as mock_build,
            patch(
                "baldur.cli.commands.security_review.run_handler",
                return_value=mock_response,
            ),
            patch("baldur.cli.commands.security_review.print_response"),
            pytest.raises(typer.Exit),
        ):
            ctx = MagicMock()
            security_review(ctx, output="/tmp/out.json", quiet=False, json_output=False)

        call_kwargs = mock_build.call_args[1]
        assert call_kwargs["query"]["output"] == "/tmp/out.json"

    def test_quiet_flag_passes_query_param(self):
        """--quiet adds quiet=true to query."""
        from baldur.cli.commands.security_review import security_review

        mock_response = _make_mock_response(200)

        with (
            patch("baldur.cli.commands.security_review.ensure_init"),
            patch(
                "baldur.cli.commands.security_review.build_request_context",
                return_value=MagicMock(),
            ) as mock_build,
            patch(
                "baldur.cli.commands.security_review.run_handler",
                return_value=mock_response,
            ),
            patch("baldur.cli.commands.security_review.print_response"),
            pytest.raises(typer.Exit),
        ):
            ctx = MagicMock()
            security_review(ctx, output=None, quiet=True, json_output=False)

        call_kwargs = mock_build.call_args[1]
        assert call_kwargs["query"]["quiet"] == "true"


# =============================================================================
# Behavior — exit codes
# =============================================================================


class TestSecurityReviewExitCodeBehavior:
    """CLI exit code reflects handler response status."""

    def test_exit_0_on_200_response(self):
        """200 response yields exit code 0."""
        from baldur.cli.commands.security_review import security_review

        mock_response = _make_mock_response(200)

        with (
            patch("baldur.cli.commands.security_review.ensure_init"),
            patch(
                "baldur.cli.commands.security_review.build_request_context",
                return_value=MagicMock(),
            ),
            patch(
                "baldur.cli.commands.security_review.run_handler",
                return_value=mock_response,
            ),
            patch("baldur.cli.commands.security_review.print_response"),
            pytest.raises(typer.Exit) as exc_info,
        ):
            ctx = MagicMock()
            security_review(ctx, output=None, quiet=False, json_output=False)

        assert exc_info.value.exit_code == 0

    def test_exit_1_on_422_response(self):
        """422 response (FAILED security review) yields exit code 1."""
        from baldur.cli.commands.security_review import security_review

        mock_response = _make_mock_response(422)

        with (
            patch("baldur.cli.commands.security_review.ensure_init"),
            patch(
                "baldur.cli.commands.security_review.build_request_context",
                return_value=MagicMock(),
            ),
            patch(
                "baldur.cli.commands.security_review.run_handler",
                return_value=mock_response,
            ),
            patch("baldur.cli.commands.security_review.print_response"),
            pytest.raises(typer.Exit) as exc_info,
        ):
            ctx = MagicMock()
            security_review(ctx, output=None, quiet=False, json_output=False)

        assert exc_info.value.exit_code == 1

    def test_exit_1_on_500_response(self):
        """500 response yields exit code 1."""
        from baldur.cli.commands.security_review import security_review

        mock_response = _make_mock_response(500)

        with (
            patch("baldur.cli.commands.security_review.ensure_init"),
            patch(
                "baldur.cli.commands.security_review.build_request_context",
                return_value=MagicMock(),
            ),
            patch(
                "baldur.cli.commands.security_review.run_handler",
                return_value=mock_response,
            ),
            patch("baldur.cli.commands.security_review.print_response"),
            pytest.raises(typer.Exit) as exc_info,
        ):
            ctx = MagicMock()
            security_review(ctx, output=None, quiet=False, json_output=False)

        assert exc_info.value.exit_code == 1


# =============================================================================
# Behavior — json_output delegation
# =============================================================================


class TestSecurityReviewJsonOutputBehavior:
    """--json flag is forwarded to print_response."""

    def test_json_flag_passed_to_print_response(self):
        """--json=True is forwarded to print_response(json_output=True)."""
        from baldur.cli.commands.security_review import security_review

        mock_response = _make_mock_response(200)

        with (
            patch("baldur.cli.commands.security_review.ensure_init"),
            patch(
                "baldur.cli.commands.security_review.build_request_context",
                return_value=MagicMock(),
            ),
            patch(
                "baldur.cli.commands.security_review.run_handler",
                return_value=mock_response,
            ),
            patch("baldur.cli.commands.security_review.print_response") as mock_print,
            pytest.raises(typer.Exit),
        ):
            ctx = MagicMock()
            security_review(ctx, output=None, quiet=False, json_output=True)

        mock_print.assert_called_once_with(mock_response, json_output=True)
