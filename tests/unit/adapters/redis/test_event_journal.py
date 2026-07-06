"""
Unit tests for RedisEventJournalRepository.

검증 항목:
- append/query/count 동작 (Redis 모킹)
- 시퀀스 할당 (Redis INCR)
- 월별 파티셔닝 키 구조
- 직렬화/역직렬화 왕복
- TTL 설정
- 에러 핸들링 (역직렬화 실패)
- count() ZCARD 최적화 (필터 없는 경우)
- scan_iter 기반 키 탐색

테스트 대상: baldur.adapters.redis.event_journal
"""

import json
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from baldur.adapters.redis.event_journal import RedisEventJournalRepository
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
        timestamp=timestamp or datetime(2026, 3, 1, 12, 0, 0, tzinfo=UTC),
        service_name=service_name,
        region=region,
        context=context or {},
    )


def _setup_repo_with_entries(entries_data):
    """Mock Redis에 직렬화된 엔트리를 준비한다."""
    mock_redis = MagicMock()
    repo = RedisEventJournalRepository(redis_client=mock_redis)

    serialized = []
    for seq, entry in enumerate(entries_data, start=1):
        data = {
            "sequence": seq,
            "event_type": entry.get("event_type", "test"),
            "source": entry.get("source", "unit"),
            "timestamp": entry.get(
                "timestamp",
                datetime(2026, 3, 1, 12, 0, 0, tzinfo=UTC),
            ).isoformat(),
            "service_name": entry.get("service_name", "svc"),
            "context": entry.get("context", {}),
            "region": entry.get("region", ""),
            "tier_id": entry.get("tier_id", ""),
        }
        serialized.append(json.dumps(data))

    mock_redis.scan_iter.return_value = iter([b"baldur:journal:2026-03"])
    mock_redis.zrangebyscore.return_value = serialized
    return repo, mock_redis


class TestRedisEventJournalContract:
    """RedisEventJournalRepository 설계 계약값 검증."""

    def test_key_prefix_is_baldur_journal(self):
        """KEY_PREFIX 값: baldur:journal."""
        assert RedisEventJournalRepository.KEY_PREFIX == "baldur:journal"

    def test_sequence_key_is_baldur_journal_sequence(self):
        """SEQUENCE_KEY 값: baldur:journal:sequence."""
        assert RedisEventJournalRepository.SEQUENCE_KEY == "baldur:journal:sequence"

    def test_default_ttl_seconds_is_2592000(self):
        """기본 TTL: 2592000초 (30일)."""
        mock_redis = MagicMock()
        repo = RedisEventJournalRepository(redis_client=mock_redis)
        assert repo._ttl_seconds == 2592000

    def test_default_max_query_limit_is_10000(self):
        """기본 max_query_limit: 10000."""
        mock_redis = MagicMock()
        repo = RedisEventJournalRepository(redis_client=mock_redis)
        assert repo._max_query_limit == 10000


class TestRedisEventJournalAppendBehavior:
    """append() 동작 검증."""

    def test_append_increments_sequence_via_redis_incr(self):
        """append()는 Redis INCR로 시퀀스를 할당한다."""
        mock_redis = MagicMock()
        mock_redis.incr.return_value = 42
        mock_redis.ttl.return_value = 1000
        repo = RedisEventJournalRepository(redis_client=mock_redis)

        seq = repo.append(_make_entry())

        assert seq == 42
        mock_redis.incr.assert_called_once_with(
            RedisEventJournalRepository.SEQUENCE_KEY
        )

    def test_append_uses_monthly_partition_key(self):
        """append()는 월별 파티션 키에 ZADD한다."""
        mock_redis = MagicMock()
        mock_redis.incr.return_value = 1
        mock_redis.ttl.return_value = 1000
        repo = RedisEventJournalRepository(redis_client=mock_redis)

        ts = datetime(2026, 3, 15, 12, 0, 0, tzinfo=UTC)
        repo.append(_make_entry(timestamp=ts))

        zadd_call = mock_redis.zadd.call_args
        assert zadd_call[0][0] == "baldur:journal:2026-03"

    def test_append_sets_ttl_when_key_has_no_expiry(self):
        """새 파티션 키에 TTL이 없으면 expire를 설정한다."""
        mock_redis = MagicMock()
        mock_redis.incr.return_value = 1
        mock_redis.ttl.return_value = -1  # no TTL set
        repo = RedisEventJournalRepository(redis_client=mock_redis, ttl_seconds=86400)

        repo.append(_make_entry())

        mock_redis.expire.assert_called_once()
        expire_args = mock_redis.expire.call_args[0]
        assert expire_args[1] == 86400

    def test_append_skips_expire_when_ttl_exists(self):
        """파티션 키에 이미 TTL이 있으면 expire를 호출하지 않는다."""
        mock_redis = MagicMock()
        mock_redis.incr.return_value = 1
        mock_redis.ttl.return_value = 5000
        repo = RedisEventJournalRepository(redis_client=mock_redis)

        repo.append(_make_entry())

        mock_redis.expire.assert_not_called()

    def test_append_raises_on_zadd_failure(self):
        """zadd 실패 시 예외를 전파한다."""
        mock_redis = MagicMock()
        mock_redis.incr.return_value = 1
        mock_redis.zadd.side_effect = ConnectionError("Redis down")
        repo = RedisEventJournalRepository(redis_client=mock_redis)

        with pytest.raises(ConnectionError):
            repo.append(_make_entry())


class TestRedisEventJournalQueryBehavior:
    """query() 동작 검증."""

    def test_query_with_time_range_resolves_partition_keys(self):
        """start_time/end_time이 있으면 월별 파티션 키를 resolve한다."""
        mock_redis = MagicMock()
        mock_redis.zrangebyscore.return_value = []
        repo = RedisEventJournalRepository(redis_client=mock_redis)

        start = datetime(2026, 1, 15, tzinfo=UTC)
        end = datetime(2026, 3, 15, tzinfo=UTC)
        repo.query(JournalQueryFilter(start_time=start, end_time=end))

        # Should query 3 partition keys: 2026-01, 2026-02, 2026-03
        assert mock_redis.zrangebyscore.call_count == 3

    def test_query_without_time_range_uses_scan_iter(self):
        """시간 범위 없으면 scan_iter로 모든 파티션을 조회한다."""
        mock_redis = MagicMock()
        mock_redis.scan_iter.return_value = iter([b"baldur:journal:2026-03"])
        mock_redis.zrangebyscore.return_value = []
        repo = RedisEventJournalRepository(redis_client=mock_redis)

        repo.query(JournalQueryFilter())

        mock_redis.scan_iter.assert_called_once_with(
            match="baldur:journal:????-??", count=200
        )

    def test_query_filters_by_service_name(self):
        """service_name 필터가 올바르게 동작한다."""
        repo, _ = _setup_repo_with_entries(
            [
                {"service_name": "svc-a"},
                {"service_name": "svc-b"},
                {"service_name": "svc-a"},
            ]
        )

        result = repo.query(JournalQueryFilter(service_name="svc-a"))
        assert len(result.entries) == 2
        assert all(e.service_name == "svc-a" for e in result.entries)

    def test_query_truncates_at_limit(self):
        """limit 초과 시 truncated=True를 반환한다."""
        repo, _ = _setup_repo_with_entries(
            [{"service_name": f"svc-{i}"} for i in range(5)]
        )

        result = repo.query(JournalQueryFilter(limit=3))
        assert len(result.entries) == 3
        assert result.truncated is True
        assert result.total_count == 5

    def test_query_clamps_limit_to_max_query_limit(self):
        """filter.limit이 max_query_limit을 초과하면 클램프된다."""
        repo, _ = _setup_repo_with_entries(
            [{"service_name": f"svc-{i}"} for i in range(5)]
        )
        repo._max_query_limit = 2

        result = repo.query(JournalQueryFilter(limit=100))
        assert len(result.entries) == 2
        assert result.truncated is True


class TestRedisEventJournalSequenceRangeBehavior:
    """get_sequence_range() 동작 검증."""

    def test_get_sequence_range_uses_zrangebyscore(self):
        """get_sequence_range()는 zrangebyscore를 사용한다."""
        mock_redis = MagicMock()
        mock_redis.scan_iter.return_value = iter([b"baldur:journal:2026-03"])
        mock_redis.zrangebyscore.return_value = []
        repo = RedisEventJournalRepository(redis_client=mock_redis)

        repo.get_sequence_range(5, 10)

        call_args = mock_redis.zrangebyscore.call_args[0]
        assert call_args[1] == 5  # start_sequence
        assert call_args[2] == 9  # end_sequence - 1


class TestRedisEventJournalLatestSequenceBehavior:
    """get_latest_sequence() 동작 검증."""

    def test_get_latest_sequence_returns_zero_when_key_missing(self):
        """시퀀스 키가 없으면 0을 반환한다."""
        mock_redis = MagicMock()
        mock_redis.get.return_value = None
        repo = RedisEventJournalRepository(redis_client=mock_redis)

        assert repo.get_latest_sequence() == 0

    def test_get_latest_sequence_returns_integer_value(self):
        """Redis에서 읽은 시퀀스를 정수로 반환한다."""
        mock_redis = MagicMock()
        mock_redis.get.return_value = b"42"
        repo = RedisEventJournalRepository(redis_client=mock_redis)

        assert repo.get_latest_sequence() == 42


class TestRedisEventJournalSerializationBehavior:
    """직렬화/역직렬화 왕복 검증."""

    def test_serialize_deserialize_round_trip_preserves_data(self):
        """직렬화 → 역직렬화 시 모든 필드가 보존된다."""
        mock_redis = MagicMock()
        repo = RedisEventJournalRepository(redis_client=mock_redis)

        ts = datetime(2026, 3, 1, 12, 30, 0, tzinfo=UTC)
        original = JournalEntry(
            sequence=0,
            event_type="circuit_breaker_opened",
            source="cb-service",
            timestamp=ts,
            service_name="payment",
            context={"error_rate": 0.5},
            region="us-east-1",
            tier_id="tier-1",
        )

        serialized = repo._serialize(original, seq=99)
        restored = repo._deserialize(serialized)

        assert restored is not None
        assert restored.sequence == 99
        assert restored.event_type == "circuit_breaker_opened"
        assert restored.source == "cb-service"
        assert restored.timestamp == ts
        assert restored.service_name == "payment"
        assert restored.context == {"error_rate": 0.5}
        assert restored.region == "us-east-1"
        assert restored.tier_id == "tier-1"

    def test_deserialize_returns_none_for_invalid_json(self):
        """잘못된 JSON은 None을 반환한다."""
        mock_redis = MagicMock()
        repo = RedisEventJournalRepository(redis_client=mock_redis)

        result = repo._deserialize("not-json{{{")
        assert result is None

    def test_deserialize_returns_none_for_missing_keys(self):
        """필수 키가 없는 JSON은 None을 반환한다."""
        mock_redis = MagicMock()
        repo = RedisEventJournalRepository(redis_client=mock_redis)

        result = repo._deserialize('{"sequence": 1}')
        assert result is None

    def test_deserialize_handles_bytes_input(self):
        """bytes 입력을 올바르게 처리한다."""
        mock_redis = MagicMock()
        repo = RedisEventJournalRepository(redis_client=mock_redis)

        ts = datetime(2026, 3, 1, 12, 0, 0, tzinfo=UTC)
        entry = _make_entry(timestamp=ts)
        serialized = repo._serialize(entry, seq=1)
        result = repo._deserialize(serialized.encode("utf-8"))

        assert result is not None
        assert result.sequence == 1


class TestRedisEventJournalCountBehavior:
    """count() 동작 검증."""

    def test_count_returns_matching_entry_count(self):
        """필터에 맞는 엔트리 수를 반환한다."""
        repo, _ = _setup_repo_with_entries(
            [
                {"event_type": "type_a"},
                {"event_type": "type_b"},
                {"event_type": "type_a"},
            ]
        )

        result = repo.count(JournalQueryFilter(event_types=["type_a"]))
        assert result == 2

    def test_count_with_no_filter_uses_zcard(self):
        """필터 없이 count()하면 ZCARD로 빠르게 카운트한다."""
        mock_redis = MagicMock()
        mock_redis.scan_iter.return_value = iter(
            [b"baldur:journal:2026-03", b"baldur:journal:2026-02"]
        )
        mock_redis.zcard.side_effect = [10, 5]
        repo = RedisEventJournalRepository(redis_client=mock_redis)

        result = repo.count(JournalQueryFilter())

        assert result == 15
        assert mock_redis.zcard.call_count == 2
        mock_redis.zrangebyscore.assert_not_called()

    def test_count_with_filter_falls_back_to_deserialization(self):
        """필터가 있으면 역직렬화 경로를 사용한다."""
        repo, mock_redis = _setup_repo_with_entries(
            [
                {"event_type": "type_a"},
                {"event_type": "type_b"},
            ]
        )

        result = repo.count(JournalQueryFilter(event_types=["type_a"]))
        assert result == 1
        mock_redis.zcard.assert_not_called()

    def test_count_with_time_range_resolves_partition_keys(self):
        """시간 범위 필터 시 월별 파티션 키를 resolve한다."""
        mock_redis = MagicMock()
        mock_redis.zrangebyscore.return_value = []
        repo = RedisEventJournalRepository(redis_client=mock_redis)

        start = datetime(2026, 1, 1, tzinfo=UTC)
        end = datetime(2026, 3, 1, tzinfo=UTC)
        repo.count(JournalQueryFilter(start_time=start, end_time=end))

        assert mock_redis.zrangebyscore.call_count == 2


class TestRedisEventJournalPartitionKeyBehavior:
    """월별 파티셔닝 키 검증."""

    def test_get_key_formats_as_yyyy_mm(self):
        """타임스탬프를 YYYY-MM 형식의 파티션 키로 변환한다."""
        mock_redis = MagicMock()
        repo = RedisEventJournalRepository(redis_client=mock_redis)

        ts = datetime(2026, 3, 15, 12, 0, 0, tzinfo=UTC)
        key = repo._get_key(ts)
        assert key == "baldur:journal:2026-03"

    def test_resolve_partition_keys_spans_multiple_months(self):
        """시간 범위가 여러 월에 걸치면 모든 월의 키를 반환한다."""
        mock_redis = MagicMock()
        repo = RedisEventJournalRepository(redis_client=mock_redis)

        start = datetime(2025, 11, 15, tzinfo=UTC)
        end = datetime(2026, 2, 10, tzinfo=UTC)
        keys = repo._resolve_partition_keys(start, end)

        assert keys == [
            "baldur:journal:2025-11",
            "baldur:journal:2025-12",
            "baldur:journal:2026-01",
            "baldur:journal:2026-02",
        ]

    def test_resolve_partition_keys_handles_year_boundary(self):
        """연도 경계를 올바르게 처리한다."""
        mock_redis = MagicMock()
        repo = RedisEventJournalRepository(redis_client=mock_redis)

        start = datetime(2025, 12, 1, tzinfo=UTC)
        end = datetime(2026, 1, 15, tzinfo=UTC)
        keys = repo._resolve_partition_keys(start, end)

        assert keys == [
            "baldur:journal:2025-12",
            "baldur:journal:2026-01",
        ]

    def test_get_all_active_keys_uses_scan_iter(self):
        """scan_iter로 키를 탐색하고 bytes를 str로 디코딩한다."""
        mock_redis = MagicMock()
        mock_redis.scan_iter.return_value = iter(
            [
                b"baldur:journal:2026-01",
                b"baldur:journal:2026-02",
            ]
        )
        repo = RedisEventJournalRepository(redis_client=mock_redis)

        keys = repo._get_all_active_keys()
        assert all(isinstance(k, str) for k in keys)
        assert len(keys) == 2
        mock_redis.scan_iter.assert_called_once_with(
            match="baldur:journal:????-??", count=200
        )
