"""``baldur dlq migrate-compressed`` — the operator entry point (723 D4).

The command is a thin wrapper over the framework-agnostic handler, which is
the point: the CLI and the admin HTTP surface must not be able to drift. So
these drive the *whole* seam — command → request context → handler →
``exit_code_for`` — with only the repository behind it stubbed, rather than
asserting that the command called a mocked ``run_handler``.

The exit codes carry operator meaning: 2 means "re-run this" (the sweep held
the lock, or the walk could not be verified), 1 means "this install has no
compressed-entry repository to migrate".
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from unittest.mock import patch

import pytest
import typer

from baldur.cli.commands.dlq import dlq_app, dlq_migrate_compressed_cmd

_REPOSITORY = "baldur.api.handlers.dlq_compressed._repository"
_LOCK = "baldur.dlq.helpers.compressed_lifecycle_lock"
_ENSURE_INIT = "baldur.cli.commands.dlq.ensure_init"


class _StubRepository:
    """Answers the migration with a fixed report."""

    def __init__(self, *, verified: bool = True):
        self.calls: list[dict] = []
        self._verified = verified

    def backfill_compressed_status_index(self, *, operator_initiated=False):
        self.calls.append({"operator_initiated": operator_initiated})
        return {
            "complete": self._verified,
            "mode": "full",
            "walked": 12,
            "added": 5,
            "skipped_unreadable": 0,
            "verified": self._verified,
            "marker_set": self._verified,
        }


@contextmanager
def _lock(acquired: bool):
    yield acquired


def _run() -> int:
    """Invoke the command and return the exit code it raised."""
    with pytest.raises(typer.Exit) as exit_info:
        dlq_migrate_compressed_cmd(None, json_output=True)
    return exit_info.value.exit_code


class TestDlqMigrateCompressedCommand:
    """Registration, the handler seam, and the exit-code contract."""

    def test_command_is_registered_on_the_dlq_app_under_its_documented_name(self):
        """The name is what the runbook and the CHANGELOG tell operators to type."""
        names = {command.name for command in dlq_app.registered_commands}

        assert "migrate-compressed" in names

    def test_successful_run_exits_zero_and_prints_the_report(self, capsys):
        repo = _StubRepository()

        with (
            patch(_ENSURE_INIT),
            patch(_REPOSITORY, return_value=repo),
            patch(_LOCK, lambda session_id: _lock(True)),
        ):
            code = _run()

        assert code == 0
        body = json.loads(capsys.readouterr().out)
        assert body["status"] == "ok"
        assert body["walked"] == 12
        # The command is the operator's judgement that the upgrade finished.
        assert repo.calls == [{"operator_initiated": True}]

    def test_lock_held_by_the_sweep_exits_two(self):
        """Exit 2 is "re-run", not "failed" — the sweep is mid-walk."""
        with (
            patch(_ENSURE_INIT),
            patch(_REPOSITORY, return_value=_StubRepository()),
            patch(_LOCK, lambda session_id: _lock(False)),
        ):
            code = _run()

        assert code == 2

    def test_unverified_walk_exits_two(self):
        with (
            patch(_ENSURE_INIT),
            patch(_REPOSITORY, return_value=_StubRepository(verified=False)),
            patch(_LOCK, lambda session_id: _lock(True)),
        ):
            code = _run()

        assert code == 2

    def test_missing_compressed_repository_exits_one(self):
        """A pure OSS install has no compressed-entry repository at all.

        The handler raises, ``run_handler`` turns that into a 500, and the
        command exits 1 — a framework gap, distinct from the retryable 2.
        """
        with (
            patch(_ENSURE_INIT),
            patch("baldur.factory.registry.ProviderRegistry") as registry,
        ):
            registry.dlq_repository.safe_get.return_value = None
            code = _run()

        assert code == 1
