"""File state store: writer-unique temps, dead-writer cleanup, the per-key lock.

Target: ``baldur.core.state_backend.FileStateBackend`` — every write goes to a
temp name unique to the writing process and thread (``<key>.<pid>-<tid>.partial``)
and is renamed onto ``<key>.json`` under the key's cross-process lock; a temp
left by a writer that died is removed at construction and never promoted; the
previous release's ``*.tmp`` files are neither created nor touched; a rename
that collides with a brief Windows sharing lock is retried; the store reports
its directory as an absolute path.

Verification techniques applied (§8):
  - §8.2 Exception/edge cases — dead-writer temps, legacy ``*.tmp`` files,
    names that are not writer temps, a lock that cannot be taken, a failed rename
  - §8.12 Branch outcome — the rename retry succeeds after a clash, and raises
    once its attempts run out
  - §8.4 Side effects — no temp survives a write, the construction INFO line
    names the absolute directory
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from unittest.mock import patch

import pytest
from structlog.testing import capture_logs

from baldur.core import state_backend as state_backend_module
from baldur.core.state_backend import FileStateBackend
from tests.factories.time_helpers import mock_sleep

KEY = "system_control"
DOTTED_KEY = "chaos:running:exp.1"


def _encoded(key: str) -> str:
    return FileStateBackend._encode_key_for_filename(
        FileStateBackend.__new__(FileStateBackend), key
    )


def _dead_writer_temp(directory: Path, key: str, value: str = '{"enabled": false}'):
    """A temp exactly as a writer that died between write and rename leaves it."""
    temp = directory / f"{_encoded(key)}.4242-777.partial"
    temp.write_text(value, encoding="utf-8")
    return temp


class _RecordingFileStateBackend(FileStateBackend):
    """File store that records the temp each write renames from."""

    renamed_from: list[str] = []

    @staticmethod
    def _replace_with_retry(source: Path, target: Path) -> None:
        _RecordingFileStateBackend.renamed_from.append(source.name)
        FileStateBackend._replace_with_retry(source, target)


@pytest.fixture
def store_dir(tmp_path) -> Path:
    directory = tmp_path / "state"
    directory.mkdir()
    return directory


class TestFileStateBackendTempFilesBehavior:
    """Temps unique to their writer, removed when dead, never promoted (G15)."""

    def test_write_renames_from_a_temp_named_for_this_process_and_thread(
        self, store_dir
    ):
        """The temp carries ``<pid>-<thread id>`` and the ``.partial`` suffix."""
        # Given
        _RecordingFileStateBackend.renamed_from = []
        backend = _RecordingFileStateBackend(store_dir)

        # When
        backend.set(KEY, {"enabled": False})

        # Then
        expected = (
            f"{_encoded(KEY)}.{os.getpid()}-{threading.get_ident()}"
            f"{state_backend_module._PARTIAL_SUFFIX}"
        )
        assert _RecordingFileStateBackend.renamed_from == [expected]

    def test_write_leaves_no_temp_and_creates_no_tmp_file(self, store_dir):
        """After a write only the state file and its lock file remain."""
        backend = FileStateBackend(store_dir)

        backend.set(KEY, {"enabled": False})
        backend.compare_and_set(KEY, 0, {"enabled": True, "__occ_version__": 1})

        names = sorted(p.name for p in store_dir.iterdir())
        assert names == [f"{KEY}.json", f"{KEY}{state_backend_module._LOCK_SUFFIX}"]

    def test_failed_rename_removes_its_own_temp_and_raises(self, store_dir):
        """A write that cannot land cleans up after itself and fails loud."""
        backend = FileStateBackend(store_dir)

        with (
            patch.object(
                FileStateBackend,
                "_replace_with_retry",
                side_effect=OSError("disk full"),
            ),
            pytest.raises(OSError, match="disk full"),
        ):
            backend.set(KEY, {"enabled": False})

        assert list(store_dir.glob("*.partial")) == []
        assert backend.get_strict(KEY) is None

    @pytest.mark.parametrize("key", [KEY, DOTTED_KEY], ids=["plain", "dotted"])
    def test_construction_removes_a_dead_writer_temp_without_promoting_it(
        self, store_dir, key
    ):
        """A value its writer never confirmed never becomes state."""
        # Given: a writer died after writing its temp, before the rename
        temp = _dead_writer_temp(store_dir, key)

        # When
        backend = FileStateBackend(store_dir)

        # Then
        assert not temp.exists()
        assert backend.get_strict(key) is None
        assert backend.get_all("*") == {}

    def test_construction_never_promotes_a_temp_over_newer_state(self, store_dir):
        """A dead writer's temp beside a stored value leaves the stored value alone."""
        FileStateBackend(store_dir).set(KEY, {"enabled": True, "__occ_version__": 3})
        temp = _dead_writer_temp(store_dir, KEY, '{"enabled": false}')

        backend = FileStateBackend(store_dir)

        assert not temp.exists()
        assert backend.get_strict(KEY) == {"enabled": True, "__occ_version__": 3}

    def test_deleted_key_never_reappears_from_a_dead_writer_temp(self, store_dir):
        """A finished experiment's key stays deleted across a restart."""
        # Given: the key was written, a later write died mid-way, then it was deleted
        first = FileStateBackend(store_dir)
        first.set(DOTTED_KEY, {"status": "running"})
        _dead_writer_temp(store_dir, DOTTED_KEY, '{"status": "running"}')
        first.delete(DOTTED_KEY)

        # When
        restarted = FileStateBackend(store_dir)

        # Then
        assert restarted.get_all("chaos:running:*") == {}
        assert not any("4242" in key for key in restarted.get_all("*"))

    @pytest.mark.parametrize(
        "name",
        ["notes.partial", f"{KEY}.writer.partial", f"{KEY}.12-ab.partial"],
        ids=["no_writer_part", "non_numeric_writer", "partial_numeric_writer"],
    )
    def test_construction_leaves_names_that_are_not_writer_temps(self, store_dir, name):
        """Only ``<key>.<pid>-<tid>.partial`` names are this store's temps."""
        stranger = store_dir / name
        stranger.write_text("kept", encoding="utf-8")

        FileStateBackend(store_dir)

        assert stranger.read_text(encoding="utf-8") == "kept"

    def test_previous_release_tmp_file_is_neither_promoted_nor_touched(self, store_dir):
        """A legacy ``<key>.tmp`` belongs to a previous-release writer: left as is."""
        # Given
        legacy = store_dir / f"{KEY}.tmp"
        legacy.write_text('{"enabled": false}', encoding="utf-8")
        before = legacy.stat().st_mtime_ns

        # When
        backend = FileStateBackend(store_dir)
        missing = backend.get(KEY)
        backend.set("other", {"x": 1})

        # Then
        assert missing is None
        assert legacy.read_text(encoding="utf-8") == '{"enabled": false}'
        assert legacy.stat().st_mtime_ns == before
        assert sorted(p.name for p in store_dir.glob("*.tmp")) == [f"{KEY}.tmp"]


class TestFileStateBackendRenameRetryBehavior:
    """A rename onto a file another process holds open is retried briefly."""

    def test_rename_clash_then_success_lands_the_write(self, store_dir):
        """Two sharing clashes, then the rename lands; the write succeeds."""
        # Given
        backend = FileStateBackend(store_dir)
        real_replace = Path.replace
        outcomes = [PermissionError("in use"), PermissionError("in use")]

        def flaky_replace(self, target):
            if outcomes:
                raise outcomes.pop(0)
            return real_replace(self, target)

        # When
        with patch.object(Path, "replace", flaky_replace), mock_sleep() as slept:
            backend.set(KEY, {"enabled": False})

        # Then
        assert backend.get_strict(KEY) == {"enabled": False}
        assert slept.call_count == 2

    def test_rename_clash_on_every_attempt_raises_permission_error(self, store_dir):
        """The retry is bounded: the last clash propagates, no temp remains."""
        backend = FileStateBackend(store_dir)
        attempts: list[str] = []

        def always_clash(self, target):
            attempts.append(self.name)
            raise PermissionError("in use")

        with (
            patch.object(Path, "replace", always_clash),
            mock_sleep(),
            pytest.raises(PermissionError),
        ):
            backend.set(KEY, {"enabled": False})

        assert len(attempts) == state_backend_module._RENAME_RETRY_ATTEMPTS
        assert list(store_dir.glob("*.partial")) == []


class TestFileStateBackendKeyLockBehavior:
    """Every access of a key holds its lock; a lock not taken writes nothing."""

    def test_lock_timeout_fails_the_write_before_anything_is_written(self, store_dir):
        """The lock is taken before the temp is opened, so a timeout leaves no trace."""
        backend = FileStateBackend(store_dir)

        with (
            patch(
                "baldur.audit.checkpoint.file_lock.lock_file",
                side_effect=OSError("lock timeout"),
            ),
            pytest.raises(OSError, match="lock timeout"),
        ):
            backend.set(KEY, {"enabled": False})

        assert not (store_dir / f"{KEY}.json").exists()
        assert list(store_dir.glob("*.partial")) == []

    def test_lock_timeout_on_read_raises_strictly_and_reads_default_tolerantly(
        self, store_dir
    ):
        """The strict read surfaces the lock failure; ``get`` keeps its default."""
        backend = FileStateBackend(store_dir)
        backend.set(KEY, {"enabled": False})

        with patch(
            "baldur.audit.checkpoint.file_lock.lock_file",
            side_effect=OSError("lock timeout"),
        ):
            assert backend.get(KEY, {"default": True}) == {"default": True}
            with pytest.raises(OSError, match="lock timeout"):
                backend.get_strict(KEY)

    def test_lock_file_persists_and_is_never_read_as_a_key(self, store_dir):
        """The per-key lock file stays beside the state file and is not state."""
        backend = FileStateBackend(store_dir)
        backend.set(KEY, {"enabled": False})
        backend.delete(KEY)

        assert (store_dir / f"{KEY}{state_backend_module._LOCK_SUFFIX}").exists()
        assert backend.get_all("*") == {}


class TestFileStateBackendDirectoryBehavior:
    """The store names its directory as an absolute path."""

    def test_relative_directory_is_reported_resolved_against_the_working_directory(
        self, tmp_path, monkeypatch
    ):
        """Two processes started from different directories can tell their stores apart."""
        monkeypatch.chdir(tmp_path)

        backend = FileStateBackend("relative/state")

        assert backend.directory == str((tmp_path / "relative" / "state").resolve())

    def test_construction_logs_the_absolute_directory(self, tmp_path, monkeypatch):
        """A process with no status route still shows which store it uses."""
        monkeypatch.chdir(tmp_path)

        with capture_logs() as logs:
            FileStateBackend("relative/state")

        initialized = [
            log
            for log in logs
            if log["event"] == "state_backend.file_backend_initialized"
        ]
        assert [log["directory"] for log in initialized] == [
            str((tmp_path / "relative" / "state").resolve())
        ]
