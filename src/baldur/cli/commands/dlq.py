"""
``baldur dlq ...`` - dead-letter queue operations.

Subcommands:
    baldur dlq list     [--status] [--domain] [--page] [--page-size]
    baldur dlq replay   [--domain] [--batch-size N]

All subcommands share the framework-agnostic handlers under
:mod:`baldur.api.handlers.dlq` so CLI and admin HTTP cannot drift.
"""

from __future__ import annotations

import typer

from baldur.api.handlers.dlq import dlq_list, dlq_replay
from baldur.cli._bootstrap import ensure_init
from baldur.cli._invoke import (
    build_request_context,
    exit_code_for,
    print_response,
    run_handler,
)

dlq_app = typer.Typer(
    name="dlq",
    help="Dead-letter queue operations.",
    no_args_is_help=True,
)


@dlq_app.command("list")
def dlq_list_cmd(
    ctx: typer.Context,
    status: str | None = typer.Option(None, "--status", help="Filter by status."),
    domain: str | None = typer.Option(
        None, "--domain", help="Filter by healing domain."
    ),
    page: int = typer.Option(1, "--page", min=1),
    page_size: int = typer.Option(20, "--page-size", min=1, max=200),
    pending: bool = typer.Option(
        False,
        "--pending",
        help="Shortcut for --status pending (overrides --status when set).",
    ),
    json_output: bool = typer.Option(
        False, "--json", help="Emit JSON instead of pretty text."
    ),
) -> None:
    """List DLQ entries (paginated)."""
    ensure_init(ctx)

    query: dict[str, str] = {
        "page": str(page),
        "page_size": str(page_size),
    }
    effective_status = "pending" if pending else status
    if effective_status:
        query["status"] = effective_status
    if domain:
        query["domain"] = domain

    request = build_request_context(method="GET", path="/dlq/list/", query=query)
    response = run_handler(dlq_list, request)
    print_response(response, json_output=json_output)
    raise typer.Exit(code=exit_code_for(response))


@dlq_app.command("replay")
def dlq_replay_cmd(
    ctx: typer.Context,
    domain: str | None = typer.Option(
        None, "--domain", help="Limit replay to one healing domain."
    ),
    batch_size: int = typer.Option(
        50, "--batch-size", min=1, max=200, help="Entries per replay batch."
    ),
    json_output: bool = typer.Option(
        False, "--json", help="Emit JSON instead of pretty text."
    ),
) -> None:
    """Trigger a DLQ replay cycle."""
    ensure_init(ctx)

    body: dict[str, object] = {"batch_size": batch_size}
    if domain:
        body["domain"] = domain

    request = build_request_context(method="POST", path="/dlq/replay/", json_body=body)
    response = run_handler(dlq_replay, request)
    print_response(response, json_output=json_output)
    raise typer.Exit(code=exit_code_for(response))
