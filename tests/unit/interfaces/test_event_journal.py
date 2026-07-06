"""
Unit tests for Event Journal interface data models.

검증 항목:
- JournalEntry frozen 불변성
- JournalEntry 기본값 계약
- JournalQueryFilter 기본값 계약
- JournalQueryResult frozen 불변성

테스트 대상: baldur.interfaces.event_journal
"""

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime

import pytest

from baldur.interfaces.event_journal import (
    JournalEntry,
    JournalQueryFilter,
    JournalQueryResult,
)


class TestJournalEntryContract:
    """JournalEntry 설계 계약값 검증."""

    def test_context_default_is_empty_dict(self):
        """context 필드 기본값: 빈 딕셔너리."""
        entry = JournalEntry(
            sequence=1,
            event_type="test",
            source="unit",
            timestamp=datetime.now(UTC),
            service_name="svc",
        )
        assert entry.context == {}

    def test_region_default_is_empty_string(self):
        """region 필드 기본값: 빈 문자열."""
        entry = JournalEntry(
            sequence=1,
            event_type="test",
            source="unit",
            timestamp=datetime.now(UTC),
            service_name="svc",
        )
        assert entry.region == ""

    def test_tier_id_default_is_empty_string(self):
        """tier_id 필드 기본값: 빈 문자열."""
        entry = JournalEntry(
            sequence=1,
            event_type="test",
            source="unit",
            timestamp=datetime.now(UTC),
            service_name="svc",
        )
        assert entry.tier_id == ""


class TestJournalEntryImmutabilityBehavior:
    """JournalEntry frozen 불변성 검증."""

    def test_frozen_entry_prevents_sequence_mutation(self):
        """JournalEntry는 frozen이므로 sequence 변경 시 에러."""
        entry = JournalEntry(
            sequence=1,
            event_type="test",
            source="unit",
            timestamp=datetime.now(UTC),
            service_name="svc",
        )
        with pytest.raises(FrozenInstanceError):
            entry.sequence = 2

    def test_frozen_entry_prevents_event_type_mutation(self):
        """JournalEntry는 frozen이므로 event_type 변경 시 에러."""
        entry = JournalEntry(
            sequence=1,
            event_type="test",
            source="unit",
            timestamp=datetime.now(UTC),
            service_name="svc",
        )
        with pytest.raises(FrozenInstanceError):
            entry.event_type = "changed"


class TestJournalQueryFilterContract:
    """JournalQueryFilter 설계 계약값 검증."""

    def test_default_limit_is_1000(self):
        """limit 기본값: 1000."""
        f = JournalQueryFilter()
        assert f.limit == 1000

    def test_all_filter_fields_default_to_none(self):
        """필터 필드(event_types, service_name, start_time, end_time, region, context_filters)의 기본값: None."""
        f = JournalQueryFilter()
        assert f.event_types is None
        assert f.service_name is None
        assert f.start_time is None
        assert f.end_time is None
        assert f.region is None
        assert f.context_filters is None


class TestJournalQueryResultContract:
    """JournalQueryResult 설계 계약값 검증."""

    def test_total_count_default_is_none(self):
        """total_count 기본값: None."""
        result = JournalQueryResult(entries=[], truncated=False)
        assert result.total_count is None

    def test_frozen_result_prevents_mutation(self):
        """JournalQueryResult는 frozen이므로 truncated 변경 시 에러."""
        result = JournalQueryResult(entries=[], truncated=False)
        with pytest.raises(FrozenInstanceError):
            result.truncated = True
