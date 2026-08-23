"""
ActorContext/TraceContext automatic injection tests.

Subjects:
- TestAuditContextAutoInjection: automatic context injection
"""

from unittest.mock import patch

import pytest


class TestAuditContextAutoInjection:
    """
    ActorContext/TraceContext automatic injection tests.

    Context coupling:
    - ShadowLogger and the WAL call ``_write_to_wal()`` directly
    - "which operator, in which operation" stays traceable
    """

    def test_shadow_logger_uses_write_to_wal(self):
        """ShadowLogger calls ``_write_to_wal()`` directly."""
        pytest.importorskip("baldur_pro")
        from baldur.adapters.memory.shadow_logger import ShadowLogger

        logger = ShadowLogger()
        logger.clear()

        with patch("baldur_pro.services.audit.base._write_to_wal") as mock_wal:
            logger.record_sync_failure(
                service_name="test_service",
                intended_state="OPEN",
                error=Exception("Connection timeout"),
                adapter_type="redis",
                operation="sync",
            )

            # _write_to_wal must have been called
            mock_wal.assert_called_once()
            call_kwargs = mock_wal.call_args[1]
            assert call_kwargs["event_type"] == "SHADOW_LOG_SYNC_FAILED"
            assert call_kwargs["source"] == "ShadowLogger"
            assert call_kwargs["details"]["service_name"] == "test_service"

    def test_shadow_logger_recovery_uses_write_to_wal(self):
        """ShadowLogger recovery calls ``_write_to_wal()``."""
        pytest.importorskip("baldur_pro")
        from baldur.adapters.memory.shadow_logger import ShadowLogger

        logger = ShadowLogger()
        logger.clear()

        # Record a failure first
        with patch("baldur_pro.services.audit.base._write_to_wal"):
            logger.record_sync_failure(
                service_name="test_service",
                intended_state="OPEN",
                error=Exception("Test error"),
            )

        # Recovery must reach _write_to_wal
        with patch("baldur_pro.services.audit.base._write_to_wal") as mock_wal:
            count = logger.mark_as_synced("test_service")

            if count > 0:
                mock_wal.assert_called_once()
                call_kwargs = mock_wal.call_args[1]
                assert call_kwargs["event_type"] == "SHADOW_LOG_RECOVERED"
                assert call_kwargs["details"]["recovered_count"] == count

    def test_meta_event_without_adapter_leaves_the_wal_untouched(self, temp_wal_dir):
        """With no adapter a meta-event does not alter the WAL it describes."""
        from pathlib import Path as _Path

        from baldur.audit.wal import WALConfig, WriteAheadLog

        config = WALConfig(
            wal_dir=temp_wal_dir,
            sync_on_write=False,
            max_files=1000,  # so retention deletion cannot move the byte count
        )
        wal = WriteAheadLog(config=config)  # audit_adapter=None

        for i in range(5):
            wal.write({"event": f"test_{i}"})
        wal.flush()

        def wal_dir_bytes() -> int:
            return sum(f.stat().st_size for f in _Path(temp_wal_dir).glob("*.wal"))

        entries_before = wal.get_stats().total_entries
        bytes_before = wal_dir_bytes()
        files_before = len(list(_Path(temp_wal_dir).glob("*.wal")))

        # 1) The rotation meta-event
        wal._rotate_file()
        assert len(list(_Path(temp_wal_dir).glob("*.wal"))) == files_before, (
            "rotation only closes the file; it does not open a new one yet"
        )

        # 2) The corruption meta-event — the branch must actually fire for
        #    the assertions below to mean anything.
        target = sorted(_Path(temp_wal_dir).glob("*.wal"))[0]
        with open(target, "r+b") as f:
            f.seek(20)
            f.write(b"CORRUPTED")

        wal2 = WriteAheadLog(config=config)  # audit_adapter=None
        wal2.recover_unprocessed(last_processed_seq=0)
        assert wal2.get_stats().corrupted_entries > 0, (
            "the corruption branch must actually be reached"
        )
        wal2.close()
        wal.close()

        assert wal.get_stats().total_entries == entries_before
        assert wal_dir_bytes() == bytes_before

    def test_shadow_logger_graceful_on_import_error(self):
        """A failing ``_write_to_wal`` import is handled gracefully."""
        from baldur.adapters.memory.shadow_logger import ShadowLogger

        logger = ShadowLogger()
        logger.clear()

        with patch.dict("sys.modules", {"baldur_pro.services.audit.base": None}):
            # The main logic keeps working even when the import raises
            logger.record_sync_failure(
                service_name="test_service",
                intended_state="OPEN",
                error=Exception("Test error"),
            )

            # The record must still be written
            records = logger.get_all_records()
            assert len(records) >= 1
