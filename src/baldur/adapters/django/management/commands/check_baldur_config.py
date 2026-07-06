"""
Thin shim — ``python manage.py check_baldur_config`` -> ``baldur check-config``.

The canonical implementation lives under :mod:`baldur.cli.commands.check_config`
so the Django management command and the framework-free CLI share exactly
one code path (429 Part 7 CLI migration). This shim exists only for
backwards compatibility with existing CI pipelines.
"""

from __future__ import annotations

import sys

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "(DEPRECATED) Run `baldur check-config` — Baldur preflight validation."

    def add_arguments(self, parser):
        parser.add_argument(
            "--strict",
            action="store_true",
            help="Exit 1 when fatal violations exist (CI/CD hard block).",
        )
        parser.add_argument(
            "--json",
            action="store_true",
            help="Emit JSON instead of human-readable text.",
        )

    def handle(self, *args, **options):
        argv = ["check-config"]
        if options["strict"]:
            argv.append("--strict")
        if options["json"]:
            argv.append("--json")

        sys.exit(_run_cli(argv))


def _run_cli(argv: list[str]) -> int:
    """Invoke the framework-free CLI and convert typer's SystemExit to an int."""
    from baldur.cli import app

    try:
        app(args=argv, standalone_mode=False)
        return 0
    except SystemExit as exc:
        return int(exc.code or 0)
