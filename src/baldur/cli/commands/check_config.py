"""
``baldur check-config`` - preflight validation + settings inspection.

Replaces the Django management commands ``check_baldur_config`` and
``baldur_config --inspect/--validate``. Intentionally does not call
``baldur.init()`` - users run this from CI/CD where the full process
does not need to start.

Exit codes:
    0 - all fatal configs valid (warnings allowed unless --strict)
    1 - ``--strict`` and fatal violations detected
    2 - internal error (settings failed to import)
"""

from __future__ import annotations

import json
import sys
from typing import Any

import typer


def check_config(
    strict: bool = typer.Option(
        False,
        "--strict",
        help="Exit 1 when fatal violations exist (CI/CD hard block).",
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Emit JSON instead of human-readable text.",
    ),
    inspect: bool = typer.Option(
        False,
        "--inspect",
        help="Also dump the resolved settings tree.",
    ),
    config_type: str | None = typer.Option(
        None,
        "--config-type",
        help="Limit --inspect to one settings section (e.g. circuit_breaker).",
    ),
) -> None:
    """Validate Baldur configuration against fatal/non-fatal rules."""
    try:
        from baldur.core.safe_defaults import validate_config_preflight
        from baldur.settings import get_config
    except ImportError as exc:
        _fail(f"Failed to import baldur modules: {exc}", json_output)
        raise typer.Exit(code=2) from None

    try:
        config = get_config()
        result = validate_config_preflight(config)
    except Exception as exc:
        _fail(f"Failed to validate config: {exc}", json_output)
        raise typer.Exit(code=2) from None

    fatal_count = sum(len(keys) for keys in result.fatal_violations.values())
    warning_count = sum(len(keys) for keys in result.non_fatal_warnings.values())

    payload: dict[str, Any] = {
        "status": "valid" if result.is_valid else "invalid",
        "fatal_violations": result.fatal_violations,
        "non_fatal_warnings": result.non_fatal_warnings,
        "fatal_violation_count": fatal_count,
        "warning_count": warning_count,
    }

    if inspect:
        payload["config"] = _inspect_settings(config, config_type)

    if json_output:
        typer.echo(json.dumps(payload, indent=2, default=str, ensure_ascii=False))
    else:
        _print_text(payload)
        if inspect:
            _print_inspect(payload["config"])

    if result.has_fatal_violations and strict:
        raise typer.Exit(code=1)


def _inspect_settings(config: Any, config_type: str | None) -> dict[str, Any]:
    if config_type:
        if not hasattr(config, config_type):
            raise typer.BadParameter(f"Unknown config type: {config_type}")
        sub = getattr(config, config_type)
        return {config_type: _model_to_dict(sub)}
    return _model_to_dict(config)


def _model_to_dict(model: Any) -> dict[str, Any]:
    if hasattr(model, "to_full_dict"):
        return model.to_full_dict()
    if hasattr(model, "model_dump"):
        return model.model_dump()
    return dict(model)


def _print_text(payload: dict[str, Any]) -> None:
    typer.echo("=" * 60)
    typer.echo("  Baldur Configuration Pre-flight Check")
    typer.echo("=" * 60)

    if payload["fatal_violations"]:
        typer.secho(
            f"\nFATAL VIOLATIONS ({payload['fatal_violation_count']})",
            fg=typer.colors.RED,
            bold=True,
        )
        for section, violations in payload["fatal_violations"].items():
            typer.echo(f"  [{section}]")
            for key, msg in violations.items():
                typer.echo(f"    - {key}: {msg}")

    if payload["non_fatal_warnings"]:
        typer.secho(
            f"\nNON-FATAL WARNINGS ({payload['warning_count']})",
            fg=typer.colors.YELLOW,
        )
        for section, warnings in payload["non_fatal_warnings"].items():
            typer.echo(f"  [{section}]")
            for key, msg in warnings.items():
                typer.echo(f"    - {key}: {msg}")

    typer.echo("-" * 60)
    if payload["status"] == "valid":
        if payload["warning_count"] > 0:
            typer.secho(
                f"Config valid with {payload['warning_count']} warning(s)",
                fg=typer.colors.YELLOW,
            )
        else:
            typer.secho("All configurations are valid", fg=typer.colors.GREEN)
    else:
        typer.secho(
            f"FAILED: {payload['fatal_violation_count']} fatal violation(s)",
            fg=typer.colors.RED,
            bold=True,
        )


def _print_inspect(config_tree: dict[str, Any], prefix: str = "") -> None:
    for key, value in config_tree.items():
        if isinstance(value, dict):
            typer.secho(f"\n[{prefix}{key}]", fg=typer.colors.CYAN)
            _print_inspect(value, prefix=f"{prefix}{key}.")
        else:
            typer.echo(f"  {key}: {value}")


def _fail(message: str, json_output: bool) -> None:
    if json_output:
        typer.echo(
            json.dumps({"status": "error", "message": message}, ensure_ascii=False),
            err=True,
        )
    else:
        typer.secho(message, fg=typer.colors.RED, err=True)
    sys.stderr.flush()
