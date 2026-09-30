"""Which state store the switch state lives in, and what a derived Redis store warns.

Target: ``baldur.core.state_backend._create_state_backend`` — builds the store
the settings select (file at the configured directory, Redis on the resolved
URL with its prefix and scan bounds, memory), and, when the Redis store was
derived rather than set, warns once that state left in the file directory is
not migrated.

Verification techniques applied (§8):
  - §8.12 Branch outcome — each backend branch, and the strand warning's
    derived × stranded conditions
  - §8.5 Dependency interaction — the Redis store receives the settings'
    resolved URL (the validator's fallback, no inline default) and bounds
  - §8.4 Side effects — one WARNING naming the absolute directory
  - §8.10 Singleton — the store is built once, so the warning fires once
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from structlog.testing import capture_logs

from baldur.core.state_backend import (
    FileStateBackend,
    MemoryStateBackend,
    _create_state_backend,
    get_state_backend,
    reset_state_backend,
)
from baldur.settings.redis import get_redis_settings, reset_redis_settings
from baldur.settings.system_control import (
    get_system_control_settings,
    reset_system_control_settings,
)

_STRANDED_EVENT = "state_backend.file_state_not_migrated"


@pytest.fixture(autouse=True)
def _clean_backend_env(monkeypatch):
    """Every test names its own backend and URLs; nothing leaks in or out."""
    for name in (
        "BALDUR_SYSTEM_CONTROL_BACKEND",
        "BALDUR_SYSTEM_CONTROL_REDIS_URL",
        "BALDUR_REDIS_URL",
        "BALDUR_SYSTEM_CONTROL_DIR",
    ):
        monkeypatch.delenv(name, raising=False)
    reset_system_control_settings()
    reset_redis_settings()
    reset_state_backend()
    yield
    reset_system_control_settings()
    reset_redis_settings()
    reset_state_backend()


def _use(monkeypatch, **env: str) -> None:
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    reset_system_control_settings()


def _stranded_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "file_state"
    directory.mkdir()
    (directory / "system_control.json").write_text('{"enabled": false}')
    return directory


class TestStateBackendSelectionBehavior:
    """The settings' backend decides the store; a derived Redis warns once."""

    def test_file_backend_is_built_at_the_configured_directory(
        self, monkeypatch, tmp_path
    ):
        """No Redis named → the file store, at ``BALDUR_SYSTEM_CONTROL_DIR``."""
        _use(monkeypatch, BALDUR_SYSTEM_CONTROL_DIR=str(tmp_path / "state"))

        backend = _create_state_backend()

        assert isinstance(backend, FileStateBackend)
        assert backend.directory == str((tmp_path / "state").resolve())

    def test_memory_backend_is_built_when_set(self, monkeypatch):
        """An explicit ``memory`` backend builds the in-process store."""
        _use(monkeypatch, BALDUR_SYSTEM_CONTROL_BACKEND="memory")

        assert isinstance(_create_state_backend(), MemoryStateBackend)

    def test_derived_redis_backend_receives_the_named_url_and_bounds(
        self, monkeypatch, tmp_path
    ):
        """A named Redis URL builds the Redis store on that URL."""
        # Given
        _use(
            monkeypatch,
            BALDUR_REDIS_URL="redis://state-store:6379/4",
            BALDUR_SYSTEM_CONTROL_DIR=str(tmp_path / "unused"),
        )
        settings = get_system_control_settings()

        # When
        with patch(
            "baldur.core.state_backend.RedisStateBackend", autospec=True
        ) as redis_cls:
            backend = _create_state_backend()

        # Then
        redis_cls.assert_called_once_with(
            redis_url="redis://state-store:6379/4",
            key_prefix=settings.redis_key_prefix,
            scan_batch_size=settings.redis_scan_batch_size,
            max_scan_keys=settings.redis_max_scan_keys,
        )
        assert backend is redis_cls.return_value

    def test_explicit_redis_without_a_url_dials_the_redis_settings_default(
        self, monkeypatch, tmp_path
    ):
        """No inline URL: the validator's fallback (``RedisSettings.url``) is passed."""
        _use(
            monkeypatch,
            BALDUR_SYSTEM_CONTROL_BACKEND="redis",
            BALDUR_SYSTEM_CONTROL_DIR=str(tmp_path / "unused"),
        )

        with patch(
            "baldur.core.state_backend.RedisStateBackend", autospec=True
        ) as redis_cls:
            _create_state_backend()

        assert redis_cls.call_args.kwargs["redis_url"] == get_redis_settings().url

    @pytest.mark.parametrize(
        ("env", "stranded", "expected_warnings"),
        [
            ({"BALDUR_REDIS_URL": "redis://state-store:6379/0"}, True, 1),
            ({"BALDUR_REDIS_URL": "redis://state-store:6379/0"}, False, 0),
            (
                {
                    "BALDUR_SYSTEM_CONTROL_BACKEND": "redis",
                    "BALDUR_REDIS_URL": "redis://state-store:6379/0",
                },
                True,
                0,
            ),
        ],
        ids=["derived_and_stranded", "derived_empty_dir", "explicit_redis"],
    )
    def test_strand_warning_only_for_a_derived_redis_over_stored_files(
        self, monkeypatch, tmp_path, env, stranded, expected_warnings
    ):
        """Only a derivation can move a deployment off its file store unasked."""
        # Given
        directory = _stranded_dir(tmp_path) if stranded else tmp_path / "missing"
        _use(monkeypatch, BALDUR_SYSTEM_CONTROL_DIR=str(directory), **env)

        # When
        with (
            patch("baldur.core.state_backend.RedisStateBackend", autospec=True),
            capture_logs() as logs,
        ):
            _create_state_backend()

        # Then
        warnings = [log for log in logs if log["event"] == _STRANDED_EVENT]
        assert len(warnings) == expected_warnings

    def test_strand_warning_names_the_absolute_directory(self, monkeypatch, tmp_path):
        """The WARNING says where the unread state is, as an absolute path."""
        # Given: a relative directory resolved against the working directory
        monkeypatch.chdir(tmp_path)
        _stranded_dir(tmp_path)
        _use(
            monkeypatch,
            BALDUR_REDIS_URL="redis://state-store:6379/0",
            BALDUR_SYSTEM_CONTROL_DIR="file_state",
        )

        # When
        with (
            patch("baldur.core.state_backend.RedisStateBackend", autospec=True),
            capture_logs() as logs,
        ):
            _create_state_backend()

        # Then
        [warning] = [log for log in logs if log["event"] == _STRANDED_EVENT]
        assert warning["log_level"] == "warning"
        assert warning["directory"] == str((tmp_path / "file_state").resolve())

    def test_strand_warning_fires_once_per_process_store(self, monkeypatch, tmp_path):
        """The store is a singleton, so repeated lookups do not repeat the WARNING."""
        _use(
            monkeypatch,
            BALDUR_REDIS_URL="redis://state-store:6379/0",
            BALDUR_SYSTEM_CONTROL_DIR=str(_stranded_dir(tmp_path)),
        )

        with (
            patch("baldur.core.state_backend.RedisStateBackend", autospec=True),
            capture_logs() as logs,
        ):
            get_state_backend()
            get_state_backend()

        assert [log["event"] for log in logs].count(_STRANDED_EVENT) == 1
