"""Resilient-storage WAL directory resolution and the production boot gate.

On the shipped default (``/var/log/baldur/wal``) a non-root install used to log
``resilient_storage.wal_init_failed`` at ERROR with a full traceback and then
run with no WAL protection at all. Resolution now lives in ``WriteAheadLog``,
so the default relocates and the WAL actually initializes.

The production boot gate keeps its exact prior meaning — production refuses to
start unless the WAL is on its *configured* directory — but now reads a
predicate that says so, which a fallback deliberately does not satisfy. Runtime
write paths keep using ``_wal_initialized``: a fallback WAL is a working WAL.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from structlog.testing import capture_logs

from baldur.adapters.resilient.backend import (
    WAL_DIR_ENV_VAR,
    ResilientStorageBackend,
)
from baldur.core.exceptions import ConfigurationError
from baldur.runtime import BaldurRuntime
from baldur.settings.resilient_storage import ResilientStorageSettings
from tests.factories.writable_dir import log_events

DEFAULT_WAL_DIR = ResilientStorageSettings.model_fields["wal_dir"].default


class TestResilientSettingsOriginContract:
    """``model_fields_set`` is what the origin split reads."""

    def test_defaults_only_settings_leave_wal_dir_unset(self):
        """Design contract: an untouched default is not an operator choice."""
        settings = ResilientStorageSettings()

        assert "wal_dir" not in settings.model_fields_set

    def test_explicit_wal_dir_marks_the_field_as_set(self):
        """Design contract: a supplied directory is an operator choice."""
        settings = ResilientStorageSettings(wal_dir="/srv/wal")

        assert "wal_dir" in settings.model_fields_set

    def test_env_supplied_wal_dir_marks_the_field_as_set(self, monkeypatch):
        """Design contract: the env var is operator input like a kwarg is."""
        monkeypatch.setenv(WAL_DIR_ENV_VAR, "/srv/wal")

        settings = ResilientStorageSettings()

        assert "wal_dir" in settings.model_fields_set

    def test_default_wal_dir_is_the_posix_baldur_path(self):
        """Design contract: the shipped default the fallback chain protects."""
        assert DEFAULT_WAL_DIR == "/var/log/baldur/wal"


class TestResilientWALDirFallbackBehavior:
    """``_init_wal`` catch split and the fallback predicate."""

    def test_unwritable_default_dir_initializes_the_wal_on_a_fallback(
        self, writable_dir_chain, deny_dir
    ):
        """Degraded-mode writes now get the WAL protection they never had."""
        deny_dir(Path(DEFAULT_WAL_DIR))

        backend = ResilientStorageBackend(settings=ResilientStorageSettings())

        assert backend._wal_initialized is True
        assert backend._wal_on_fallback_dir is True

    def test_unwritable_default_dir_logs_no_error_and_no_traceback(
        self, writable_dir_chain, deny_dir
    ):
        """Negative: the first-touch ERROR traceback must be gone."""
        deny_dir(Path(DEFAULT_WAL_DIR))

        with capture_logs() as logs:
            ResilientStorageBackend(settings=ResilientStorageSettings())

        assert [r for r in logs if r["log_level"] in ("error", "critical")] == []
        assert log_events(logs, "resilient_storage.wal_init_failed") == []

    def test_unwritable_operator_set_dir_warns_without_a_traceback(
        self, writable_dir_chain, deny_dir, tmp_path
    ):
        """An expected, handled condition is a WARNING, not an exception log."""
        chosen = tmp_path / "compliance-wal"
        deny_dir(chosen)

        with capture_logs() as logs:
            backend = ResilientStorageBackend(
                settings=ResilientStorageSettings(wal_dir=str(chosen))
            )

        assert backend._wal_initialized is False
        records = log_events(logs, "resilient_storage.wal_init_failed")
        assert len(records) == 1
        assert records[0]["log_level"] == "warning"
        assert records[0]["override_env"] == WAL_DIR_ENV_VAR
        assert "exc_info" not in records[0]

    def test_unexpected_wal_failure_keeps_the_exception_log(
        self, writable_dir_chain, tmp_path
    ):
        """The unexpected branch must stay loud with its traceback."""
        settings = ResilientStorageSettings(wal_dir=str(tmp_path / "wal"))

        with patch(
            "baldur.audit.wal.WriteAheadLog",
            autospec=True,
            side_effect=RuntimeError("unexpected"),
        ):
            with capture_logs() as logs:
                backend = ResilientStorageBackend(settings=settings)

        assert backend._wal_initialized is False
        records = log_events(logs, "resilient_storage.wal_init_failed")
        assert len(records) == 1
        assert records[0]["log_level"] == "error"

    def test_writable_configured_dir_is_not_marked_as_a_fallback(
        self, writable_dir_chain, tmp_path
    ):
        """Negative: the healthy path must not look like a fallback."""
        settings = ResilientStorageSettings(wal_dir=str(tmp_path / "wal"))

        backend = ResilientStorageBackend(settings=settings)

        assert backend._wal_initialized is True
        assert backend._wal_on_fallback_dir is False

    @pytest.mark.parametrize(
        ("initialized", "on_fallback", "expected"),
        [
            (True, False, True),
            (True, True, False),
            (False, False, False),
            (False, True, False),
        ],
        ids=["configured_dir", "fallback_dir", "wal_dead", "wal_dead_after_fallback"],
    )
    def test_honors_configured_dir_requires_both_initialized_and_no_fallback(
        self, writable_dir_chain, tmp_path, initialized, on_fallback, expected
    ):
        """The boot gate's predicate is stricter than ``_wal_initialized`` alone."""
        backend = ResilientStorageBackend(
            settings=ResilientStorageSettings(wal_dir=str(tmp_path / "wal"))
        )
        backend._wal_initialized = initialized
        backend._wal_on_fallback_dir = on_fallback

        assert backend._wal_honors_configured_dir is expected

    def test_get_stats_reports_the_fallback_alongside_initialization(
        self, writable_dir_chain, deny_dir
    ):
        """Operators need to see 'working but relocated' as its own state."""
        deny_dir(Path(DEFAULT_WAL_DIR))
        backend = ResilientStorageBackend(settings=ResilientStorageSettings())

        stats = backend.get_stats()

        assert stats["wal_initialized"] is True
        assert stats["wal_on_fallback_dir"] is True


class TestProductionWALBootGateBehavior:
    """``_install_resilient_storage_backend``'s fail-closed durability promise."""

    @pytest.fixture
    def install_backend(self, monkeypatch):
        """Run the installer against a chosen runtime mode.

        Returns an ``install(is_production=...)`` callable that yields the
        backend the installer constructed. ``configure_storage_backend`` is
        stubbed — installing a real backend into the process-global registry
        would leak into sibling tests — and doubles as the capture seam.
        """
        from baldur import bootstrap

        def _install(*, is_production: bool) -> ResilientStorageBackend:
            runtime = MagicMock(spec=BaldurRuntime)
            runtime.is_production = is_production
            with patch(
                "baldur.adapters.resilient.backend.configure_storage_backend",
                autospec=True,
            ) as configure:
                bootstrap._install_resilient_storage_backend(runtime)
            return configure.call_args[0][0]

        return _install

    def test_production_boots_when_the_wal_is_on_its_configured_dir(
        self, writable_dir_chain, install_backend, monkeypatch, tmp_path
    ):
        """Case (i): the healthy production path is unchanged."""
        monkeypatch.setenv(WAL_DIR_ENV_VAR, str(tmp_path / "wal"))

        backend = install_backend(is_production=True)

        assert backend._wal_honors_configured_dir is True

    def test_production_refuses_to_boot_when_an_operator_dir_is_unwritable(
        self, writable_dir_chain, deny_dir, install_backend, monkeypatch, tmp_path
    ):
        """Case (ii): a dead WAL still fails closed, as before."""
        chosen = tmp_path / "compliance-wal"
        monkeypatch.setenv(WAL_DIR_ENV_VAR, str(chosen))
        deny_dir(chosen)

        with pytest.raises(ConfigurationError):
            install_backend(is_production=True)

    def test_production_refuses_to_boot_on_a_fallback_wal(
        self, writable_dir_chain, deny_dir, install_backend, monkeypatch
    ):
        """Case (iii) — the negative that keeps the guarantee honest.

        A fallback WAL is initialized and usable, so the old
        ``_wal_initialized`` predicate would have let production boot with an
        ephemeral WAL satisfying a durability promise it cannot keep.
        """
        monkeypatch.delenv(WAL_DIR_ENV_VAR, raising=False)
        deny_dir(Path(DEFAULT_WAL_DIR))

        with pytest.raises(ConfigurationError):
            install_backend(is_production=True)

    def test_production_gate_message_names_the_paths_and_the_break_glass(
        self, writable_dir_chain, deny_dir, install_backend, monkeypatch
    ):
        """The operator must learn why boot stopped and how to proceed."""
        monkeypatch.delenv(WAL_DIR_ENV_VAR, raising=False)
        deny_dir(Path(DEFAULT_WAL_DIR))

        with pytest.raises(ConfigurationError) as exc_info:
            install_backend(is_production=True)

        message = str(exc_info.value)
        assert DEFAULT_WAL_DIR in message
        assert "fell back to" in message
        assert WAL_DIR_ENV_VAR in message

    def test_non_production_boots_with_a_working_fallback_wal(
        self, writable_dir_chain, deny_dir, install_backend, monkeypatch
    ):
        """The behavior change: dev now gets a live WAL instead of a dead one."""
        monkeypatch.delenv(WAL_DIR_ENV_VAR, raising=False)
        deny_dir(Path(DEFAULT_WAL_DIR))

        backend = install_backend(is_production=False)

        assert backend._wal_initialized is True
        assert backend._wal_on_fallback_dir is True

    def test_defaults_only_settings_survive_the_installer_carry_over(
        self, writable_dir_chain, deny_dir, install_backend, monkeypatch
    ):
        """Regression: a full ``model_dump`` made every default look operator-chosen.

        If the carry-over marked ``wal_dir`` as set, the unwritable default
        would read as operator-chosen and raise instead of falling back.
        """
        monkeypatch.delenv(WAL_DIR_ENV_VAR, raising=False)
        deny_dir(Path(DEFAULT_WAL_DIR))

        backend = install_backend(is_production=False)

        assert "wal_dir" not in backend.config.model_fields_set
        assert backend.config.wal_dir == DEFAULT_WAL_DIR
