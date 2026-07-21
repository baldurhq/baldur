"""DLQ Tier-1 disk buffer directory resolution.

``/var/lib/baldur/buffer`` is a default-ON root-owned path, so on a non-root
host the LMDB open died at construction and captures degraded to the Tier-2 raw
jsonl file.

The two-reader assertions are the load-bearing ones: the settings object is not
mutated by the primitive, and ``data_path`` is read both for the LMDB directory
and for the free-space monitor. A monitor left on the original path decides
``fail_open_on_disk_full`` and priority purge against a filesystem the buffer no
longer uses — and does so silently, because it snapshots the path at
construction and its ``check()`` swallows the error on a missing path.
"""

from __future__ import annotations

import pytest

from baldur.audit.persistence.config import DiskBufferSettings
from baldur.audit.persistence.disk_buffer import DiskPersistentBuffer
from baldur.audit.persistence.disk_buffer_models import BufferState
from baldur.core.exceptions import ConfigurationError

pytest.importorskip("lmdb", reason="disk buffer requires the lmdb extra")


# The platform default LMDB map is allocated as a file up front on Windows.
# Six xdist workers each reserving one can push free space below the buffer's
# threshold, which flips it to DISK_FULL_FAILOPEN and makes put() silently
# return None — a host-capacity failure wearing this test's name.
TEST_MAP_SIZE_MB = 16


@pytest.fixture
def buffer_settings(tmp_path):
    """Defaults-only settings, minus the two things that are host-sensitive.

    ``data_dir`` is deliberately left at its default — the whole point is that
    the shipped default is unwritable — so only the map size and the shutdown
    handler registration are pinned.
    """
    return DiskBufferSettings(
        enable_shutdown_handlers=False,
        lmdb_map_size_mb=TEST_MAP_SIZE_MB,
    )


class TestDiskBufferDirFallbackBehavior:
    """Single resolution, followed by every reader."""

    def test_unwritable_default_data_dir_falls_back_instead_of_failing(
        self, writable_dir_chain, deny_dir, buffer_settings
    ):
        """Tier-1 structured buffering survives a non-root host."""
        deny_dir(buffer_settings.data_path)

        buffer = DiskPersistentBuffer(settings=buffer_settings)

        try:
            assert buffer._resolved_data_path.is_relative_to(writable_dir_chain.state)
        finally:
            buffer.close()

    def test_lmdb_directory_follows_the_resolved_path(
        self, writable_dir_chain, deny_dir, buffer_settings
    ):
        """Reader one: the database is opened under the resolved directory."""
        deny_dir(buffer_settings.data_path)

        buffer = DiskPersistentBuffer(settings=buffer_settings)

        try:
            assert (buffer._resolved_data_path / buffer._db_name).is_dir()
        finally:
            buffer.close()

    def test_disk_space_monitor_follows_the_resolved_path(
        self, writable_dir_chain, deny_dir, buffer_settings
    ):
        """Reader two: the monitor must measure the volume actually in use."""
        deny_dir(buffer_settings.data_path)

        buffer = DiskPersistentBuffer(settings=buffer_settings)

        try:
            assert buffer._disk_monitor is not None
            assert buffer._disk_monitor._path == buffer._resolved_data_path
        finally:
            buffer.close()

    def test_disk_space_monitor_is_never_left_on_the_pre_resolution_path(
        self, writable_dir_chain, deny_dir, buffer_settings
    ):
        """Negative: the settings object is not mutated, so this is a real risk."""
        deny_dir(buffer_settings.data_path)

        buffer = DiskPersistentBuffer(settings=buffer_settings)

        try:
            assert buffer._disk_monitor._path != buffer_settings.data_path
        finally:
            buffer.close()

    def test_writable_data_dir_is_used_without_falling_back(
        self, writable_dir_chain, tmp_path
    ):
        """Negative: a healthy directory must not be relocated."""
        settings = DiskBufferSettings(
            data_dir=str(tmp_path / "buffer"), enable_shutdown_handlers=False
        )

        buffer = DiskPersistentBuffer(settings=settings)

        try:
            assert buffer._resolved_data_path == settings.data_path
            assert buffer._disk_monitor._path == settings.data_path
        finally:
            buffer.close()

    def test_unwritable_operator_set_data_dir_raises(
        self, writable_dir_chain, deny_dir, tmp_path
    ):
        """An explicitly configured buffer directory fails loud."""
        chosen = tmp_path / "chosen-buffer"
        deny_dir(chosen)
        settings = DiskBufferSettings(
            data_dir=str(chosen), enable_shutdown_handlers=False
        )

        with pytest.raises(ConfigurationError) as exc_info:
            DiskPersistentBuffer(settings=settings)

        assert DiskPersistentBuffer.DIR_ENV_VAR in str(exc_info.value)

    def test_entries_written_after_a_fallback_are_readable(
        self, writable_dir_chain, deny_dir, buffer_settings
    ):
        """The buffer actually works on the fallback, not just resolves."""
        # Given — a buffer that fell back off an unwritable default
        deny_dir(buffer_settings.data_path)
        buffer = DiskPersistentBuffer(settings=buffer_settings)

        try:
            # When
            key = buffer.put({"event": "audit.entry", "value": 1})
            buffer.flush_group_commit()

            # Then — named explicitly so a host-capacity trip cannot masquerade
            # as a directory-resolution bug
            assert buffer._state == BufferState.ACTIVE
            assert key is not None
            assert buffer.count() == 1
        finally:
            buffer.close()
