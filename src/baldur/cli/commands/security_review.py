"""
``baldur security-review`` — run comprehensive security checks.

Shares :mod:`baldur.adapters.django.management.commands.security_review`
check functions but invokes them via the CLI handler pattern
(:mod:`baldur.cli._invoke`).
"""

from __future__ import annotations

import typer

from baldur.cli._bootstrap import ensure_init
from baldur.cli._invoke import build_request_context, print_response, run_handler


def _handler():
    from baldur.api.handlers.security_review import security_review_run

    return security_review_run


def security_review(
    ctx: typer.Context,
    output: str | None = typer.Option(
        None,
        "--output",
        "-o",
        help="Export JSON results to this path.",
    ),
    quiet: bool = typer.Option(
        False,
        "--quiet",
        "-q",
        help="Only show summary, not individual checks.",
    ),
    json_output: bool = typer.Option(
        False, "--json", help="Emit JSON instead of pretty text."
    ),
) -> None:
    """Run comprehensive security review for the Baldur system."""
    ensure_init(ctx)

    query: dict[str, str] = {}
    if output:
        query["output"] = output
    if quiet:
        query["quiet"] = "true"

    request = build_request_context(
        method="GET",
        path="/security-review/",
        query=query or None,
    )
    response = run_handler(_handler(), request)
    print_response(response, json_output=json_output)
    raise typer.Exit(code=0 if 200 <= response.status_code < 400 else 1)
