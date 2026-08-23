"""Unit tests for the bounded WAL recovery read (#763 D3/D4).

``recover_unprocessed`` returned every entry above the cursor while the audit
drain consumed only ``batch_size`` of them, so each one-second cycle re-read
the whole retained backlog to use the first hundred entries of it — linear
per-cycle work, quadratic cumulative cost. The read now takes an optional
``limit``: ``None`` (the default, and what the eight callers that reconstruct
state keep using) replays the complete history, and a caller that consumes a
fixed budget passes that budget.

Verification techniques per UNIT_TEST_GUIDELINES §8:
- Boundary analysis: ``limit`` in {0, 1, backlog-1, backlog, backlog+1}
- Side effects: how many records the read actually consumes (the defect was
  the read volume, not the return value), the recovered counter, the metric
- Data ordering invariant: strictly ascending, every element above the cursor
- Property (hypothesis): ``len(result) <= limit`` for any limit and cursor —
  never ``== limit``, which a corrupt or short file makes false
- Exception & edge case: a checksum field the strict reader cannot decode must
  not cost the entries behind it
- Dependency interaction: the memory-guard path reports what it recovered

Files are stamped after the WAL is constructed: ``_init_or_recover``
strict-reads the last own-PID file, which would otherwise count a corruption
before the test's own read.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from hypothesis import HealthCheck, given
from hypothesis import settings as hyp_settings
from hypothesis import strategies as st
from structlog.testing import capture_logs

from baldur.audit.wal import WALConfig, WriteAheadLog
from tests.factories.wal_records import RawRecord, own_pid_wal_name, write_raw_wal_file
from tests.factories.writable_dir import log_events

WAL_PREFIX = "bounded_wal"

# A checksum field carrying a byte outside ASCII. ``_read_wal_file`` decodes
# the field with no ``errors=``, so a strict read ends the file silently here;
# the bounded read is best-effort precisely so the entries behind it survive.
UNDECODABLE_CHECKSUM = b"\xffeadbeef"


def _config(wal_dir: Path) -> WALConfig:
    return WALConfig(
        wal_dir=str(wal_dir),
        sync_on_write=False,
        file_prefix=WAL_PREFIX,
    )


@pytest.fixture
def wal(tmp_path: Path):
    """A WAL over an empty directory — tests stamp their own files after."""
    instance = WriteAheadLog(config=_config(tmp_path))
    yield instance
    instance.close()


def _stamp(wal_dir: Path, stamp: str, sequences, corrupt_at: int | None = None) -> Path:
    records = [
        RawRecord(
            sequence=seq,
            checksum=UNDECODABLE_CHECKSUM if seq == corrupt_at else None,
        )
        for seq in sequences
    ]
    return write_raw_wal_file(wal_dir / own_pid_wal_name(WAL_PREFIX, stamp), records)


def _count_consumed(wal: WriteAheadLog, consumed: list[int]):
    """Patch the WAL's best-effort reader so every record it actually pulls
    off disk is recorded. The bound being tested is on the read, and the
    return value alone cannot distinguish a bounded read from a full one.
    """
    original = wal._read_wal_file_best_effort

    def counting(filepath):
        for entry in original(filepath):
            consumed.append(entry.sequence)
            yield entry

    return patch.object(wal, "_read_wal_file_best_effort", counting)


# =============================================================================
# Contract: the default keeps the complete replay every other caller needs
# =============================================================================


class TestWALBoundedRecoveryContract:
    """``limit`` is opt-in per call and never a new default."""

    def test_limit_parameter_defaults_to_none(self):
        """Design contract: the bound must be requested, so the eight callers
        that reconstruct state are untouched by this change.
        """
        import inspect

        from baldur.audit.wal._reader import WALReaderMixin

        signature = inspect.signature(WALReaderMixin.recover_unprocessed)
        assert signature.parameters["limit"].default is None

    def test_default_call_returns_the_complete_history_above_the_cursor(
        self, wal, tmp_path
    ):
        _stamp(tmp_path, "20260101_000000", range(1, 11))

        entries = wal.recover_unprocessed(last_processed_seq=0)

        assert [e.sequence for e in entries] == list(range(1, 11))


# =============================================================================
# Behavior: the bounded read
# =============================================================================


class TestWALBoundedRecovery:
    """``recover_unprocessed(limit=N)`` returns the globally lowest ``N``
    unprocessed entries and reads no more than it has to.
    """

    @pytest.mark.parametrize(
        ("limit", "expected"),
        [
            (0, []),
            (1, [1]),
            (9, list(range(1, 10))),
            (10, list(range(1, 11))),
            (11, list(range(1, 11))),
        ],
        ids=["zero", "one", "below_backlog", "at_backlog", "above_backlog"],
    )
    def test_limit_caps_the_result_at_the_lowest_sequences(
        self, wal, tmp_path, limit, expected
    ):
        _stamp(tmp_path, "20260101_000000", range(1, 11))

        entries = wal.recover_unprocessed(last_processed_seq=0, limit=limit)

        assert [e.sequence for e in entries] == expected

    def test_result_starts_above_the_cursor_and_ascends(self, wal, tmp_path):
        _stamp(tmp_path, "20260101_000000", range(1, 21))

        entries = wal.recover_unprocessed(last_processed_seq=12, limit=5)

        sequences = [e.sequence for e in entries]
        assert sequences == [13, 14, 15, 16, 17]
        assert all(seq > 12 for seq in sequences)

    def test_the_read_stops_at_the_budget_instead_of_draining_the_backlog(
        self, wal, tmp_path
    ):
        """The defect itself: a cycle that delivers ``limit`` entries must not
        pull the whole retained backlog off disk to find them.
        """
        _stamp(tmp_path, "20260101_000000", range(1, 51))
        _stamp(tmp_path, "20260101_000001", range(51, 101))
        consumed: list[int] = []

        with _count_consumed(wal, consumed):
            entries = wal.recover_unprocessed(last_processed_seq=0, limit=5)

        assert [e.sequence for e in entries] == [1, 2, 3, 4, 5]
        # Per-file cap: at most ``limit`` records off each of the two files,
        # against a 100-entry backlog.
        assert len(consumed) == 10

    def test_unbounded_read_still_drains_the_whole_backlog(self, wal, tmp_path):
        """Non-vacuity guard for the assertion above: the same corpus read
        without a limit does consume everything, so the count is measuring the
        bound and not the corpus.
        """
        _stamp(tmp_path, "20260101_000000", range(1, 51))
        _stamp(tmp_path, "20260101_000001", range(51, 101))
        consumed: list[int] = []

        with _count_consumed(wal, consumed):
            entries = wal.recover_unprocessed(last_processed_seq=0)

        assert len(entries) == 100
        assert len(consumed) == 100

    def test_lowest_sequences_win_over_filename_order(self, wal, tmp_path):
        """C9 — filenames are timestamp-stamped, so a backward clock step
        across a rotation orders a newer file first. Stopping at the first file
        that fills the budget would return the *higher* sequences and strand
        the lower ones behind an already-advanced cursor.
        """
        # The earlier-sorting name holds the higher sequences.
        _stamp(tmp_path, "20250101_000000", range(100, 110))
        _stamp(tmp_path, "20260101_000000", range(1, 10))

        entries = wal.recover_unprocessed(last_processed_seq=0, limit=5)

        assert [e.sequence for e in entries] == [1, 2, 3, 4, 5]

    def test_an_undecodable_checksum_does_not_cost_the_entries_behind_it(
        self, wal, tmp_path
    ):
        """C8 — the bounded read is best-effort for this reason: a strict read
        ends the file silently at a checksum byte outside ASCII, so a caller
        advancing its cursor from a later file's entries would lose everything
        between the corruption and that file's end.
        """
        _stamp(tmp_path, "20260101_000000", range(1, 7), corrupt_at=3)
        _stamp(tmp_path, "20260101_000001", range(7, 11))

        first_cycle = wal.recover_unprocessed(last_processed_seq=0, limit=3)
        assert [e.sequence for e in first_cycle] == [1, 2, 4], (
            "the corrupt record is dropped, the ones behind it are not"
        )

        second_cycle = wal.recover_unprocessed(last_processed_seq=4, limit=3)
        assert [e.sequence for e in second_cycle] == [5, 6, 7], (
            "the first file's tail is still read before the next file's head"
        )

    def test_counters_advance_by_what_was_returned_not_by_what_was_read(
        self, wal, tmp_path
    ):
        """Two files of five, a budget of three: the read pulls six records and
        returns three. A counter fed by the read would over-report recovery.
        """
        _stamp(tmp_path, "20260101_000000", range(1, 6))
        _stamp(tmp_path, "20260101_000001", range(6, 11))
        recovered_before = wal.get_stats().recovered_entries
        consumed: list[int] = []

        with (
            _count_consumed(wal, consumed),
            patch(
                "baldur.metrics.drift_metrics.record_wal_entries_recovered"
            ) as mock_metric,
        ):
            entries = wal.recover_unprocessed(last_processed_seq=0, limit=3)

        assert len(entries) == 3
        assert len(consumed) == 6
        assert wal.get_stats().recovered_entries == recovered_before + 3
        mock_metric.assert_called_once_with(3)

    def test_empty_result_does_not_touch_the_recovered_metric(self, wal, tmp_path):
        """An idle cycle with nothing above the cursor must not report a
        recovery of zero entries.
        """
        _stamp(tmp_path, "20260101_000000", range(1, 6))

        with patch(
            "baldur.metrics.drift_metrics.record_wal_entries_recovered"
        ) as mock_metric:
            entries = wal.recover_unprocessed(last_processed_seq=5, limit=3)

        assert entries == []
        assert mock_metric.call_count == 0


# =============================================================================
# Property: the bound holds for every limit and cursor
# =============================================================================


class TestWALBoundedRecoveryProperty:
    """The bounded read's contract is a property, not an example."""

    BACKLOG = 40
    CORRUPT_SEQUENCE = 17

    @pytest.fixture(scope="class")
    def property_wal(self, tmp_path_factory):
        """One corpus for every example — the read is non-destructive.

        A corrupt record sits in the middle on purpose: it is what makes
        ``len(result) == limit`` false, so a test asserting equality instead of
        the inequality would be wrong rather than merely weaker.
        """
        wal_dir = tmp_path_factory.mktemp("bounded_property")
        instance = WriteAheadLog(config=_config(wal_dir))
        for index, chunk in enumerate(
            (
                range(1, self.BACKLOG // 2 + 1),
                range(self.BACKLOG // 2 + 1, self.BACKLOG + 1),
            )
        ):
            _stamp(
                wal_dir,
                f"2026010{index}_000000",
                chunk,
                corrupt_at=self.CORRUPT_SEQUENCE,
            )
        yield instance
        instance.close()

    @given(
        limit=st.integers(min_value=1, max_value=BACKLOG + 5),
        cursor=st.integers(min_value=0, max_value=BACKLOG),
    )
    @hyp_settings(
        max_examples=50,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    def test_result_never_exceeds_the_limit_and_stays_above_the_cursor(
        self, property_wal, limit, cursor
    ):
        entries = property_wal.recover_unprocessed(
            last_processed_seq=cursor, limit=limit
        )

        sequences = [e.sequence for e in entries]
        assert len(sequences) <= limit
        assert sequences == sorted(set(sequences))
        assert all(seq > cursor for seq in sequences)


# =============================================================================
# Behavior: the memory-guarded chunked path
# =============================================================================


class TestWALChunkedRecovery:
    """The chunked path returns before the shared metric and completion log,
    so it carries both itself — otherwise a memory-guarded recovery is the one
    recovery an operator cannot see.
    """

    def test_memory_guard_trip_reports_what_the_chunked_pass_recovered(
        self, wal, tmp_path
    ):
        # Given: two files, and an available-memory answer that admits the
        # first file's estimate and not both.
        first = _stamp(tmp_path, "20260101_000000", range(1, 6))
        _stamp(tmp_path, "20260101_000001", range(6, 11))
        available = first.stat().st_size * 3 + 1

        # When
        with (
            capture_logs() as logs,
            patch(
                "baldur.core.resource_monitor.CgroupResourceMonitor"
                ".get_available_memory_bytes",
                return_value=available,
            ),
            patch(
                "baldur.metrics.drift_metrics.record_wal_entries_recovered"
            ) as mock_metric,
        ):
            entries = wal.recover_unprocessed(last_processed_seq=0)

        # Then: the guard tripped and the chunked pass reported itself.
        assert log_events(logs, "wal.recovery_memory_guard_blocked"), (
            "the memory guard must actually trip for this test to mean anything"
        )
        assert [e.sequence for e in entries] == [1, 2, 3, 4, 5]

        completed = log_events(logs, "wal.chunked_recovery_completed")
        assert len(completed) == 1
        assert completed[0]["recovered_count"] == 5
        assert completed[0]["last_processed_seq"] == 0
        assert completed[0]["new_last_seq"] == 5
        mock_metric.assert_called_once_with(5)
        assert wal.get_stats().recovered_entries == 5

        skipped = log_events(logs, "wal.chunked_recovery_file_skipped")
        assert len(skipped) == 1, "the file that did not fit is named, not dropped"

    def test_chunked_pass_that_recovers_nothing_reports_nothing(self, wal, tmp_path):
        """Branch outcome: with the cursor past every entry the completion log
        and the metric stay silent rather than reporting a zero recovery.
        """
        first = _stamp(tmp_path, "20260101_000000", range(1, 6))
        _stamp(tmp_path, "20260101_000001", range(6, 11))
        available = first.stat().st_size * 3 + 1

        with (
            capture_logs() as logs,
            patch(
                "baldur.core.resource_monitor.CgroupResourceMonitor"
                ".get_available_memory_bytes",
                return_value=available,
            ),
            patch(
                "baldur.metrics.drift_metrics.record_wal_entries_recovered"
            ) as mock_metric,
        ):
            entries = wal.recover_unprocessed(last_processed_seq=10)

        assert entries == []
        assert log_events(logs, "wal.recovery_memory_guard_blocked")
        assert log_events(logs, "wal.chunked_recovery_completed") == []
        assert mock_metric.call_count == 0
