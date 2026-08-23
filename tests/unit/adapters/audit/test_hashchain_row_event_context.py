"""A ledger row records the event's context, not the appending thread's.

The hash-chain adapter stamped every row with ``utc_now()`` and the ambient
trace of whatever thread happened to append it. For rows appended by the
request path that was close enough; for rows drained out of the WAL — written
minutes earlier, on another thread, possibly after a restart — it was wrong in
the two fields a compliance ledger is read for. A range query over "what
happened between 02:00 and 03:00" returned the rows *drained* in that window,
and the trace column pointed at an unrelated request.

``_build_entry()`` now honours the entry's own timestamp and its trace pair,
falling back to the appending thread only for an entry that carries neither.
The trace pair is taken **together**: a row must never mix one event's short
id with another thread's ambient traceparent.

Row timestamps are consequently no longer monotonically non-decreasing along
the chain sequence. That is intended — the chain is verified by hash linkage,
not by timestamp ordering — and it is what makes range queries correct.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from baldur.adapters.audit.hashchain_adapter import HashChainFileAuditLogAdapter
from baldur.interfaces.audit_adapter import AuditAction, AuditEntry

ADAPTER_MODULE = "baldur.adapters.audit.hashchain_adapter"

# A clearly-past event time, distinguishable from any append-time stamp.
EVENT_TIME = datetime(2020, 6, 15, 12, 0, 0, tzinfo=UTC)
APPEND_TIME = datetime(2026, 7, 1, 9, 30, 0, tzinfo=UTC)

EVENT_TRACE_ID = "trace-of-the-event"
EVENT_TRACE_FULL = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"
AMBIENT_TRACE_ID = "trace-of-the-appending-thread"
AMBIENT_TRACE_FULL = "00-99999999999999999999999999999999-8888888888888888-01"


@pytest.fixture
def adapter(tmp_path):
    """A real adapter over ``tmp_path`` — rows land on disk as an operator
    would read them."""
    instance = HashChainFileAuditLogAdapter(
        log_dir=str(tmp_path),
        enable_hash_chain=True,
        use_file_lock=False,
        enable_anchor_backup=False,
    )
    yield instance
    instance.close()


@pytest.fixture
def ambient_trace():
    """Pin the appending thread's ambient trace to a recognisable value."""
    with (
        patch(f"{ADAPTER_MODULE}.get_trace_id", return_value=AMBIENT_TRACE_ID),
        patch(f"{ADAPTER_MODULE}.get_trace_id_full", return_value=AMBIENT_TRACE_FULL),
    ):
        yield


def _rows(log_dir: Path) -> list[dict]:
    rows: list[dict] = []
    for path in sorted(log_dir.glob("audit_*.jsonl")):
        with open(path, encoding="utf-8") as handle:
            rows.extend(json.loads(line) for line in handle if line.strip())
    return rows


def _entry(**overrides) -> AuditEntry:
    """A drained CB entry: its own timestamp and its own trace pair."""
    kwargs = {
        "action": AuditAction.CB_FORCE_OPEN,
        "timestamp": EVENT_TIME,
        "target_type": "circuit_breaker",
        "target_id": "payment-pg",
        "actor_id": "alice",
        "reason": "operator force",
        "details": {
            "trace_id": EVENT_TRACE_ID,
            "trace_id_full": EVENT_TRACE_FULL,
        },
    }
    kwargs.update(overrides)
    return AuditEntry(**kwargs)


class TestRowRecordsEventContext:
    """``_build_entry()`` records the event's time and trace, not the
    appending thread's."""

    # --- timestamp -----------------------------------------------------------

    def test_row_timestamp_is_the_events_own_time(self, adapter, tmp_path):
        """The forensic guarantee: a row drained long after the event still
        says when the event happened."""
        with patch(f"{ADAPTER_MODULE}.utc_now", return_value=APPEND_TIME):
            adapter.log(_entry())

        assert _rows(tmp_path)[0]["timestamp"] == EVENT_TIME.isoformat()

    def test_row_timestamp_is_not_the_append_time(self, adapter, tmp_path):
        """The explicit negative: the pre-fix row carried the append time, so
        pinning only "equals the event time" would pass for a clock that
        happened to agree."""
        with patch(f"{ADAPTER_MODULE}.utc_now", return_value=APPEND_TIME):
            adapter.log(_entry())

        assert _rows(tmp_path)[0]["timestamp"] != APPEND_TIME.isoformat()

    def test_row_timestamp_falls_back_to_now_without_an_event_time(self, adapter):
        """An entry carrying no usable timestamp still gets a row — the
        appending clock is the fallback, not a crash and not an empty field."""
        built = adapter._build_entry({"config_type": "x", "action": "config_change"})

        assert built["timestamp"]

    @pytest.mark.parametrize(
        "raw",
        ["2020-06-15T12:00:00+00:00", 1592222400.0, None, ""],
        ids=["iso-string", "float-epoch", "none", "empty"],
    )
    def test_a_non_datetime_event_time_degrades_to_the_append_clock(self, adapter, raw):
        """``_build_entry`` consumes a ``datetime``; anything else is a
        malformed carrier, and must degrade rather than reach ``.isoformat()``
        and stall the drain."""
        with patch(f"{ADAPTER_MODULE}.utc_now", return_value=APPEND_TIME):
            built = adapter._build_entry(
                {"config_type": "x", "action": "config_change", "h1_timestamp": raw}
            )

        assert built["timestamp"] == APPEND_TIME.isoformat()

    def test_rows_keep_event_order_not_append_order(self, adapter, tmp_path):
        """The consequence worth stating: two events appended in one batch
        carry their own times, so a range query over the ledger selects by
        when things happened."""
        earlier = datetime(2020, 1, 1, 0, 0, 0, tzinfo=UTC)
        later = datetime(2020, 6, 15, 12, 0, 0, tzinfo=UTC)

        adapter.log(_entry(timestamp=later, target_id="second-event"))
        adapter.log(_entry(timestamp=earlier, target_id="first-event"))

        stamps = [row["timestamp"] for row in _rows(tmp_path)]
        assert stamps == [later.isoformat(), earlier.isoformat()]

    # --- trace pair ----------------------------------------------------------

    def test_row_carries_the_events_trace_pair(self, adapter, tmp_path, ambient_trace):
        """Both halves come from the event."""
        adapter.log(_entry())

        row = _rows(tmp_path)[0]
        assert row["trace_id"] == EVENT_TRACE_ID
        assert row["trace_id_full"] == EVENT_TRACE_FULL

    def test_the_ambient_trace_does_not_leak_into_the_row(
        self, adapter, tmp_path, ambient_trace
    ):
        """The negative half: the appending thread's trace is present and
        recognisable, and must not appear anywhere in the row."""
        adapter.log(_entry())

        row = _rows(tmp_path)[0]
        assert AMBIENT_TRACE_ID not in json.dumps(row)
        assert AMBIENT_TRACE_FULL not in json.dumps(row)

    def test_a_half_carried_trace_is_not_completed_from_the_thread(
        self, adapter, tmp_path, ambient_trace
    ):
        """Taken together: an event carrying only the short id must NOT have
        the appending thread's traceparent grafted on — the row would then
        claim a correlation that never existed."""
        adapter.log(_entry(details={"trace_id": EVENT_TRACE_ID}))

        row = _rows(tmp_path)[0]
        assert row["trace_id"] == EVENT_TRACE_ID
        assert row.get("trace_id_full") is None

    def test_a_full_only_trace_is_not_completed_from_the_thread(
        self, adapter, tmp_path, ambient_trace
    ):
        """The mirror case: only the traceparent carried."""
        adapter.log(_entry(details={"trace_id_full": EVENT_TRACE_FULL}))

        row = _rows(tmp_path)[0]
        assert row["trace_id_full"] == EVENT_TRACE_FULL
        assert row.get("trace_id") is None

    def test_an_event_with_no_trace_uses_the_appending_thread(
        self, adapter, tmp_path, ambient_trace
    ):
        """The fallback the request path relies on: an entry that carries no
        trace at all is still correlated to the work that recorded it."""
        adapter.log(_entry(details={}))

        row = _rows(tmp_path)[0]
        assert row["trace_id"] == AMBIENT_TRACE_ID
        assert row["trace_id_full"] == AMBIENT_TRACE_FULL

    # --- round trip ----------------------------------------------------------

    def test_the_trace_pair_round_trips_back_into_details(self, adapter):
        """``_entry_to_event_dict`` lifts the pair out of ``details`` to the
        top level, so ``query()`` must put it back — otherwise reading a row
        would silently lose the correlation the write preserved."""
        adapter.log(_entry())

        recovered = adapter.query(limit=10)

        assert len(recovered) == 1
        assert recovered[0].details["trace_id"] == EVENT_TRACE_ID
        assert recovered[0].details["trace_id_full"] == EVENT_TRACE_FULL

    def test_the_event_time_round_trips(self, adapter):
        """The recovered entry reports the event's time, closing the loop the
        row-level assertions open."""
        with patch(f"{ADAPTER_MODULE}.utc_now", return_value=APPEND_TIME):
            adapter.log(_entry())

        recovered = adapter.query(limit=10)

        assert recovered[0].timestamp == EVENT_TIME

    def test_the_chain_still_verifies_with_out_of_order_timestamps(self, adapter):
        """Integrity does not depend on timestamp ordering: the chain is
        verified by hash linkage, which is why honouring event times is safe."""
        adapter.log(_entry(timestamp=datetime(2026, 1, 1, tzinfo=UTC)))
        adapter.log(_entry(timestamp=datetime(2020, 1, 1, tzinfo=UTC)))

        valid, issues = adapter.verify_integrity()

        assert valid is True
        assert issues == []
