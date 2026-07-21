"""WAL directory resolution and its cross-instance isolation.

``WriteAheadLog`` mkdir'd ``/var/log/audit/wal`` at construction, and each
consumer improvised a guard around the failure — the Redis event bus dropped
the critical event it was trying to protect. Resolution now lives in the
primitive.

The non-merge tests are the regression for a refuted assumption: the dataclass
default ``file_prefix="audit_wal"`` is in use at three sites with three
different directories, so a shared fallback directory would let one WAL's
cleanup glob delete another's files.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from structlog.testing import capture_logs

from baldur.audit.wal import (
    LEGACY_WAL_DIR_ENV_VAR,
    WAL_DIR_ENV_VAR,
    WALConfig,
    WriteAheadLog,
    create_wal,
)
from baldur.core.exceptions import ConfigurationError
from tests.factories.writable_dir import log_events


class TestWALConfigContract:
    """Design contract values for the directory-origin fields."""

    def test_wal_dir_default_is_the_posix_audit_path(self):
        """Design contract: the shipped default WAL directory."""
        assert WALConfig().wal_dir == "/var/log/audit/wal"

    def test_wal_dir_operator_set_defaults_to_false(self):
        """Design contract: additive field, so existing constructions still fall back."""
        assert WALConfig().wal_dir_operator_set is False

    def test_wal_dir_env_var_defaults_to_promising_nothing(self):
        """Design contract: ``WALConfig`` reads no environment, so it names none.

        Regression: this defaulted to ``BALDUR_AUDIT_WAL_DIR``, which the
        dataclass never reads. A surface built on the default — the continuous
        recorder, ``create_wal()`` — then told operators to set a variable that
        would not move its WAL, and the identical fallback recurred every boot.
        """
        assert WALConfig().wal_dir_env_var is None

    def test_setting_the_audit_wal_variable_does_not_move_a_default_config(
        self, monkeypatch, tmp_path
    ):
        """The reason the default promises nothing: the variable is inert here."""
        monkeypatch.setenv("BALDUR_AUDIT_WAL_DIR", str(tmp_path / "operator-choice"))

        assert WALConfig().wal_dir == "/var/log/audit/wal"

    def test_env_var_names_are_the_prefixed_and_legacy_pair(self):
        """Design contract: the legacy unprefixed alias is still honored."""
        assert WAL_DIR_ENV_VAR == "BALDUR_AUDIT_WAL_DIR"
        assert LEGACY_WAL_DIR_ENV_VAR == "AUDIT_WAL_DIR"

    def test_file_prefix_default_is_shared_across_surfaces(self):
        """Design contract: the shared default is why the non-merge tests exist."""
        assert WALConfig().file_prefix == "audit_wal"


class TestWALDirResolutionBehavior:
    """Origin split and resolved-path propagation for the WAL."""

    def test_unwritable_default_dir_falls_back_and_initializes(
        self, writable_dir_chain, deny_dir
    ):
        """A hardcoded default relocates instead of killing the WAL."""
        config = WALConfig(wal_dir="/var/log/audit/wal")
        deny_dir(Path(config.wal_dir))

        wal = WriteAheadLog(config=config)

        assert wal.resolved_dir is not None
        assert wal.resolved_dir.fell_back is True
        assert wal.wal_dir.is_relative_to(writable_dir_chain.state)

    def test_unwritable_operator_set_dir_raises_naming_the_override(
        self, writable_dir_chain, deny_dir, tmp_path
    ):
        """An operator-chosen WAL directory fails loud rather than relocating.

        The surface passes the variable it reads; ``WALConfig`` promises none
        of its own.
        """
        chosen = tmp_path / "compliance-wal"
        deny_dir(chosen)
        config = WALConfig(
            wal_dir=str(chosen),
            wal_dir_operator_set=True,
            wal_dir_env_var=WAL_DIR_ENV_VAR,
        )

        with pytest.raises(ConfigurationError) as exc_info:
            WriteAheadLog(config=config)

        assert str(chosen) in str(exc_info.value)
        assert WAL_DIR_ENV_VAR in str(exc_info.value)

    def test_surface_without_an_override_promises_no_variable_name(
        self, writable_dir_chain, deny_dir, tmp_path
    ):
        """The event-bus WAL has no override, so none is named to the operator."""
        chosen = tmp_path / "hardcoded-wal"
        deny_dir(chosen)
        config = WALConfig(
            wal_dir=str(chosen),
            wal_dir_operator_set=True,
            wal_dir_env_var=None,
        )

        with pytest.raises(ConfigurationError) as exc_info:
            WriteAheadLog(config=config)

        assert WAL_DIR_ENV_VAR not in str(exc_info.value)

    def test_writable_dir_is_used_without_falling_back(
        self, writable_dir_chain, tmp_path
    ):
        """Negative: a healthy directory must not be relocated."""
        config = WALConfig(wal_dir=str(tmp_path / "wal"))

        wal = WriteAheadLog(config=config)

        assert wal.resolved_dir.fell_back is False
        assert wal.wal_dir == tmp_path / "wal"

    def test_writes_after_a_fallback_land_in_the_resolved_dir(
        self, writable_dir_chain, deny_dir
    ):
        """Resolved-path propagation: the writer follows the fallback."""
        # Given — a WAL that fell back off its unwritable default
        config = WALConfig(wal_dir="/var/log/audit/wal", sync_on_write=False)
        deny_dir(Path(config.wal_dir))
        wal = WriteAheadLog(config=config)

        # When
        wal.write({"event": "audit.entry", "value": 1})

        # Then
        assert list(wal.wal_dir.glob(f"{config.file_prefix}_*.wal"))
        wal.close()

    def test_create_wal_forwards_the_operator_origin_flag(
        self, writable_dir_chain, deny_dir, tmp_path
    ):
        """The public helper must let its caller own the loud-failure promise."""
        chosen = tmp_path / "caller-chosen"
        deny_dir(chosen)

        with pytest.raises(ConfigurationError):
            create_wal(wal_dir=str(chosen), wal_dir_operator_set=True)

    def test_create_wal_defaults_to_falling_back(
        self, writable_dir_chain, deny_dir, tmp_path
    ):
        """Negative: without the flag the helper keeps the fail-open default."""
        chosen = tmp_path / "caller-default"
        deny_dir(chosen)

        wal = create_wal(wal_dir=str(chosen))

        assert wal.resolved_dir.fell_back is True


class TestSharedPrefixWALIsolationBehavior:
    """Two WALs sharing ``file_prefix`` must not share a fallback directory."""

    @pytest.fixture
    def two_fallen_back_wals(self, writable_dir_chain, deny_dir, tmp_path):
        """Two default-prefix WALs whose distinct hardcoded dirs are unwritable."""
        first_dir = tmp_path / "wal-a"
        second_dir = tmp_path / "wal-b"
        deny_dir(first_dir)
        deny_dir(second_dir)
        first = WriteAheadLog(config=WALConfig(wal_dir=str(first_dir), max_files=1))
        second = WriteAheadLog(config=WALConfig(wal_dir=str(second_dir), max_files=1))
        yield first, second
        first.close()
        second.close()

    def test_two_default_prefix_wals_resolve_to_different_directories(
        self, two_fallen_back_wals
    ):
        """The deterministic leaf separates them by construction."""
        first, second = two_fallen_back_wals

        assert first.wal_dir != second.wal_dir

    def test_cleanup_does_not_delete_a_peer_wals_files(self, two_fallen_back_wals):
        """The refuted-assumption regression: cross-delete is WAL data loss."""
        # Given — the peer holds more files than this WAL's retention allows
        first, second = two_fallen_back_wals
        prefix = first._config.file_prefix
        peer_files = [first.wal_dir / f"{prefix}_{i}_{i}.wal" for i in range(3)]
        for path in peer_files:
            path.write_bytes(b"peer data")

        # When — the second WAL prunes its own directory
        second._cleanup_old_files()

        # Then — every peer file survives
        assert all(path.is_file() for path in peer_files)

    def test_recovery_does_not_inherit_a_peer_wals_sequence(
        self, writable_dir_chain, deny_dir, tmp_path
    ):
        """Cross-inherited sequence numbers are the other half of the merge failure."""
        # Given — a WAL that has written entries into its own fallback dir
        first_dir = tmp_path / "wal-a"
        second_dir = tmp_path / "wal-b"
        deny_dir(first_dir)
        deny_dir(second_dir)
        first = WriteAheadLog(
            config=WALConfig(wal_dir=str(first_dir), sync_on_write=False)
        )
        first.write({"event": "audit.entry", "value": 1})
        first.write({"event": "audit.entry", "value": 2})

        # When — a peer sharing the default file_prefix initializes
        second = WriteAheadLog(
            config=WALConfig(wal_dir=str(second_dir), sync_on_write=False)
        )

        # Then — the peer starts its own sequence rather than resuming this one
        assert first._sequence > 0
        assert second._sequence == 0
        first.close()
        second.close()

    def test_shared_purpose_across_directories_is_reported(
        self, writable_dir_chain, deny_dir, tmp_path
    ):
        """The collision diagnostic still fires as a wiring-bug signal."""
        first_dir = tmp_path / "wal-a"
        second_dir = tmp_path / "wal-b"
        deny_dir(first_dir)
        deny_dir(second_dir)

        with capture_logs() as logs:
            first = WriteAheadLog(config=WALConfig(wal_dir=str(first_dir)))
            second = WriteAheadLog(config=WALConfig(wal_dir=str(second_dir)))

        records = log_events(logs, "storage.writable_dir_purpose_error")
        assert len(records) == 1
        assert records[0]["log_level"] == "error"
        # The purpose is what bootstrap's durability-split check looks up.
        assert records[0]["purpose"] == f"wal_{WALConfig().file_prefix}"
        first.close()
        second.close()
