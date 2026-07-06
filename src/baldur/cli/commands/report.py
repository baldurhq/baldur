"""
``baldur report`` - daily report inspection.

Shares :mod:`baldur.api.handlers.daily_report` with the admin server so
CLI and HTTP surface identical data. The value of ``--date`` follows the
handler's ``YYYY-MM-DD`` format; the sentinel ``today`` is expanded on
the CLI side for convenience.
"""

from __future__ import annotations

import typer

from baldur.api.handlers.daily_report import daily_report_detail, daily_report_list
from baldur.cli._bootstrap import ensure_init
from baldur.cli._invoke import build_request_context, print_response, run_handler
from baldur.utils.time import utc_now


def report(
    ctx: typer.Context,
    date: str | None = typer.Option(
        None,
        "--date",
        "-d",
        help=(
            "Report date (YYYY-MM-DD, or 'today' for the UTC date). "
            "Omit to list recent reports."
        ),
    ),
    days: int = typer.Option(
        7,
        "--days",
        help="When --date is omitted, list this many recent reports.",
    ),
    json_output: bool = typer.Option(
        False, "--json", help="Emit JSON instead of pretty text."
    ),
) -> None:
    """Show a single daily report or list recent reports."""
    ensure_init(ctx)

    if date is None:
        request = build_request_context(
            method="GET",
            path="/reports/daily/",
            query={"days": str(days)},
        )
        response = run_handler(daily_report_list, request)
        print_response(response, json_output=json_output)
        raise typer.Exit(code=0 if 200 <= response.status_code < 400 else 1)

    resolved_date = _resolve_date(date)
    request = build_request_context(
        method="GET",
        path=f"/reports/daily/{resolved_date}/",
        path_params={"date": resolved_date},
    )
    response = run_handler(daily_report_detail, request)
    print_response(response, json_output=json_output)
    if response.status_code == 404:
        raise typer.Exit(code=2)
    raise typer.Exit(code=0 if 200 <= response.status_code < 400 else 1)


def _resolve_date(raw: str) -> str:
    if raw.lower() == "today":
        return utc_now().strftime("%Y-%m-%d")
    return raw
