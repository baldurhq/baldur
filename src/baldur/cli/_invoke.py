"""
Shared CLI helpers: handler invocation + output formatting.

Subcommands that share logic with the admin server (dlq, cb, report)
build a :class:`RequestContext`, call the handler function, and format
:class:`ResponseContext` for the terminal. Non-handler commands
(``check-config``, ``scheduler list``, ``admin``) bypass this and call
services directly.
"""

from __future__ import annotations

import json
import sys
from typing import Any

import structlog

from baldur.interfaces.web_framework import (
    HttpMethod,
    RequestContext,
    ResponseContext,
)

logger = structlog.get_logger()

__all__ = [
    "build_request_context",
    "print_response",
    "exit_code_for",
    "run_handler",
]


def build_request_context(
    *,
    method: str = "GET",
    path: str = "/",
    query: dict[str, Any] | None = None,
    json_body: dict[str, Any] | None = None,
    path_params: dict[str, Any] | None = None,
    actor: str = "cli",
) -> RequestContext:
    """Construct a minimal ``RequestContext`` suitable for CLI dispatch.

    Actor identity is stamped into headers so handler audit logs show
    ``cli`` instead of ``unknown`` - the same mechanism
    ``baldur.api.handlers._common.resolve_actor`` uses.
    """
    headers = {"X-Baldur-Actor": actor}
    return RequestContext(
        method=HttpMethod(method.upper()),
        path=path,
        headers=headers,
        query_params=query or {},
        path_params=path_params or {},
        json_body=json_body,
        is_authenticated=True,
        client_ip="127.0.0.1",
        user_agent="baldur-cli",
    )


def run_handler(handler, ctx: RequestContext) -> ResponseContext:
    """Invoke a handler and surface framework errors as JSON responses.

    Most handler errors are already converted to ``ResponseContext`` by
    the handler itself. This wrapper catches the remaining unexpected
    exceptions (import failures, adapter gaps) and converts them so the
    CLI never crashes with a bare traceback on a handler gap. The
    exception is logged at ERROR so handler gaps don't become silent
    500s.
    """
    try:
        return handler(ctx)
    except Exception as exc:
        logger.exception(
            "cli.handler_unhandled_error",
            handler=getattr(handler, "__name__", repr(handler)),
            path=ctx.path,
            method=ctx.method.value,
            error_type=type(exc).__name__,
        )
        return ResponseContext.json(
            {
                "status": "error",
                "error": str(exc),
                "error_type": type(exc).__name__,
            },
            status_code=500,
        )


def print_response(
    response: ResponseContext,
    *,
    json_output: bool = False,
    stream=None,
) -> None:
    """Render a ``ResponseContext`` to stdout.

    ``json_output=True`` emits the raw body as JSON (useful for
    machine-readable CI consumption). The default pretty-prints the body
    for terminal use while still emitting JSON for structured data.
    """
    out = stream or sys.stdout
    body = response.body

    if json_output or isinstance(body, (dict, list)):
        out.write(json.dumps(body, indent=2, default=str, ensure_ascii=False))
        out.write("\n")
    elif body is None:
        out.write("(no content)\n")
    else:
        out.write(str(body))
        out.write("\n")


def exit_code_for(response: ResponseContext) -> int:
    """Map HTTP status code to process exit code.

    2xx/3xx -> 0 (success). 4xx -> 2 (user / validation error).
    5xx -> 1 (server / framework error). Matches the convention typer
    itself uses - Exit(0) for success, Exit(>=1) for failure.
    """
    status = response.status_code
    if 200 <= status < 400:
        return 0
    if 400 <= status < 500:
        return 2
    return 1
