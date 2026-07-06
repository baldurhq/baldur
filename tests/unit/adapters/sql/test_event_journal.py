"""
Unit tests for SQLEventJournalRepository.

Coverage:
- append() assigns monotonically increasing sequence numbers.
- query() filters by event_type / service_name / region / time range.
- context_filters are applied Python-side (parity with memory adapter).
- get_sequence_range returns inclusive-lower, exclusive-upper window.
- get_latest_sequence / count behaviour.
- query truncation when result exceeds limit.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from baldur.adapters.sql.event_journal import SQLEventJournalRepository
from baldur.interfaces.event_journal import (
    JournalEntry,
    JournalQueryFilter,
)


def _make_entry(
    event_type: str = "cb.opened",
    service_name: str = "openai",
    timestamp: datetime | None = None,
    region: str = "",
    context: dict | None = None,
) -> JournalEntry:
    return JournalEntry(
        sequence=0,
        event_type=event_type,
        source="core",
        timestamp=timestamp or datetime.now(UTC),
        service_name=service_name,
        region=region,
        context=context or {},
    )


@pytest.fixture
def journal(get_sqlite_conn) -> SQLEventJournalRepository:
    return SQLEventJournalRepository(get_sqlite_conn)


class TestSQLEventJournalAppendBehavior:
    """append() is the core append-only primitive."""

    def test_append_returns_monotonically_increasing_sequence(self, journal):
        s1 = journal.append(_make_entry())
        s2 = journal.append(_make_entry())
        s3 = journal.append(_make_entry())
        assert s1 < s2 < s3

    def test_append_stores_all_fields(self, journal):
        ts = datetime(2026, 4, 14, 12, 0, 0, tzinfo=UTC)
        seq = journal.append(
            _make_entry(
                event_type="cb.opened",
                service_name="openai",
                timestamp=ts,
                region="us-east-1",
                context={"failure_count": 5},
            )
        )
        entries = journal.get_sequence_range(seq, seq + 1)
        assert len(entries) == 1
        e = entries[0]
        assert e.event_type == "cb.opened"
        assert e.service_name == "openai"
        assert e.region == "us-east-1"
        assert e.context == {"failure_count": 5}

    def test_get_latest_sequence_reflects_last_append(self, journal):
        assert journal.get_latest_sequence() == 0
        seq = journal.append(_make_entry())
        assert journal.get_latest_sequence() == seq


class TestSQLEventJournalQueryFilterBehavior:
    """query() filters by columnar fields."""

    def test_event_types_filter_returns_only_matching(self, journal):
        journal.append(_make_entry(event_type="cb.opened"))
        journal.append(_make_entry(event_type="cb.opened"))
        journal.append(_make_entry(event_type="cb.closed"))

        result = journal.query(JournalQueryFilter(event_types=["cb.opened"]))
        assert len(result.entries) == 2
        assert all(e.event_type == "cb.opened" for e in result.entries)

    def test_service_name_filter_returns_only_matching(self, journal):
        journal.append(_make_entry(service_name="openai"))
        journal.append(_make_entry(service_name="stripe"))
        result = journal.query(JournalQueryFilter(service_name="openai"))
        assert len(result.entries) == 1
        assert result.entries[0].service_name == "openai"

    def test_region_filter_returns_only_matching(self, journal):
        journal.append(_make_entry(region="us-east-1"))
        journal.append(_make_entry(region="eu-west-1"))
        result = journal.query(JournalQueryFilter(region="eu-west-1"))
        assert len(result.entries) == 1

    def test_time_range_filter_is_inclusive_lower_exclusive_upper(self, journal):
        t0 = datetime(2026, 4, 14, 10, 0, 0, tzinfo=UTC)
        t1 = t0 + timedelta(hours=1)
        t2 = t0 + timedelta(hours=2)
        journal.append(_make_entry(timestamp=t0))
        journal.append(_make_entry(timestamp=t1))
        journal.append(_make_entry(timestamp=t2))

        result = journal.query(JournalQueryFilter(start_time=t1, end_time=t2))
        # t1 included, t2 excluded → exactly one entry.
        assert len(result.entries) == 1

    def test_context_filters_apply_in_python(self, journal):
        journal.append(_make_entry(context={"tenant": "acme"}))
        journal.append(_make_entry(context={"tenant": "globex"}))

        result = journal.query(JournalQueryFilter(context_filters={"tenant": "acme"}))
        assert len(result.entries) == 1
        assert result.entries[0].context["tenant"] == "acme"


class TestSQLEventJournalSequenceRangeBehavior:
    """get_sequence_range returns [start, end)."""

    def test_range_is_lower_inclusive_upper_exclusive(self, journal):
        sequences = [journal.append(_make_entry()) for _ in range(5)]
        start, end = sequences[1], sequences[4]
        got = journal.get_sequence_range(start, end)
        assert [e.sequence for e in got] == sequences[1:4]


class TestSQLEventJournalCountBehavior:
    """count() mirrors query() filtering."""

    def test_count_returns_filtered_total(self, journal):
        for _ in range(3):
            journal.append(_make_entry(event_type="cb.opened"))
        journal.append(_make_entry(event_type="cb.closed"))

        assert journal.count(JournalQueryFilter(event_types=["cb.opened"])) == 3
        assert journal.count(JournalQueryFilter()) == 4

    def test_count_with_context_filters_matches_python_semantics(self, journal):
        journal.append(_make_entry(context={"tenant": "acme"}))
        journal.append(_make_entry(context={"tenant": "globex"}))
        assert (
            journal.count(JournalQueryFilter(context_filters={"tenant": "acme"})) == 1
        )


class TestSQLEventJournalTruncationBehavior:
    """query() flags truncated=True when total > limit."""

    def test_truncated_flag_set_when_result_exceeds_limit(self, journal):
        for _ in range(5):
            journal.append(_make_entry())

        result = journal.query(JournalQueryFilter(limit=2))
        assert len(result.entries) == 2
        assert result.truncated is True
        assert result.total_count == 5
