"""
Shared CLI helpers: handler invocation + output formatting.

Subcommands that share logic with the admin server (dlq, cb, report)
build a :class:`RequestContext`, call the handler function, and format
:class:`ResponseContext` for the terminal. Non-handler commands
(``check-config``, ``scheduler list``, ``admin``) bypass this and call
services directly.
"""

from __future__ import annotations

import contextlib
import json
import sys
from collections.abc import Iterator
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

# Actor vocabulary for a terminal invocation — matches ``ContextType.CLI`` so a
# ledger row's actor type and context type name the same entry point.
CLI_ACTOR_TYPE = "cli"

# Source recorded when the command name cannot be resolved from the active
# command context (a handler driven outside a parsed command line).
CLI_ACTOR_SOURCE_FALLBACK = "baldur cli"


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


def _operator_actor_id() -> str:
    """Identity a terminal invocation carries: ``user@host``.

    The same formula the management-command entry point uses, so one operator
    reads the same in the ledger whichever entry point they drove. A host with
    no resolvable login name degrades to the process owner's uid rather than
    raising — an unnamed operator is still a better record than none.
    """
    import getpass
    import os
    import socket

    try:
        user = getpass.getuser()
    except Exception:
        user = f"uid-{os.getuid()}" if hasattr(os, "getuid") else "unknown-user"

    try:
        host = socket.gethostname()
    except Exception:
        host = "unknown-host"

    return f"{user}@{host}"


def _invoked_command_path() -> str:
    """Name the command being run, e.g. ``baldur cb force-open``.

    Read from the active command context rather than passed in by each
    command, so a new subcommand is attributed without editing this seam.
    """
    try:
        import click

        ctx = click.get_current_context(silent=True)
        if ctx is not None and ctx.command_path:
            return ctx.command_path
    except Exception:
        pass
    return CLI_ACTOR_SOURCE_FALLBACK


@contextlib.contextmanager
def _operator_actor_context() -> Iterator[None]:
    """Attribute the work done inside to the operator at the terminal.

    Without this, nothing under ``cli/`` sets an actor, so
    ``ActorContext.get_current()`` hands back the system actor and an
    operator's force-open is recorded in the compliance ledger as an
    *automatic* one. An actor already set by an embedding caller wins — this
    only fills the gap, it never overwrites an identity someone else
    established.

    Fail-open: the context module being unavailable leaves the invocation
    running unattributed rather than failing the command. The import is
    resolved *before* the body runs, so an ``ImportError`` raised by the
    handler itself is never mistaken for this module being absent.
    """
    try:
        from baldur.context.actor_context import ActorContext

        already_attributed = ActorContext.is_set()
    except ImportError:
        yield
        return

    if already_attributed:
        yield
        return

    with ActorContext.set_actor(
        actor_id=_operator_actor_id(),
        actor_type=CLI_ACTOR_TYPE,
        source=_invoked_command_path(),
    ):
        yield


def run_handler(handler, ctx: RequestContext) -> ResponseContext:
    """Invoke a handler and surface framework errors as JSON responses.

    Most handler errors are already converted to ``ResponseContext`` by
    the handler itself. This wrapper catches the remaining unexpected
    exceptions (import failures, adapter gaps) and converts them so the
    CLI never crashes with a bare traceback on a handler gap. The
    exception is logged at ERROR so handler gaps don't become silent
    500s.

    The invocation runs under an operator :class:`ActorContext`. This is the
    seam every handler-backed command routes through, so state-changing
    commands are attributed to the person who ran them without each command
    repeating the wiring.
    """
    try:
        with _operator_actor_context():
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
