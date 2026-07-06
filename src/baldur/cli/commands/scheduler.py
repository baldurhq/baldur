"""
``baldur scheduler list`` - introspect registered scheduled jobs.

Does not start the scheduler thread; ``baldur.init()`` already handles
that (honoring ``BALDUR_SCHEDULER_AUTOSTART``). The command reads the
singleton ``LeaderScheduler``'s ``jobs`` property - a plain dict copy,
no I/O.
"""

from __future__ import annotations

import json

import typer

from baldur.cli._bootstrap import ensure_init

scheduler_app = typer.Typer(
    name="scheduler",
    help="Scheduled job introspection.",
    no_args_is_help=True,
)


@scheduler_app.command("list")
def scheduler_list_cmd(
    ctx: typer.Context,
    json_output: bool = typer.Option(
        False, "--json", help="Emit JSON instead of pretty text."
    ),
) -> None:
    """List registered scheduled jobs (name, interval, last run, run count)."""
    ensure_init(ctx)

    from baldur.coordination.scheduler import get_leader_scheduler

    scheduler = get_leader_scheduler()
    jobs = scheduler.jobs
    is_leader = scheduler.is_leader

    rows = []
    for name, job in jobs.items():
        rows.append(
            {
                "name": name,
                "interval_seconds": job.interval_seconds,
                "enabled": job.enabled,
                "run_count": job.run_count,
                "error_count": job.error_count,
                "last_run": job.last_run.isoformat() if job.last_run else None,
            }
        )

    payload = {
        "is_leader": is_leader,
        "job_count": len(rows),
        "jobs": rows,
    }

    if json_output:
        typer.echo(json.dumps(payload, indent=2, ensure_ascii=False))
        return

    typer.echo(f"Leader: {is_leader}")
    typer.echo(f"Registered jobs: {len(rows)}")
    if not rows:
        return

    for row in rows:
        status_symbol = "ok" if row["error_count"] == 0 else "errors"
        typer.echo(
            f"  [{status_symbol}] {row['name']:<40} "
            f"interval={row['interval_seconds']:>6}s  "
            f"runs={row['run_count']:<4}  "
            f"errors={row['error_count']:<3}  "
            f"last_run={row['last_run'] or '-'}"
        )
