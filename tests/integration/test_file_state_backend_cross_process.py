"""Two processes writing one key of the file state store at once.

Target: ``baldur.core.state_backend.FileStateBackend`` across process
boundaries — the default single-host store every worker shares. Each write goes
to a temp unique to its writer and is renamed under the key's OS file lock, and
``compare_and_set`` compares and writes inside that lock, so concurrent writers
never interleave bytes into one file and never both win one version (802 G15).

Two real processes (``_file_store_writer.py``) are released at the same instant
against a shared temporary directory:

- ``interleave``: 2 × 100 read-then-CAS attempts on one key. Every later read
  parses, and the stored version equals the number of attempts that committed —
  a lost update or a double win would break the equality.
- ``rounds``: in each round both writers CAS expecting the same version; exactly
  one of them commits.

No infrastructure beyond the local filesystem.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from baldur.core.state_backend import OCC_VERSION_FIELD, FileStateBackend

_WRITER = Path(__file__).with_name("_file_store_writer.py")
_KEY = "system_control"
_ATTEMPTS_PER_WRITER = 100
_ROUNDS = 25
#: Both writers import Baldur first; the start instant leaves room for that.
_START_DELAY_SECONDS = 4.0
_RUN_TIMEOUT_SECONDS = 120.0


def _run_writers(directory: Path, mode: str, count: int, base: int = 0) -> list[dict]:
    start_at = time.time() + _START_DELAY_SECONDS
    env = {k: v for k, v in os.environ.items() if k != "DJANGO_SETTINGS_MODULE"}
    procs = []
    for name in ("writer-a", "writer-b"):
        procs.append(
            subprocess.Popen(
                [sys.executable, str(_WRITER)],
                env={
                    **env,
                    "PYTHONUNBUFFERED": "1",
                    "WRITER_DIR": str(directory),
                    "WRITER_KEY": _KEY,
                    "WRITER_MODE": mode,
                    "WRITER_COUNT": str(count),
                    "WRITER_BASE": str(base),
                    "WRITER_START_AT": repr(start_at),
                    "WRITER_NAME": name,
                },
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
            )
        )
    summaries = []
    for proc in procs:
        out, _ = proc.communicate(timeout=_RUN_TIMEOUT_SECONDS)
        assert proc.returncode == 0
        [line] = [entry for entry in out.splitlines() if entry.startswith("WRITER ")]
        summaries.append(json.loads(line.removeprefix("WRITER ")))
    return summaries


@pytest.fixture
def store_dir(tmp_path) -> Path:
    directory = tmp_path / "shared_state"
    directory.mkdir()
    return directory


class TestFileStateBackendCrossProcessBehavior:
    """Concurrent writers in two processes keep one key readable and consistent."""

    def test_interleaved_writes_leave_the_key_readable_with_no_lost_update(
        self, store_dir
    ):
        """200 racing CAS attempts: the stored version is exactly the commit count."""
        # When
        summaries = _run_writers(store_dir, "interleave", _ATTEMPTS_PER_WRITER)

        # Then
        assert [s["errors"] for s in summaries] == [[], []]
        committed = sum(s["committed"] for s in summaries)
        stored = FileStateBackend(store_dir).get_strict(_KEY)
        assert stored is not None
        assert stored[OCC_VERSION_FIELD] == committed
        assert committed >= _ATTEMPTS_PER_WRITER
        assert list(store_dir.glob("*.partial")) == []
        assert list(store_dir.glob("*.tmp")) == []

    def test_exactly_one_of_two_writers_based_on_one_version_commits(self, store_dir):
        """Every round has one winner — never two, never none."""
        # Given: the key at version 7
        FileStateBackend(store_dir).set(_KEY, {"seed": True, OCC_VERSION_FIELD: 7})

        # When
        summaries = _run_writers(store_dir, "rounds", _ROUNDS, base=7)

        # Then
        assert [s["errors"] for s in summaries] == [[], []]
        winners_per_round = [
            int(a) + int(b)
            for a, b in zip(summaries[0]["rounds"], summaries[1]["rounds"], strict=True)
        ]
        assert winners_per_round == [1] * _ROUNDS
        stored = FileStateBackend(store_dir).get_strict(_KEY)
        assert stored[OCC_VERSION_FIELD] == 7 + _ROUNDS
