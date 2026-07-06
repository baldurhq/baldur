"""
Baldur CLI - framework-free command-line entry point (429 Part 7).

The ``baldur`` script is registered via ``[project.scripts]`` in
``pyproject.toml`` and dispatches typer subcommands:

    baldur check-config          # Preflight / inspect runtime settings
    baldur admin                 # Start the admin HTTP server (foreground)
    baldur report --date today   # Daily report inspection
    baldur dlq list|replay       # Dead-letter queue operations
    baldur cb list|reset|...     # Circuit-breaker control
    baldur scheduler list        # Scheduled job introspection

CLI invocations resolve configuration via ``baldur.cli._config.resolve_config``
(D10: CLI flag -> BALDUR_CONFIG -> cwd baldur.toml -> XDG -> env vars),
then call :func:`baldur.init` exactly once before dispatching.

Subcommands share handler functions with the admin server wherever the
surface is already framework-agnostic (``baldur.api.handlers.*``) so there
is a single source of truth for command-level behavior.

Status: Public
"""

from __future__ import annotations

from baldur.cli.app import app

__all__ = ["app"]
