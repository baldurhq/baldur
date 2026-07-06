"""CLI bootstrap helper - ``ensure_init`` lives here as a leaf module.

Relocated out of ``cli/app.py`` so it imports no ``cli`` sibling: ``app.py``
imports the command modules at module level (to register their subcommands)
while every command module needs ``ensure_init``. With ``ensure_init`` defined
back on ``app`` that formed an import-time hub: ``app`` -> command module ->
``app``. Hosting it on a typer-only leaf module removes the back-edge and
dissolves the cycle (enforced by the import-cycle fitness gate).

Kept separate from ``cli/_invoke`` deliberately: ``_invoke`` owns handler
invocation + output formatting, whereas bootstrap orchestration (running
``baldur.init()`` once per process) is a distinct concern.
"""

from __future__ import annotations

import typer


def ensure_init(ctx: typer.Context) -> None:
    """Invoke ``baldur.init()`` once for this CLI process.

    Idempotent at the framework level (``bootstrap.py:_init_done``) but
    we still track it on the typer context to skip the lock round-trip
    on repeated subcommand invocations within a composite command.
    """
    ctx.ensure_object(dict)
    if ctx.obj.get("init_done"):
        return
    from baldur.bootstrap import init

    init()
    ctx.obj["init_done"] = True
