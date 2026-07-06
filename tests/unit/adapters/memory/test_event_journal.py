"""
Unit tests for InMemoryEventJournalRepository.

검증 항목:
- append/query/count 기본 동작
- 시퀀스 단조 증가
- max_entries 초과 시 오래된 엔트리 삭제
- 필터링 (event_types, service_name, time range, region)
- get_sequence_range 동작
- get_latest_sequence 동작
- 스레드 안전성
- 쿼리 truncation

테스트 대상: baldur.adapters.memory.event_journal
"""

import threading
from datetime import UTC, datetime

from baldur.adapters.memory.event_journal import InMemoryEventJournalRepository
from baldur.interfaces.event_journal import (
    JournalEntry,
    JournalQueryFilter,
)


def _make_entry(
    event_type: str = "circuit_breaker_opened",
    source: str = "test",
    service_name: str = "svc-a",
    timestamp: datetime | None = None,
    region: str = "",
    context: dict | None = None,
) -> JournalEntry:
    """테스트용 JournalEntry 생성 헬퍼."""
    return JournalEntry(
        sequence=0,
        event_type=event_type,
        source=source,
        timestamp=timestamp or datetime.now(UTC),
        service_name=service_name,
        region=region,
        context=context or {},
    )


class TestInMemoryEventJournalAppendBehavior:
    """append() 동작 검증."""

    def test_append_returns_monotonically_increasing_sequence(self):
        """append()는 단조 증가하는 시퀀스를 반환한다."""
        repo = InMemoryEventJournalRepository()
        seq1 = repo.append(_make_entry())
        seq2 = repo.append(_make_entry())
        seq3 = repo.append(_make_entry())
        assert seq1 == 1
        assert seq2 == 2
        assert seq3 == 3

    def test_append_assigns_sequence_to_stored_entry(self):
        """append()는 저장된 엔트리에 시퀀스를 할당한다."""
        repo = InMemoryEventJournalRepository()
        seq = repo.append(_make_entry(event_type="test_event"))

        result = repo.query(JournalQueryFilter())
        assert len(result.entries) == 1
        assert result.entries[0].sequence == seq
        assert result.entries[0].event_type == "test_event"

    def test_append_evicts_oldest_when_max_entries_exceeded(self):
        """max_entries 초과 시 가장 오래된 엔트리가 삭제된다."""
        repo = InMemoryEventJournalRepository(max_entries=3)
        repo.append(_make_entry(service_name="svc-1"))
        repo.append(_make_entry(service_name="svc-2"))
        repo.append(_make_entry(service_name="svc-3"))
        repo.append(_make_entry(service_name="svc-4"))

        result = repo.query(JournalQueryFilter())
        assert len(result.entries) == 3
        service_names = [e.service_name for e in result.entries]
        assert "svc-1" not in service_names
        assert "svc-4" in service_names


class TestInMemoryEventJournalQueryBehavior:
    """query() 동작 검증."""

    def test_query_returns_all_entries_with_no_filter(self):
        """필터 없이 query()하면 모든 엔트리를 반환한다."""
        repo = InMemoryEventJournalRepository()
        repo.append(_make_entry())
        repo.append(_make_entry())

        result = repo.query(JournalQueryFilter())
        assert len(result.entries) == 2
        assert result.truncated is False
        assert result.total_count == 2

    def test_query_filters_by_event_types(self):
        """event_types 필터가 올바르게 동작한다."""
        repo = InMemoryEventJournalRepository()
        repo.append(_make_entry(event_type="type_a"))
        repo.append(_make_entry(event_type="type_b"))
        repo.append(_make_entry(event_type="type_a"))

        result = repo.query(JournalQueryFilter(event_types=["type_a"]))
        assert len(result.entries) == 2
        assert all(e.event_type == "type_a" for e in result.entries)

    def test_query_filters_by_service_name(self):
        """service_name 필터가 올바르게 동작한다."""
        repo = InMemoryEventJournalRepository()
        repo.append(_make_entry(service_name="svc-a"))
        repo.append(_make_entry(service_name="svc-b"))

        result = repo.query(JournalQueryFilter(service_name="svc-a"))
        assert len(result.entries) == 1
        assert result.entries[0].service_name == "svc-a"

    def test_query_filters_by_time_range(self):
        """start_time/end_time 필터가 올바르게 동작한다 (start inclusive, end exclusive)."""
        repo = InMemoryEventJournalRepository()
        t1 = datetime(2026, 1, 1, 10, 0, 0, tzinfo=UTC)
        t2 = datetime(2026, 1, 1, 11, 0, 0, tzinfo=UTC)
        t3 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
        repo.append(_make_entry(timestamp=t1))
        repo.append(_make_entry(timestamp=t2))
        repo.append(_make_entry(timestamp=t3))

        result = repo.query(JournalQueryFilter(start_time=t2, end_time=t3))
        assert len(result.entries) == 1
        assert result.entries[0].timestamp == t2

    def test_query_filters_by_region(self):
        """region 필터가 올바르게 동작한다."""
        repo = InMemoryEventJournalRepository()
        repo.append(_make_entry(region="us-east-1"))
        repo.append(_make_entry(region="eu-west-1"))

        result = repo.query(JournalQueryFilter(region="us-east-1"))
        assert len(result.entries) == 1
        assert result.entries[0].region == "us-east-1"

    def test_query_truncates_when_exceeding_limit(self):
        """결과가 limit을 초과하면 truncated=True를 반환한다."""
        repo = InMemoryEventJournalRepository()
        for _ in range(5):
            repo.append(_make_entry())

        result = repo.query(JournalQueryFilter(limit=3))
        assert len(result.entries) == 3
        assert result.truncated is True
        assert result.total_count == 5

    def test_query_returns_entries_in_sequence_order(self):
        """query() 결과는 시퀀스 오름차순이다."""
        repo = InMemoryEventJournalRepository()
        repo.append(_make_entry(service_name="first"))
        repo.append(_make_entry(service_name="second"))
        repo.append(_make_entry(service_name="third"))

        result = repo.query(JournalQueryFilter())
        sequences = [e.sequence for e in result.entries]
        assert sequences == sorted(sequences)


class TestInMemoryEventJournalSequenceRangeBehavior:
    """get_sequence_range() 동작 검증."""

    def test_get_sequence_range_returns_inclusive_start_exclusive_end(self):
        """start_sequence inclusive, end_sequence exclusive로 조회한다."""
        repo = InMemoryEventJournalRepository()
        repo.append(_make_entry())  # seq=1
        repo.append(_make_entry())  # seq=2
        repo.append(_make_entry())  # seq=3
        repo.append(_make_entry())  # seq=4

        entries = repo.get_sequence_range(2, 4)
        assert len(entries) == 2
        assert entries[0].sequence == 2
        assert entries[1].sequence == 3

    def test_get_sequence_range_returns_empty_for_no_match(self):
        """매칭되는 시퀀스가 없으면 빈 리스트를 반환한다."""
        repo = InMemoryEventJournalRepository()
        repo.append(_make_entry())  # seq=1

        entries = repo.get_sequence_range(10, 20)
        assert entries == []


class TestInMemoryEventJournalLatestSequenceBehavior:
    """get_latest_sequence() 동작 검증."""

    def test_get_latest_sequence_returns_zero_when_empty(self):
        """비어있으면 0을 반환한다."""
        repo = InMemoryEventJournalRepository()
        assert repo.get_latest_sequence() == 0

    def test_get_latest_sequence_returns_last_appended_sequence(self):
        """마지막으로 추가된 엔트리의 시퀀스를 반환한다."""
        repo = InMemoryEventJournalRepository()
        repo.append(_make_entry())
        repo.append(_make_entry())
        seq3 = repo.append(_make_entry())
        assert repo.get_latest_sequence() == seq3


class TestInMemoryEventJournalCountBehavior:
    """count() 동작 검증."""

    def test_count_returns_total_matching_entries(self):
        """필터에 맞는 엔트리 수를 반환한다."""
        repo = InMemoryEventJournalRepository()
        repo.append(_make_entry(event_type="type_a"))
        repo.append(_make_entry(event_type="type_b"))
        repo.append(_make_entry(event_type="type_a"))

        assert repo.count(JournalQueryFilter(event_types=["type_a"])) == 2

    def test_count_with_no_filter_returns_all(self):
        """필터 없이 count()하면 전체 엔트리 수를 반환한다."""
        repo = InMemoryEventJournalRepository()
        repo.append(_make_entry())
        repo.append(_make_entry())
        repo.append(_make_entry())

        assert repo.count(JournalQueryFilter()) == 3


class TestInMemoryEventJournalThreadSafetyBehavior:
    """InMemoryEventJournalRepository 멀티스레드 접근 안전성 검증."""

    def test_concurrent_append_no_sequence_collision(self):
        """10개 스레드가 동시에 append해도 시퀀스 충돌 없음."""
        repo = InMemoryEventJournalRepository()
        sequences: list[int] = []
        lock = threading.Lock()

        def worker():
            for _ in range(10):
                seq = repo.append(_make_entry())
                with lock:
                    sequences.append(seq)

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(sequences) == 100
        assert len(set(sequences)) == 100  # no duplicates
