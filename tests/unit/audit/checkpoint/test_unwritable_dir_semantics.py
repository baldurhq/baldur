"""Checkpoint storage behavior when its directory is not writable.

Before the writable-dir primitive, ``FileCheckpointStorage`` mkdir'd
``/var/log/audit`` at construction and every consumer swallowed the resulting
``PermissionError``: on a non-root deploy checkpointing was silently dead and
WAL recovery restarted from sequence 0. These tests pin the replacement — an
operator-chosen directory fails loud, the platform default falls back and keeps
working — and the Kafka backup's fail-open construction.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from structlog.testing import capture_logs

from baldur.audit.checkpoint import (
    FileCheckpointStorage,
    UnifiedCheckpointData,
    get_default_checkpoint_strategy,
    reset_default_checkpoint_strategy,
)
from baldur.audit.checkpoint.kafka_redis_storage import KafkaRedisCheckpointStorage
from baldur.audit.sync_worker import AuditSyncWorker
from baldur.core.exceptions import ConfigurationError
from tests.factories import MockRedisClient
from tests.factories.writable_dir import log_events

windows_only = pytest.mark.skipif(
    sys.platform != "win32",
    reason="Windows-only platform default",
)


def _deny_platform_default(deny_dir, writable_dir_chain) -> None:
    """Deny whichever directory the storage defaults to on this platform.

    POSIX defaults to ``DEFAULT_DIR``; Windows defaults to a directory under
    the system temp dir, which the chain fixture has redirected. Denying both
    keeps the test platform-symmetric — the persistent state step stays
    writable either way, so the fallback lands there on both.
    """
    deny_dir(Path(FileCheckpointStorage.DEFAULT_DIR))
    deny_dir(writable_dir_chain.temp)


class TestFileCheckpointStorageDirResolutionBehavior:
    """Origin inference and fallback for the file checkpoint store."""

    def test_explicit_base_path_that_is_unwritable_raises(
        self, writable_dir_chain, deny_dir, tmp_path
    ):
        """An explicit argument means the operator chose the directory."""
        chosen = tmp_path / "compliance"
        deny_dir(chosen)

        with pytest.raises(ConfigurationError) as exc_info:
            FileCheckpointStorage(base_path=chosen)

        assert str(chosen) in str(exc_info.value)
        assert FileCheckpointStorage.PATH_ENV_VAR in str(exc_info.value)

    def test_audit_path_env_var_that_is_unwritable_raises(
        self, writable_dir_chain, deny_dir, monkeypatch, tmp_path
    ):
        """A set ``BALDUR_AUDIT_PATH`` means the operator chose the directory."""
        chosen = tmp_path / "from-env"
        monkeypatch.setenv(FileCheckpointStorage.PATH_ENV_VAR, str(chosen))
        deny_dir(chosen)

        with pytest.raises(ConfigurationError):
            FileCheckpointStorage()

    def test_platform_default_without_env_falls_back_instead_of_raising(
        self, writable_dir_chain, deny_dir, monkeypatch
    ):
        """The shipped default is not an operator choice, so it relocates."""
        monkeypatch.delenv(FileCheckpointStorage.PATH_ENV_VAR, raising=False)
        _deny_platform_default(deny_dir, writable_dir_chain)

        storage = FileCheckpointStorage()

        assert storage.resolved_dir.fell_back is True
        assert storage.base_path.is_relative_to(writable_dir_chain.state)

    def test_fallback_warning_names_both_paths_and_the_override_env(
        self, writable_dir_chain, deny_dir, monkeypatch
    ):
        """An operator must be able to see where checkpoints actually went."""
        monkeypatch.delenv(FileCheckpointStorage.PATH_ENV_VAR, raising=False)
        _deny_platform_default(deny_dir, writable_dir_chain)

        with capture_logs() as logs:
            storage = FileCheckpointStorage()

        warnings = log_events(logs, "storage.writable_dir_probe_failed")
        assert len(warnings) == 1
        assert warnings[0]["preferred"] == str(storage.resolved_dir.preferred)
        assert warnings[0]["fallback"] == str(storage.base_path)
        assert warnings[0]["override_env"] == FileCheckpointStorage.PATH_ENV_VAR

    def test_operator_set_false_overrides_the_env_var_inference(
        self, writable_dir_chain, deny_dir, monkeypatch, tmp_path
    ):
        """An explicit origin flag wins over the inference, so it can fall back."""
        chosen = tmp_path / "from-env"
        monkeypatch.setenv(FileCheckpointStorage.PATH_ENV_VAR, str(chosen))
        deny_dir(chosen)

        storage = FileCheckpointStorage(base_path_operator_set=False)

        assert storage.resolved_dir.fell_back is True

    def test_checkpoint_saved_after_a_fallback_is_loadable(
        self, writable_dir_chain, deny_dir, monkeypatch
    ):
        """Durability machinery actually runs on the fallback, not just resolves."""
        # Given — a storage that fell back off an unwritable default
        monkeypatch.delenv(FileCheckpointStorage.PATH_ENV_VAR, raising=False)
        _deny_platform_default(deny_dir, writable_dir_chain)
        storage = FileCheckpointStorage()

        # When
        storage.save("default", UnifiedCheckpointData(wal_sequence=42))

        # Then — the round trip works and the file is in the fallback directory
        assert storage.load("default").wal_sequence == 42
        assert (storage.base_path / "checkpoint.default.json").is_file()

    def test_file_path_follows_the_resolved_directory(
        self, writable_dir_chain, deny_dir, monkeypatch
    ):
        """Negative: no reader may stay on the pre-resolution path."""
        monkeypatch.delenv(FileCheckpointStorage.PATH_ENV_VAR, raising=False)
        _deny_platform_default(deny_dir, writable_dir_chain)

        storage = FileCheckpointStorage()

        assert storage._get_file_path("default").parent == storage.base_path
        assert storage.base_path != storage.resolved_dir.preferred

    @windows_only
    def test_windows_platform_default_stays_on_the_tempdir_base(
        self, writable_dir_chain, monkeypatch
    ):
        """Windows already defaulted to a writable base — that must not change."""
        monkeypatch.delenv(FileCheckpointStorage.PATH_ENV_VAR, raising=False)

        storage = FileCheckpointStorage()

        assert storage.resolved_dir.fell_back is False
        assert storage.base_path.is_relative_to(writable_dir_chain.temp)


class TestDefaultCheckpointStrategyFallbackBehavior:
    """The singleton strategy every audit consumer resolves through."""

    @pytest.fixture(autouse=True)
    def _reset_strategy_singleton(self):
        """Force reconstruction so each test resolves its own directory."""
        reset_default_checkpoint_strategy()
        yield
        reset_default_checkpoint_strategy()

    def test_default_strategy_constructs_on_an_unwritable_default_dir(
        self, writable_dir_chain, deny_dir, monkeypatch
    ):
        """Construction used to raise here, which read as 'no strategy'."""
        monkeypatch.delenv(FileCheckpointStorage.PATH_ENV_VAR, raising=False)
        _deny_platform_default(deny_dir, writable_dir_chain)

        strategy = get_default_checkpoint_strategy()

        assert isinstance(strategy, FileCheckpointStorage)
        assert strategy.resolved_dir.fell_back is True

    def test_sync_worker_saves_instead_of_reporting_no_strategy_available(
        self, writable_dir_chain, deny_dir, monkeypatch
    ):
        """Negative: the silent seq-0 path must not be taken on this branch."""
        # Given — the non-root deploy shape: default dir, no operator override
        monkeypatch.delenv(FileCheckpointStorage.PATH_ENV_VAR, raising=False)
        _deny_platform_default(deny_dir, writable_dir_chain)
        worker = AuditSyncWorker()

        # When
        with capture_logs() as logs:
            worker._save_checkpoint()

        # Then — a strategy was found and the checkpoint landed
        assert (
            log_events(logs, "audit_sync_worker.no_checkpoint_strategy_available") == []
        )
        assert log_events(logs, "audit_sync_worker.checkpoint_save_failed") == []
        strategy = worker._get_checkpoint_strategy()
        assert strategy is not None
        assert (strategy.base_path / "checkpoint.sync_worker.json").is_file()

    def test_sync_worker_reports_an_operator_chosen_dir_at_warning(
        self, writable_dir_chain, deny_dir, monkeypatch, tmp_path
    ):
        """An operator misconfiguration is named, not swallowed at debug."""
        chosen = tmp_path / "compliance"
        monkeypatch.setenv(FileCheckpointStorage.PATH_ENV_VAR, str(chosen))
        deny_dir(chosen)
        worker = AuditSyncWorker()

        with capture_logs() as logs:
            strategy = worker._get_checkpoint_strategy()

        assert strategy is None
        records = log_events(logs, "audit_sync_worker.checkpoint_strategy_unavailable")
        assert len(records) == 1
        assert records[0]["log_level"] == "warning"


class TestKafkaBackupFailOpenBehavior:
    """The Redis-failure file backup must never break construction."""

    @pytest.fixture
    def redis_client(self):
        """In-memory Redis double — the backup path never touches it."""
        return MockRedisClient()

    def test_unwritable_default_backup_dir_disables_the_backup(
        self, writable_dir_chain, deny_dir, monkeypatch, redis_client
    ):
        """Enabling the backup must not fail where plain Redis storage works."""
        monkeypatch.delenv(FileCheckpointStorage.PATH_ENV_VAR, raising=False)
        for root in (
            writable_dir_chain.state,
            writable_dir_chain.var_tmp,
            writable_dir_chain.temp,
        ):
            deny_dir(root)
        deny_dir(Path("/var/log/audit/kafka_checkpoint_backup"))

        with capture_logs() as logs:
            storage = KafkaRedisCheckpointStorage(redis_client=redis_client)

        assert storage._file_backup is None
        records = log_events(logs, "kafka_redis_checkpoint.file_backup_disabled")
        assert len(records) == 1
        assert records[0]["log_level"] == "warning"

    def test_unwritable_operator_set_backup_path_disables_the_backup(
        self, writable_dir_chain, deny_dir, tmp_path, redis_client
    ):
        """Even the loud operator-set raise is absorbed by this optional integration."""
        chosen = tmp_path / "backup"
        deny_dir(chosen)

        storage = KafkaRedisCheckpointStorage(
            redis_client=redis_client, file_backup_path=chosen
        )

        assert storage._file_backup is None

    def test_writable_backup_dir_keeps_the_backup_enabled(
        self, writable_dir_chain, tmp_path, redis_client
    ):
        """Negative: the fail-open branch must not fire on a healthy directory."""
        storage = KafkaRedisCheckpointStorage(
            redis_client=redis_client, file_backup_path=tmp_path / "backup"
        )

        assert storage._file_backup is not None
        assert storage._file_backup.base_path == tmp_path / "backup"

    def test_backup_falls_back_to_a_directory_of_its_own(
        self, writable_dir_chain, deny_dir, monkeypatch, redis_client
    ):
        """A shared purpose would merge the backup into the primary store's dir."""
        # Given — both stores' defaults unwritable, so both fall back
        monkeypatch.delenv(FileCheckpointStorage.PATH_ENV_VAR, raising=False)
        _deny_platform_default(deny_dir, writable_dir_chain)
        deny_dir(Path("/var/log/audit/kafka_checkpoint_backup"))

        # When
        with capture_logs() as logs:
            primary = FileCheckpointStorage()
            backup = KafkaRedisCheckpointStorage(redis_client=redis_client)

        # Then — distinct directories, and no purpose-collision diagnostic
        assert backup._file_backup is not None
        assert backup._file_backup.base_path != primary.base_path
        assert log_events(logs, "storage.writable_dir_purpose_error") == []
