"""Unit tests for unconditional WAL corruption reporting (#763 D1).

The corruption report used to live inside the strict-read arm only, so a
checksum mismatch was invisible on every best-effort read — the orphan absorb,
the multi-file drain, the bounded read and every parallel recovery all counted
the bad record and moved on with no metric, no callback, no log line and no
event. The report is now hoisted above the strict/best-effort branch; only the
control flow *after* it stays mode-dependent.

Verification techniques per UNIT_TEST_GUIDELINES §8:
- Parametrize: read mode {strict, best-effort} x ``best_effort_recovery``
  {True, False} — all four arms must report
- Side effects: WARNING log, corruption counter, ``on_corruption`` callback,
  adapter delivery, ``get_stats().corrupted_entries``
- Branch outcome: the post-report control flow (``break`` vs ``continue``) is
  what the mode still decides
- Dependency interaction: the reported checksums are the record's own, not a
  placeholder

Corrupt records are authored by writing a deliberately wrong checksum field
rather than by poking bytes into a valid file: the record length is unchanged,
so every following record stays at its original offset and the expected /
computed checksum pair is exactly predictable.

Every WAL here is constructed against an empty directory and the files are
stamped afterwards. ``_init_or_recover`` strict-reads the last own-PID file to
restore the sequence, so a WAL constructed over an already-corrupt directory
would report that corruption once before the test's own read.
"""

from __future__ import annotations

import json
import zlib
from pathlib import Path
from unittest.mock import patch

import pytest
from structlog.testing import capture_logs

from baldur.audit.wal import WALConfig, WriteAheadLog
from baldur.audit.wal._models import WALCorruptionError
from tests.factories.wal_records import RawRecord, own_pid_wal_name, write_raw_wal_file
from tests.factories.writable_dir import log_events

WAL_PREFIX = "corruption_wal"

# A checksum field that is valid ASCII but cannot be the record's CRC32.
BAD_CHECKSUM = b"deadbeef"

# The record the corrupt file's checksum field lies about.
CORRUPT_SEQUENCE = 2

READ_MODES = ("strict", "best_effort")


@pytest.fixture
def make_wal(tmp_path: Path):
    """Build a WAL over an empty directory; close every one at teardown."""
    built: list[WriteAheadLog] = []

    def _make(*, best_effort_recovery: bool = True, **kwargs) -> WriteAheadLog:
        config = WALConfig(
            wal_dir=str(tmp_path),
            sync_on_write=False,
            file_prefix=WAL_PREFIX,
            best_effort_recovery=best_effort_recovery,
        )
        wal = WriteAheadLog(config=config, **kwargs)
        built.append(wal)
        return wal

    yield _make

    for wal in built:
        wal.close()


def _corrupt_file(wal_dir: Path, stamp: str = "20260101_000000") -> Path:
    """A three-record file whose middle record carries a wrong checksum."""
    return write_raw_wal_file(
        wal_dir / own_pid_wal_name(WAL_PREFIX, stamp),
        [
            RawRecord(sequence=1),
            RawRecord(sequence=CORRUPT_SEQUENCE, checksum=BAD_CHECKSUM),
            RawRecord(sequence=3),
        ],
    )


def _crc32_of_corrupt_record() -> str:
    """The checksum the reader computes for the lied-about record.

    Derived from the same serialization the factory writes, so this tracks the
    record format instead of restating a byte string.
    """
    payload = json.dumps(RawRecord(sequence=CORRUPT_SEQUENCE).as_entry()).encode(
        "utf-8"
    )
    return format(zlib.crc32(payload) & 0xFFFFFFFF, "08x")


def _read(wal: WriteAheadLog, filepath: Path, mode: str) -> list:
    reader = wal._read_wal_file if mode == "strict" else wal._read_wal_file_best_effort
    return list(reader(filepath))


class TestWALCorruptionReporting:
    """A checksum mismatch is reported through every channel, in every read
    mode — detection does not depend on which mode the caller happened to use.
    """

    @pytest.mark.parametrize("mode", READ_MODES, ids=READ_MODES)
    @pytest.mark.parametrize(
        "best_effort_recovery", [True, False], ids=["recovery_on", "recovery_off"]
    )
    def test_checksum_mismatch_is_reported_in_every_read_mode(
        self, tmp_path, make_wal, capturing_adapter, mode, best_effort_recovery
    ):
        # Given: a file with one wrong-checksum record among intact ones.
        wal = make_wal(
            best_effort_recovery=best_effort_recovery,
            audit_adapter=capturing_adapter,
        )
        corrupt = _corrupt_file(tmp_path)

        # When
        with (
            capture_logs() as logs,
            patch("baldur.metrics.drift_metrics.record_wal_corruption") as mock_metric,
        ):
            _read(wal, corrupt, mode)

        # Then: log, metric, adapter and the stats counter all fired once.
        detections = log_events(logs, "wal.corruption_detected")
        assert len(detections) == 1
        assert detections[0]["log_level"] == "warning"
        assert detections[0]["filepath"] == str(corrupt)
        assert mock_metric.call_count == 1
        assert capturing_adapter.actions() == ["WAL_CORRUPTION_DETECTED"]
        assert wal.get_stats().corrupted_entries == 1

    @pytest.mark.parametrize("mode", READ_MODES, ids=READ_MODES)
    def test_report_carries_the_records_own_checksums(
        self, tmp_path, make_wal, capturing_adapter, mode
    ):
        """A report naming a placeholder instead of the real pair cannot be
        used to tell one corrupt record from another.
        """
        wal = make_wal(audit_adapter=capturing_adapter)
        corrupt = _corrupt_file(tmp_path)
        expected_computed = _crc32_of_corrupt_record()

        with capture_logs() as logs:
            _read(wal, corrupt, mode)

        detection = log_events(logs, "wal.corruption_detected")[0]
        assert detection["expected_checksum"] == BAD_CHECKSUM.decode("ascii")
        assert detection["computed_checksum"] == expected_computed

        delivered = capturing_adapter.entries[0].details
        assert delivered["expected_checksum"] == BAD_CHECKSUM.decode("ascii")
        assert delivered["computed_checksum"] == expected_computed
        assert delivered["source"] == "WriteAheadLog"

    @pytest.mark.parametrize("mode", READ_MODES, ids=READ_MODES)
    def test_on_corruption_callback_fires_in_every_read_mode(
        self, tmp_path, make_wal, mode
    ):
        """The host's own corruption hook is part of the unconditional report."""
        seen: list[WALCorruptionError] = []
        wal = make_wal(on_corruption=seen.append)
        corrupt = _corrupt_file(tmp_path)

        _read(wal, corrupt, mode)

        assert len(seen) == 1
        assert isinstance(seen[0], WALCorruptionError)
        assert seen[0].expected == BAD_CHECKSUM.decode("ascii")
        assert seen[0].computed == _crc32_of_corrupt_record()

    def test_an_intact_file_reports_no_corruption(
        self, tmp_path, make_wal, capturing_adapter
    ):
        """Non-vacuity guard for the whole class: the report is driven by the
        bad record, not by reading a file at all.
        """
        wal = make_wal(audit_adapter=capturing_adapter)
        intact = write_raw_wal_file(
            tmp_path / own_pid_wal_name(WAL_PREFIX, "20260101_000000"),
            [RawRecord(sequence=1), RawRecord(sequence=2)],
        )

        with capture_logs() as logs:
            entries = _read(wal, intact, "best_effort")

        assert [e.sequence for e in entries] == [1, 2]
        assert log_events(logs, "wal.corruption_detected") == []
        assert capturing_adapter.entries == []
        assert wal.get_stats().corrupted_entries == 0

    @pytest.mark.parametrize(
        ("mode", "best_effort_recovery", "expected_sequences"),
        [
            ("strict", True, [1, 3]),
            ("strict", False, [1, 3]),
            ("best_effort", True, [1, 3]),
            ("best_effort", False, [1]),
        ],
        ids=[
            "strict_recovery_on_continues",
            "strict_recovery_off_continues",
            "best_effort_recovery_on_continues",
            "best_effort_recovery_off_stops",
        ],
    )
    def test_read_mode_still_decides_whether_the_read_continues(
        self, tmp_path, make_wal, mode, best_effort_recovery, expected_sequences
    ):
        """Only the control flow after the report is mode-dependent: the
        opt-out (``best_effort_recovery=False`` on a best-effort read) is the
        one arm that stops at the corruption boundary.
        """
        wal = make_wal(best_effort_recovery=best_effort_recovery)
        corrupt = _corrupt_file(tmp_path)

        entries = _read(wal, corrupt, mode)

        assert [e.sequence for e in entries] == expected_sequences

    def test_parallel_recovery_reports_corruption_it_used_to_swallow(
        self, tmp_path, make_wal, capturing_adapter
    ):
        """The regression that mattered: with two or more files
        ``recover_unprocessed`` reads every file best-effort, so before the
        hoist this path — the one a running drain takes — saw nothing at all.
        """
        wal = make_wal(audit_adapter=capturing_adapter)
        _corrupt_file(tmp_path, stamp="20260101_000000")
        write_raw_wal_file(
            tmp_path / own_pid_wal_name(WAL_PREFIX, "20260101_000001"),
            [RawRecord(sequence=4)],
        )

        with capture_logs() as logs:
            entries = wal.recover_unprocessed(last_processed_seq=0)

        assert [e.sequence for e in entries] == [1, 3, 4], (
            "the intact records around the corrupt one are still recovered"
        )
        assert len(log_events(logs, "wal.corruption_detected")) == 1
        assert capturing_adapter.actions() == ["WAL_CORRUPTION_DETECTED"]
