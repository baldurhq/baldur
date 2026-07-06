"""
``baldur cb ...`` - circuit-breaker control.

Subcommands:
    baldur cb list                       # all service states
    baldur cb reset       <name>         # reset one service to defaults
    baldur cb force-open  <name> [...]   # quick block (opens the breaker)
    baldur cb force-close <name> [...]   # quick allow (closes the breaker)

Shares :mod:`baldur.api.handlers.circuit_breaker` with the admin server.
All mutating subcommands require ``BALDUR_ADMIN_UNLOCK=1`` when invoked
via the admin HTTP surface - the CLI reuses the same control service so
the same audit trail and RBAC model applies.
"""

from __future__ import annotations

import typer

from baldur.api.handlers.circuit_breaker import (
    control_status,
    quick_allow,
    quick_block,
    quick_reset,
)
from baldur.cli._bootstrap import ensure_init
from baldur.cli._invoke import (
    build_request_context,
    exit_code_for,
    print_response,
    run_handler,
)

cb_app = typer.Typer(
    name="cb",
    help="Circuit-breaker control.",
    no_args_is_help=True,
)


@cb_app.command("list")
def cb_list_cmd(
    ctx: typer.Context,
    environment: str = typer.Option(
        "ops", "--environment", "-e", help="Environment filter (ops/chaos/test)."
    ),
    json_output: bool = typer.Option(
        False, "--json", help="Emit JSON instead of pretty text."
    ),
) -> None:
    """List circuit-breaker states for all services."""
    ensure_init(ctx)
    request = build_request_context(
        method="GET",
        path="/control/status/",
        query={"environment": environment},
    )
    response = run_handler(control_status, request)
    print_response(response, json_output=json_output)
    raise typer.Exit(code=exit_code_for(response))


@cb_app.command("reset")
def cb_reset_cmd(
    ctx: typer.Context,
    service_name: str = typer.Argument(..., help="Service name."),
    reason: str = typer.Option(
        "Quick reset via CLI", "--reason", "-r", help="Audit reason."
    ),
    environment: str = typer.Option("ops", "--environment", "-e"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Reset one circuit breaker to its default configuration."""
    ensure_init(ctx)
    response = _run_quick(
        quick_reset,
        service_name=service_name,
        reason=reason,
        environment=environment,
    )
    print_response(response, json_output=json_output)
    raise typer.Exit(code=exit_code_for(response))


@cb_app.command("force-open")
def cb_force_open_cmd(
    ctx: typer.Context,
    service_name: str = typer.Argument(..., help="Service name."),
    reason: str = typer.Option(
        "Force-open via CLI", "--reason", "-r", help="Audit reason."
    ),
    ttl_minutes: int | None = typer.Option(
        None,
        "--ttl",
        help="TTL in minutes (default inferred by handler; 60 max in ops).",
    ),
    environment: str = typer.Option("ops", "--environment", "-e"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Force a circuit breaker OPEN (blocks traffic for the named service)."""
    ensure_init(ctx)
    response = _run_quick(
        quick_block,
        service_name=service_name,
        reason=reason,
        environment=environment,
        ttl_minutes=ttl_minutes,
    )
    print_response(response, json_output=json_output)
    raise typer.Exit(code=exit_code_for(response))


@cb_app.command("force-close")
def cb_force_close_cmd(
    ctx: typer.Context,
    service_name: str = typer.Argument(..., help="Service name."),
    reason: str = typer.Option(
        "Force-close via CLI", "--reason", "-r", help="Audit reason."
    ),
    environment: str = typer.Option("ops", "--environment", "-e"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Force a circuit breaker CLOSED (restores traffic for the named service)."""
    ensure_init(ctx)
    response = _run_quick(
        quick_allow,
        service_name=service_name,
        reason=reason,
        environment=environment,
    )
    print_response(response, json_output=json_output)
    raise typer.Exit(code=exit_code_for(response))


def _run_quick(
    handler,
    *,
    service_name: str,
    reason: str,
    environment: str,
    ttl_minutes: int | None = None,
):
    body: dict[str, object] = {"reason": reason, "environment": environment}
    if ttl_minutes is not None:
        body["ttl_minutes"] = ttl_minutes

    request = build_request_context(
        method="POST",
        path=f"/control/quick/{service_name}/",
        path_params={"service_name": service_name},
        json_body=body,
    )
    return run_handler(handler, request)
