"""
Typer application root - wires subcommands and a global ``--config`` /
``--env-file`` bootstrap callback.

The callback runs before any subcommand callback so ``baldur.init()``
sees the TOML-derived env vars. Subcommands that do not need init
(e.g. ``check-config``'s TOML inspection) may opt out via a typer
parameter; by default the root callback invokes init exactly once.
"""

from __future__ import annotations

import typer

from baldur.cli._config import (
    apply_config_to_env,
    load_dotenv_if_requested,
    resolve_config,
)

app = typer.Typer(
    name="baldur",
    help="Baldur - Self-Healing framework CLI.",
    no_args_is_help=True,
    add_completion=False,
)


@app.callback()
def _root(
    ctx: typer.Context,
    config: str | None = typer.Option(
        None,
        "--config",
        "-c",
        help="Path to baldur.toml. Overrides BALDUR_CONFIG and auto-detect.",
        metavar="PATH",
    ),
    env_file: str | None = typer.Option(
        None,
        "--env-file",
        help=(
            "Load a .env file before init(). Never silent - must be "
            "explicit (or set BALDUR_DOTENV=1 for cwd/.env)."
        ),
        metavar="PATH",
    ),
) -> None:
    """Resolve config chain and store context for subcommands.

    Subcommands call :func:`baldur.cli._bootstrap.ensure_init` when they need
    ``baldur.init()`` to have run (most do; ``check-config`` does not).
    """
    resolution = resolve_config(config)
    apply_config_to_env(resolution)
    load_dotenv_if_requested(env_file)

    ctx.ensure_object(dict)
    ctx.obj["config_resolution"] = resolution
    ctx.obj["init_done"] = False


# Subcommand modules are imported at module level after the root
# callback is defined so ``app`` exists when each module registers its
# commands. Each subcommand module handles heavy imports (handlers,
# scheduler, admin server) lazily inside its function bodies, so
# importing ``baldur.cli`` itself stays cheap.
from baldur.cli.commands import admin as _admin_cmd  # noqa: E402
from baldur.cli.commands import cb as _cb_cmd  # noqa: E402
from baldur.cli.commands import check_config as _check_config_cmd  # noqa: E402
from baldur.cli.commands import dlq as _dlq_cmd  # noqa: E402
from baldur.cli.commands import escalation as _escalation_cmd  # noqa: E402
from baldur.cli.commands import init_ai as _init_ai_cmd  # noqa: E402
from baldur.cli.commands import report as _report_cmd  # noqa: E402
from baldur.cli.commands import scheduler as _scheduler_cmd  # noqa: E402
from baldur.cli.commands import security_review as _security_review_cmd  # noqa: E402

app.command("check-config")(_check_config_cmd.check_config)
app.command("init-ai")(_init_ai_cmd.init_ai)
app.command("admin")(_admin_cmd.admin)
app.command("report")(_report_cmd.report)
app.command("security-review")(_security_review_cmd.security_review)
app.add_typer(_dlq_cmd.dlq_app, name="dlq")  # type: ignore[has-type]
app.add_typer(_cb_cmd.cb_app, name="cb")  # type: ignore[has-type]
app.add_typer(_scheduler_cmd.scheduler_app, name="scheduler")  # type: ignore[has-type]
app.add_typer(_escalation_cmd.escalation_app, name="escalation")  # type: ignore[has-type]
