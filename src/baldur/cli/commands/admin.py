"""
``baldur admin`` - start the admin HTTP server in the foreground.

Runs ``baldur.init()``, then :func:`baldur.start_admin_server`, then
blocks on the admin server thread until Ctrl-C. This matches the
Prometheus ``prometheus ...`` / Gunicorn foreground model - PID 1 is the
admin server, K8s / systemd handles restart semantics.

When ``BALDUR_ADMIN_AUTOSTART=1`` (the default), ``init()`` already
starts the server in the background. Running ``baldur admin`` still
works: ``start_admin_server`` is idempotent and returns the running
instance.
"""

from __future__ import annotations

import signal
import threading

import typer

from baldur.cli._bootstrap import ensure_init


def admin(
    ctx: typer.Context,
    port: int | None = typer.Option(
        None,
        "--port",
        "-p",
        help="Override BALDUR_ADMIN_PORT.",
    ),
    bind: str | None = typer.Option(
        None,
        "--bind",
        "-b",
        help="Override BALDUR_ADMIN_BIND (default 127.0.0.1).",
    ),
) -> None:
    """Start the admin server and run in the foreground."""
    from baldur.api.admin import start_admin_server, stop_admin_server

    ensure_init(ctx)
    server = start_admin_server(port=port, bind=bind)
    typer.secho(
        f"Baldur admin server listening on {server.settings.bind}:{server.settings.port}",
        fg=typer.colors.GREEN,
    )

    stop_event = threading.Event()

    def _handle_signal(signum, frame):  # noqa: ANN001
        typer.echo(f"\nReceived signal {signum}, shutting down admin server...")
        stop_event.set()

    signal.signal(signal.SIGINT, _handle_signal)
    try:
        signal.signal(signal.SIGTERM, _handle_signal)
    except (AttributeError, ValueError):
        # SIGTERM is unavailable on Windows when not in the main thread.
        pass

    try:
        while not stop_event.is_set():
            stop_event.wait(timeout=1.0)
    finally:
        stop_admin_server(timeout=5.0)
        typer.echo("Admin server stopped.")
