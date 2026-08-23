"""Unit tests for WAL meta-event delivery and the rotation report (#763 D1).

A WAL meta-event used to fall through to ``_write_to_wal`` whenever no audit
adapter was wired — which is every production process — so the record saying
"this WAL was recovered" became content of the WAL it described, and the next
read found it and emitted another. ``_deliver_meta_event`` now delivers to a
host-wired adapter or does nothing at all, and every emitting call site carries
its own unconditional log line and counter instead.

Verification techniques per UNIT_TEST_GUIDELINES §8:
- Dependency interaction: the adapter receives one ``AuditEntry`` per event,
  with the emitting component tagged into ``details["source"]``
- Negative side effect: with no adapter the WAL is byte-identical and its
  entry/sequence counters do not move
- Exception & edge case: a raising adapter is reported and swallowed, never
  propagated into the write path the delivery runs under
- Side effects: ``wal.file_rotated`` INFO + the rotation counter fire on every
  rotation, adapter or not
- Branch outcome: a rotation with no previous file reports nothing

The negative assertion at the *call sites* (a rotation and a checksum mismatch
driven end to end with ``audit_adapter=None`` leave the WAL directory's byte
count unchanged) lives in
``tests/unit/audit/forensic_bridge/test_context_injection.py``; this module
pins the delivery primitive itself.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from structlog.testing import capture_logs

from baldur.audit.wal import WALConfig, WriteAheadLog
from tests.factories.writable_dir import log_events

WAL_PREFIX = "meta_event_wal"

META_EVENT_TYPES = ("WAL_ROTATED", "WAL_CORRUPTION_DETECTED")


@pytest.fixture
def wal_config(tmp_path: Path) -> WALConfig:
    return WALConfig(
        wal_dir=str(tmp_path),
        sync_on_write=False,
        max_files=1000,
        file_prefix=WAL_PREFIX,
    )


def _wal_dir_bytes(wal_dir: Path) -> int:
    return sum(f.stat().st_size for f in wal_dir.glob(f"{WAL_PREFIX}_*.wal"))


# =============================================================================
# Behavior: _deliver_meta_event
# =============================================================================


class TestWALMetaEventDeliveryBehavior:
    """``_deliver_meta_event`` delivers to a wired adapter, and to nothing else."""

    @pytest.mark.parametrize("event_type", META_EVENT_TYPES, ids=META_EVENT_TYPES)
    def test_meta_event_reaches_a_wired_adapter_with_its_source_tagged(
        self, wal_config, capturing_adapter, event_type
    ):
        """``AuditEntry`` has no source field, so the emitting component is
        merged into ``details`` — an operator reading the trail must still be
        able to tell the WAL wrote this.
        """
        wal = WriteAheadLog(config=wal_config, audit_adapter=capturing_adapter)

        wal._deliver_meta_event(event_type=event_type, details={"filepath": "/x.wal"})
        wal.close()

        assert capturing_adapter.actions() == [event_type]
        delivered = capturing_adapter.entries[0]
        assert delivered.details["filepath"] == "/x.wal"
        assert delivered.details["source"] == "WriteAheadLog"

    @pytest.mark.parametrize("event_type", META_EVENT_TYPES, ids=META_EVENT_TYPES)
    def test_meta_event_without_an_adapter_writes_nothing_into_the_wal(
        self, wal_config, event_type, tmp_path
    ):
        """The core negative: a meta-event about this WAL must never become
        content of this WAL. Paired with the wired case above, which proves the
        same call does produce a delivery when there is somewhere to deliver.
        """
        # Given: a WAL with real content, and no adapter wired.
        wal = WriteAheadLog(config=wal_config)
        for i in range(5):
            wal.write({"event": f"real_{i}"})
        wal.flush()

        entries_before = wal.get_stats().total_entries
        sequence_before = wal.get_stats().last_sequence
        bytes_before = _wal_dir_bytes(tmp_path)

        # When
        wal._deliver_meta_event(event_type=event_type, details={"filepath": "/x.wal"})
        wal.flush()

        # Then: nothing was appended, and no sequence was consumed.
        stats = wal.get_stats()
        wal.close()
        assert stats.total_entries == entries_before
        assert stats.last_sequence == sequence_before
        assert _wal_dir_bytes(tmp_path) == bytes_before

    def test_raising_adapter_is_reported_as_a_warning_and_not_propagated(
        self, wal_config, capturing_adapter
    ):
        """Fail-open: delivery runs inside the write path (rotation happens
        under the write lock), so an adapter fault must not surface there.
        """
        capturing_adapter.raise_on_log = RuntimeError("central store down")
        wal = WriteAheadLog(config=wal_config, audit_adapter=capturing_adapter)

        with capture_logs() as logs:
            wal._deliver_meta_event(event_type="WAL_ROTATED", details={})
        wal.close()

        failures = log_events(logs, "wal.meta_event_delivery_failed")
        assert len(failures) == 1
        assert failures[0]["log_level"] == "warning"
        assert failures[0]["event_type"] == "WAL_ROTATED"
        assert "central store down" in failures[0]["error"]

    def test_a_raising_adapter_does_not_abort_the_rotation_it_reports(
        self, wal_config, capturing_adapter
    ):
        """The fault must fire on a real call site, not only on a direct call:
        the rotation still completes and still reclaims its handle.
        """
        capturing_adapter.raise_on_log = RuntimeError("central store down")
        wal = WriteAheadLog(config=wal_config, audit_adapter=capturing_adapter)
        wal.write({"event": "before_rotation"})
        wal.flush()

        with capture_logs() as logs:
            wal._rotate_file()

        assert log_events(logs, "wal.meta_event_delivery_failed"), (
            "the adapter fault must actually fire for this assertion to mean anything"
        )
        assert log_events(logs, "wal.file_rotated"), (
            "the unconditional channel survives a failing adapter"
        )
        # The rotation ran to completion: the handle was closed and released.
        assert wal._current_handle is None
        wal.write({"event": "after_rotation"})
        wal.close()


# =============================================================================
# Behavior: the rotation site's unconditional channel
# =============================================================================


class TestWALRotationReportingBehavior:
    """Rotation reports through a log line and a counter that do not depend on
    a wired adapter — the channels that replaced the WAL-write fallback.
    """

    def test_rotation_without_an_adapter_logs_the_retired_file_and_its_size(
        self, wal_config
    ):
        wal = WriteAheadLog(config=wal_config)  # audit_adapter=None
        wal.write({"event": "x" * 100})
        wal.flush()
        retired = wal._current_file
        retired_size = retired.stat().st_size

        with capture_logs() as logs:
            wal._rotate_file()
        wal.close()

        rotations = log_events(logs, "wal.file_rotated")
        assert len(rotations) == 1
        assert rotations[0]["log_level"] == "info"
        assert rotations[0]["old_file"] == str(retired)
        assert rotations[0]["old_size_bytes"] == retired_size
        assert rotations[0]["file_prefix"] == WAL_PREFIX

    def test_rotation_without_an_adapter_still_increments_the_counter(self, wal_config):
        wal = WriteAheadLog(config=wal_config)  # audit_adapter=None
        wal.write({"event": "x"})
        wal.flush()

        with patch("baldur.audit.wal.record_wal_rotation") as mock_counter:
            wal._rotate_file()
        wal.close()

        assert mock_counter.call_count == 1

    def test_rotation_with_no_previous_file_reports_nothing(self, wal_config):
        """Branch outcome: a WAL that never opened a file has nothing to
        retire, so neither channel fires and no adapter is consulted.
        """
        wal = WriteAheadLog(config=wal_config)  # no write yet
        assert wal._current_file is None, "precondition: no file was ever opened"

        with (
            capture_logs() as logs,
            patch("baldur.audit.wal.record_wal_rotation") as mock_counter,
            patch.object(wal, "_deliver_meta_event") as mock_delivery,
        ):
            wal._rotate_file()
        wal.close()

        assert log_events(logs, "wal.file_rotated") == []
        assert mock_counter.call_count == 0
        assert mock_delivery.call_count == 0
