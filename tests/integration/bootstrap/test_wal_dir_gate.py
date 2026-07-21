"""Production WAL boot gate across the real resolution chain.

Everything else in the writable-dir change is delegation — a surface calls the
primitive and stores ``.path`` — and is covered by unit tests. The boot gate is
genuine composition: ``_install_resilient_storage_backend`` reads a predicate
the backend derives from a ``ResolvedDir`` produced inside ``WriteAheadLog``,
which is produced by the primitive, while the settings carry-over in that same
function decides the ``operator_set`` input to the whole chain. Four components
share one state dependency across a transaction boundary — boot succeeds, or
the process does not start.

No infrastructure: the runtime mode and the Redis URL are stubbed, the
directories are real ``tmp_path`` ones, and only the unwritable case is
simulated.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from baldur.adapters.resilient.backend import WAL_DIR_ENV_VAR
from baldur.core.exceptions import ConfigurationError
from baldur.runtime import BaldurRuntime
from baldur.settings.redis import RedisSettings
from baldur.settings.resilient_storage import ResilientStorageSettings

DEFAULT_WAL_DIR = ResilientStorageSettings.model_fields["wal_dir"].default


@pytest.fixture(autouse=True)
def _reset_bootstrap_state():
    """Each test starts and ends with clean bootstrap state."""
    from baldur import bootstrap

    bootstrap.reset_init_state()
    yield
    bootstrap.reset_init_state()


@pytest.fixture
def wire_storage(monkeypatch):
    """Run the real eager-backend install for a chosen runtime mode.

    Returns an ``install(is_production=...)`` callable yielding the backend
    that was constructed. Only ``configure_storage_backend`` is stubbed —
    installing a real backend into the process-global registry would leak into
    sibling tests — so the settings carry-over, the WAL, the primitive and the
    gate are all the real implementations.
    """
    from baldur import bootstrap

    redis_settings = MagicMock(spec=RedisSettings)
    redis_settings.url = "redis://stub:6379/0"
    monkeypatch.setattr(
        "baldur.settings.redis.get_redis_settings", lambda: redis_settings
    )

    def _install(*, is_production: bool):
        runtime = MagicMock(spec=BaldurRuntime)
        runtime.is_production = is_production
        with patch(
            "baldur.adapters.resilient.backend.configure_storage_backend",
            autospec=True,
        ) as configure:
            bootstrap._install_resilient_storage_backend(runtime)
        return configure.call_args[0][0]

    return _install


class TestProductionWALBootGateIntegration:
    """The three production cases, end to end through the real chain."""

    def test_configured_writable_dir_boots_production(
        self, writable_dir_chain, wire_storage, monkeypatch, tmp_path
    ):
        """Case (i): the operator's directory is honored, so boot proceeds."""
        configured = tmp_path / "srv-wal"
        monkeypatch.setenv(WAL_DIR_ENV_VAR, str(configured))

        backend = wire_storage(is_production=True)

        assert backend._wal_initialized is True
        assert backend._wal_on_fallback_dir is False
        assert backend._wal.wal_dir == configured

    def test_unwritable_configured_dir_refuses_production_boot(
        self, writable_dir_chain, deny_dir, wire_storage, monkeypatch, tmp_path
    ):
        """Case (ii): a dead WAL fails closed, exactly as before the change."""
        configured = tmp_path / "srv-wal"
        monkeypatch.setenv(WAL_DIR_ENV_VAR, str(configured))
        deny_dir(configured)

        with pytest.raises(ConfigurationError):
            wire_storage(is_production=True)

    def test_unwritable_default_dir_refuses_production_boot_despite_a_live_wal(
        self, writable_dir_chain, deny_dir, wire_storage, monkeypatch
    ):
        """Case (iii): the composition that the old predicate would have let through.

        The default falls back, so the WAL is initialized and usable — yet the
        gate must still refuse, because it promises the WAL is on its
        *configured* directory.
        """
        monkeypatch.delenv(WAL_DIR_ENV_VAR, raising=False)
        deny_dir(Path(DEFAULT_WAL_DIR))

        with pytest.raises(ConfigurationError) as exc_info:
            wire_storage(is_production=True)

        message = str(exc_info.value)
        assert DEFAULT_WAL_DIR in message
        assert WAL_DIR_ENV_VAR in message

    def test_unwritable_default_dir_boots_non_production_with_a_live_wal(
        self, writable_dir_chain, deny_dir, wire_storage, monkeypatch
    ):
        """The behavior change: dev gets a working fallback WAL, not a dead one."""
        monkeypatch.delenv(WAL_DIR_ENV_VAR, raising=False)
        deny_dir(Path(DEFAULT_WAL_DIR))

        backend = wire_storage(is_production=False)

        assert backend._wal_initialized is True
        assert backend._wal_on_fallback_dir is True
        assert backend._wal.wal_dir.is_relative_to(writable_dir_chain.state)

    def test_break_glass_env_var_restores_production_boot(
        self, writable_dir_chain, deny_dir, wire_storage, monkeypatch, tmp_path
    ):
        """The documented escape: choosing a writable path explicitly is honored.

        This is the only break-glass mechanism, so it has to actually work —
        pointing the variable at any writable path makes the directory
        operator-chosen and the gate then accepts it.
        """
        # Given — the shipped default is unwritable and production refuses
        monkeypatch.delenv(WAL_DIR_ENV_VAR, raising=False)
        deny_dir(Path(DEFAULT_WAL_DIR))
        with pytest.raises(ConfigurationError):
            wire_storage(is_production=True)

        # When — the operator points the override at a writable path
        monkeypatch.setenv(WAL_DIR_ENV_VAR, str(tmp_path / "break-glass"))
        backend = wire_storage(is_production=True)

        # Then — boot proceeds on the explicitly chosen directory
        assert backend._wal_honors_configured_dir is True


class TestStorageDirsReportIntegration:
    """The boot-time report an operator reads after a fallback."""

    def test_fallback_is_visible_in_the_startup_report(
        self, writable_dir_chain, deny_dir, wire_storage, monkeypatch
    ):
        """The resolution the gate acted on must also be reportable."""
        # Given — a non-production boot that fell back
        from baldur.bootstrap import ExtensionResult, _build_startup_report

        monkeypatch.delenv(WAL_DIR_ENV_VAR, raising=False)
        deny_dir(Path(DEFAULT_WAL_DIR))
        wire_storage(is_production=False)

        # When
        report = _build_startup_report(ExtensionResult())

        # Then
        entries = [
            entry
            for key, entry in report["storage_dirs"].items()
            if key.startswith("wal_resilient_storage-")
        ]
        assert len(entries) == 1
        assert entries[0]["status"] == "fallback"
        # The registry stores the path form, which is separator-normalized.
        assert entries[0]["preferred"] == str(Path(DEFAULT_WAL_DIR))
