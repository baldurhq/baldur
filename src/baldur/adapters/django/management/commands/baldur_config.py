"""
Thin shim — ``python manage.py baldur_config`` -> ``baldur check-config --inspect``.

The canonical implementation lives under :mod:`baldur.cli.commands.check_config`
(``baldur check-config --inspect`` / ``--json``). This shim exists only
for backwards compatibility.
"""

from __future__ import annotations

import sys

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "(DEPRECATED) Run `baldur check-config --inspect` — settings inspection."

    def add_arguments(self, parser):
        parser.add_argument(
            "--inspect",
            action="store_true",
            help="Display all currently loaded settings.",
        )
        parser.add_argument(
            "--validate",
            action="store_true",
            help="Validate settings (same as --inspect without dump).",
        )
        parser.add_argument(
            "--format",
            choices=["text", "json", "table"],
            default="text",
            help="Output format — text/json only in new CLI (table deprecated).",
        )
        parser.add_argument(
            "--config-type",
            type=str,
            default=None,
            help="Limit inspection to one settings section.",
        )

    def handle(self, *args, **options):
        argv = ["check-config"]
        if options["format"] == "json":
            argv.append("--json")
        if options["inspect"] or not options["validate"]:
            argv.append("--inspect")
        if options.get("config_type"):
            argv.extend(["--config-type", options["config_type"]])

        sys.exit(_run_cli(argv))


def _run_cli(argv: list[str]) -> int:
    from baldur.cli import app

    try:
        app(args=argv, standalone_mode=False)
        return 0
    except SystemExit as exc:
        return int(exc.code or 0)
