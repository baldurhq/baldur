"""
``baldur escalation ...`` - escalation channel operations.

Subcommands:
    baldur escalation test    # send a labelled self-test notification to
                              # every configured channel and report per-channel
                              # delivery

Shares :func:`baldur.api.handlers.meta_watchdog.meta_watchdog_send_test`
with the admin server so the CLI and HTTP surface run one code path - the
same handler, status mapping, and audit/RBAC model apply.
"""

from __future__ import annotations

import typer

from baldur.api.handlers.meta_watchdog import meta_watchdog_send_test
from baldur.cli._bootstrap import ensure_init
from baldur.cli._invoke import (
    build_request_context,
    exit_code_for,
    print_response,
    run_handler,
)

__all__ = ["escalation_app"]

escalation_app = typer.Typer(
    name="escalation",
    help="Escalation channel operations.",
    no_args_is_help=True,
)


@escalation_app.command("test")
def escalation_test_cmd(
    ctx: typer.Context,
    json_output: bool = typer.Option(
        False, "--json", help="Emit JSON instead of pretty text."
    ),
) -> None:
    """Send a test notification to every configured escalation channel.

    Validates the configured Slack/PagerDuty channel. On an OSS install the
    test validates config and logs the intended delivery (live external push
    is a PRO capability); on PRO it confirms the channel actually delivers.
    Exit code: 0 all delivered, 2 no channel configured, 1 a configured
    channel failed.
    """
    ensure_init(ctx)
    request = build_request_context(
        method="POST",
        path="/meta-watchdog/escalation-test",
    )
    response = run_handler(meta_watchdog_send_test, request)
    print_response(response, json_output=json_output)
    raise typer.Exit(code=exit_code_for(response))
