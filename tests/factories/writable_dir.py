"""Helpers for testing writable-directory resolution.

``resolve_writable_dir`` walks a fallback chain whose steps are real system
locations — the per-user state directory, ``/var/tmp`` and the system temp
directory. A test that lets it reach those would write into the developer's
home directory and would assert against the host's actual permissions, so the
chain is redirected into ``tmp_path`` and unwritable directories are simulated
rather than chmod'd (the POSIX permission tricks do not work on Windows, and
the same contract has to hold on both platforms).

The fixtures live here too, so both the unit and integration trees can
register them from one definition (``tests/unit/conftest.py`` and
``tests/integration/bootstrap/conftest.py`` import them).
"""

from __future__ import annotations

import errno
import os
import tempfile
from pathlib import Path

import pytest

from baldur.utils import fs
from baldur.utils.fs import reset_writable_dir_resolutions

__all__ = [
    "ChainBases",
    "deny_dir",
    "log_events",
    "no_state_dir",
    "spoof_lstat",
    "writable_dir_chain",
]


class ChainBases:
    """The fallback chain bases redirected into one test's ``tmp_path``.

    Attributes:
        state: Stands in for the persistent per-user state directory
            (``$XDG_STATE_HOME`` / ``%LOCALAPPDATA%``) — chain step 1.
        var_tmp: Stands in for the reboot-preserved system temp base — chain
            step 2, POSIX only.
        temp: Stands in for the volatile system temp directory — the last
            chain step.
    """

    def __init__(self, state: Path, var_tmp: Path, temp: Path) -> None:
        self.state = state
        self.var_tmp = var_tmp
        self.temp = temp


def log_events(logs: list[dict], name: str) -> list[dict]:
    """Return the captured ``structlog`` records carrying one event name."""
    return [record for record in logs if record["event"] == name]


def spoof_lstat(monkeypatch, target: Path, *, mode=None, uid=None) -> None:
    """Report ``target`` with a different ``st_mode`` / ``st_uid``.

    The trust check rejects a chain segment that is a symlink or is owned by
    another user. Creating a genuinely foreign-owned or symlinked directory
    needs privileges the suite does not have, so the ``os.lstat`` result is
    rewritten for that one path instead.
    """
    real_lstat = os.lstat

    def fake_lstat(path, *args, **kwargs):
        info = real_lstat(path, *args, **kwargs)
        if Path(path) != target:
            return info
        return os.stat_result(
            (
                info.st_mode if mode is None else mode,
                info.st_ino,
                info.st_dev,
                info.st_nlink,
                info.st_uid if uid is None else uid,
                info.st_gid,
                info.st_size,
                int(info.st_atime),
                int(info.st_mtime),
                int(info.st_ctime),
            )
        )

    monkeypatch.setattr(os, "lstat", fake_lstat)


@pytest.fixture
def writable_dir_chain(monkeypatch, tmp_path) -> ChainBases:
    """Redirect every fallback chain base under ``tmp_path``.

    Also isolates the process-level resolution registry, whose cached entries
    and one-time-warning dedup would otherwise leak between tests. Every test
    that resolves a directory requests this fixture, so the reset rides along
    with it instead of being a tree-wide autouse.
    """
    state = tmp_path / "state"
    var_tmp = tmp_path / "var-tmp"
    temp = tmp_path / "temp"
    for base in (state, var_tmp, temp):
        base.mkdir()

    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    monkeypatch.setenv("LOCALAPPDATA", str(state))
    monkeypatch.setattr(fs, "_VAR_TMP_BASE", str(var_tmp))
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(temp))

    reset_writable_dir_resolutions()
    yield ChainBases(state, var_tmp, temp)
    reset_writable_dir_resolutions()


@pytest.fixture
def no_state_dir(monkeypatch):
    """Remove every variable the persistent state-dir chain step resolves from."""
    for name in ("XDG_STATE_HOME", "HOME", "LOCALAPPDATA"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def deny_dir(monkeypatch):
    """Make directory creation / write probing fail under chosen roots.

    Returns a ``deny(root, error=None, *, op="mkdir")`` callable. ``op`` picks
    which half of the probe fails: ``"mkdir"`` for an uncreatable directory,
    ``"touch"`` for one that exists but rejects writes, ``"both"`` for either.
    """
    real_mkdir = Path.mkdir
    real_touch = Path.touch
    denied: list[tuple[Path, BaseException, str]] = []

    def _match(path: Path, op: str) -> BaseException | None:
        for root, error, scope in denied:
            if scope not in (op, "both"):
                continue
            if path == root or root in path.parents:
                return error
        return None

    def guarded_mkdir(self, *args, **kwargs):
        error = _match(self, "mkdir")
        if error is not None:
            raise error
        return real_mkdir(self, *args, **kwargs)

    def guarded_touch(self, *args, **kwargs):
        error = _match(self, "touch")
        if error is not None:
            raise error
        return real_touch(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", guarded_mkdir)
    monkeypatch.setattr(Path, "touch", guarded_touch)

    def _deny(root, error: BaseException | None = None, *, op: str = "mkdir") -> None:
        denied.append(
            (
                Path(root),
                error or PermissionError(errno.EACCES, "Permission denied"),
                op,
            )
        )

    return _deny
