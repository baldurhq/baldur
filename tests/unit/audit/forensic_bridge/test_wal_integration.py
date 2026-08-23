"""WAL <-> audit adapter integration tests.

Under test:
- TestWALAuditIntegration: what a host-wired audit adapter does and does not
  receive from a ``WriteAheadLog``.
"""

from pathlib import Path

from tests.factories.wal_records import RawRecord, own_pid_wal_name, write_raw_wal_file

# A checksum field that is valid ASCII but cannot be the record's CRC32.
# Rewriting the field leaves every record's length and offset untouched, so
# the mismatch lands exactly where the test put it.
BAD_CHECKSUM = b"deadbeef"


class TestWALAuditIntegration:
    """WAL meta-events reach a wired adapter; recovery is not one of them."""

    def test_wal_init_accepts_audit_adapter(self):
        """The constructor exposes the seam a host wires its adapter into."""
        import inspect

        from baldur.audit.wal import WriteAheadLog

        sig = inspect.signature(WriteAheadLog.__init__)
        assert "audit_adapter" in sig.parameters

    def test_recovery_emits_no_audit_event(self, temp_wal_dir, mock_audit_adapter):
        """Recovery reports through a metric and a log line, never an audit
        entry: it used to fire on ordinary steady-state reads rather than on a
        real recovery, which is how it came to feed itself.
        """
        from baldur.audit.wal import WALConfig, WriteAheadLog

        config = WALConfig(
            wal_dir=temp_wal_dir,
            sync_on_write=False,
        )

        # Write through one WAL instance...
        wal = WriteAheadLog(config=config, audit_adapter=mock_audit_adapter)
        wal.write({"event": "test1"})
        wal.write({"event": "test2"})
        wal.write({"event": "test3"})
        wal.close()

        # ...and recover through a fresh one.
        wal2 = WriteAheadLog(config=config, audit_adapter=mock_audit_adapter)
        entries = wal2.recover_unprocessed(last_processed_seq=0)
        wal2.close()

        assert entries, "entries must be recovered for this assertion to mean anything"
        assert mock_audit_adapter.get_events_by_type("WAL_RECOVERED") == []

    def test_wal_rotated_event_on_rotation(self, temp_wal_dir, mock_audit_adapter):
        """A rotation delivers WAL_ROTATED to a wired adapter."""
        from baldur.audit.wal import WALConfig, WriteAheadLog

        # A tiny file-size cap forces rotation quickly.
        config = WALConfig(
            wal_dir=temp_wal_dir,
            max_file_size_mb=0.0001,
            sync_on_write=False,
        )

        wal = WriteAheadLog(config=config, audit_adapter=mock_audit_adapter)

        for i in range(100):
            wal.write({"event": f"test_{i}", "data": "x" * 1000})

        wal.close()

        rotated_events = mock_audit_adapter.get_events_by_type("WAL_ROTATED")
        assert len(rotated_events) > 0
        assert rotated_events[0]["source"] == "WriteAheadLog"

    def test_wal_corruption_detected_event(self, temp_wal_dir, mock_audit_adapter):
        """A checksum mismatch delivers WAL_CORRUPTION_DETECTED to a wired
        adapter, on the best-effort read a running drain actually takes.

        The corruption is authored as a wrong checksum field rather than by
        poking bytes at a fixed offset: the previous version of this test could
        not tell whether the branch had been reached, so it asserted nothing.
        """
        from baldur.audit.wal import WALConfig, WriteAheadLog

        config = WALConfig(
            wal_dir=temp_wal_dir,
            sync_on_write=False,
        )

        # The WAL is constructed before the files are stamped:
        # ``_init_or_recover`` strict-reads the last own-PID file, which would
        # otherwise report this corruption once before the recovery below does.
        wal = WriteAheadLog(config=config, audit_adapter=mock_audit_adapter)
        wal_dir = Path(temp_wal_dir)
        write_raw_wal_file(
            wal_dir / own_pid_wal_name(config.file_prefix, "20260101_000000"),
            [
                RawRecord(sequence=1),
                RawRecord(sequence=2, checksum=BAD_CHECKSUM),
                RawRecord(sequence=3),
            ],
        )
        # A second file puts the read on the parallel (best-effort) path, which
        # is the one a running drain takes.
        write_raw_wal_file(
            wal_dir / own_pid_wal_name(config.file_prefix, "20260101_000001"),
            [RawRecord(sequence=4)],
        )

        entries = wal.recover_unprocessed(last_processed_seq=0)
        wal.close()

        assert [e.sequence for e in entries] == [1, 3, 4], (
            "only the corrupt record is skipped"
        )
        detections = mock_audit_adapter.get_events_by_type("WAL_CORRUPTION_DETECTED")
        assert len(detections) == 1
        assert detections[0]["details"]["expected_checksum"] == BAD_CHECKSUM.decode(
            "ascii"
        )
        assert detections[0]["source"] == "WriteAheadLog"
