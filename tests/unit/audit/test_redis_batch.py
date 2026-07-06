"""
RedisAuditBuffer 배치 처리 단위 테스트.

테스트 항목:
- log_batch() 단일 pipeline 호출 확인
- 배치 처리 성능 검증
- Redis 오류 시 폴백 버퍼 동작
- 폴백 버퍼 재시도 로직
- 버퍼 통계 조회
"""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import Mock, patch

import pytest


class FakeRedisPipeline:
    """테스트용 Redis Pipeline 모킹."""

    def __init__(self):
        self.commands: list[tuple[str, tuple]] = []
        self._executed = False

    def lpush(self, key: str, *values) -> FakeRedisPipeline:
        self.commands.append(("lpush", (key, values)))
        return self

    def expire(self, key: str, ttl: int) -> FakeRedisPipeline:
        self.commands.append(("expire", (key, ttl)))
        return self

    def sadd(self, key: str, *values) -> FakeRedisPipeline:
        self.commands.append(("sadd", (key, values)))
        return self

    def execute(self) -> list:
        self._executed = True
        return [len(self.commands)]


class FakeRedis:
    """테스트용 Redis 클라이언트 모킹."""

    def __init__(self):
        self._data: dict[str, list] = {}
        self._sets: dict[str, set] = {}
        self._pipelines_created = 0
        self._pipeline_instance: FakeRedisPipeline | None = None

    def sadd(self, key: str, *values) -> int:
        if key not in self._sets:
            self._sets[key] = set()
        added = 0
        for v in values:
            if v not in self._sets[key]:
                self._sets[key].add(v)
                added += 1
        return added

    def smembers(self, key: str) -> set:
        return self._sets.get(key, set())

    def pipeline(self, transaction: bool = False) -> FakeRedisPipeline:
        self._pipelines_created += 1
        self._pipeline_instance = FakeRedisPipeline()
        return self._pipeline_instance

    def lpush(self, key: str, *values) -> int:
        if key not in self._data:
            self._data[key] = []
        for v in values:
            self._data[key].insert(0, v)
        return len(self._data[key])

    def llen(self, key: str) -> int:
        return len(self._data.get(key, []))

    def scan_iter(self, match: str = "*") -> list[str]:
        prefix = match.rstrip("*")
        return [k for k in self._data.keys() if k.startswith(prefix)]

    def rpop(self, key: str) -> str | None:
        if key in self._data and self._data[key]:
            return self._data[key].pop()
        return None

    def rpush(self, key: str, value: str) -> int:
        if key not in self._data:
            self._data[key] = []
        self._data[key].append(value)
        return len(self._data[key])

    def ping(self) -> bool:
        return True


class TestRedisAuditBufferBatch:
    """RedisAuditBuffer 배치 처리 테스트."""

    @pytest.fixture
    def fake_redis(self) -> FakeRedis:
        """테스트용 Fake Redis 인스턴스."""
        return FakeRedis()

    @pytest.fixture
    def buffer(self, fake_redis: FakeRedis):
        """테스트용 RedisAuditBuffer 인스턴스."""
        from baldur.adapters.audit.redis_buffer import RedisAuditBuffer

        return RedisAuditBuffer(
            redis_client=fake_redis,
            fallback_adapter=None,
            enable_graceful_shutdown=False,  # 테스트 종료 시 atexit 훅 방지
        )

    def test_log_batch_single_pipeline_call(
        self, buffer, fake_redis: FakeRedis
    ) -> None:
        """배치가 단일 pipeline으로 처리되는지 확인."""
        entries = [
            {
                "action": "test",
                "source": "app",
                "target_type": "order",
                "target_id": "1",
            },
            {
                "action": "test",
                "source": "app",
                "target_type": "order",
                "target_id": "2",
            },
            {
                "action": "test",
                "source": "app",
                "target_type": "order",
                "target_id": "3",
            },
        ]

        # pipeline 호출 전 카운트
        initial_count = fake_redis._pipelines_created

        buffer.log_batch(entries)

        # 3개 이벤트 → 1회 pipeline 호출
        assert fake_redis._pipelines_created == initial_count + 1

    def test_log_batch_empty_entries(self, buffer, fake_redis: FakeRedis) -> None:
        """빈 배치는 pipeline 호출하지 않음."""
        initial_count = fake_redis._pipelines_created

        result = buffer.log_batch([])

        # 빈 배치 → pipeline 호출 없음
        assert fake_redis._pipelines_created == initial_count
        assert result is True

    def test_log_batch_updates_statistics(self, buffer) -> None:
        """배치 처리 후 통계 업데이트."""
        entries = [
            {"action": "test1", "source": "app"},
            {"action": "test2", "source": "app"},
        ]

        buffer.log_batch(entries)

        stats = buffer.get_buffer_stats()
        assert stats["total_batch_writes"] == 1
        assert stats["total_writes"] == 2

    def test_log_batch_fallback_on_redis_error(self, buffer) -> None:
        """Redis 오류 시 폴백 버퍼 사용."""
        entries = [
            {
                "action": "test",
                "source": "app",
                "target_type": "order",
                "target_id": "1",
            },
        ]

        # Redis pipeline 오류 시뮬레이션
        with patch.object(
            buffer._redis, "pipeline", side_effect=Exception("Redis connection lost")
        ):
            result = buffer.log_batch(entries)

        # 실패 반환
        assert result is False
        # 폴백 버퍼에 저장됨
        assert buffer.get_fallback_buffer_size() == 1
        # 에러 카운트 증가
        assert buffer.get_buffer_stats()["total_batch_errors"] == 1

    def test_log_batch_multiple_domains(self, buffer, fake_redis: FakeRedis) -> None:
        """여러 도메인 배치도 단일 pipeline 호출."""
        entries_domain1 = [{"action": "test1", "source": "domain1"}]
        entries_domain2 = [{"action": "test2", "source": "domain2"}]

        initial_count = fake_redis._pipelines_created

        buffer.log_batch(entries_domain1, domain="domain1")
        buffer.log_batch(entries_domain2, domain="domain2")

        # 각 도메인별로 1회씩 = 총 2회
        assert fake_redis._pipelines_created == initial_count + 2

    def test_log_batch_performance(self, buffer) -> None:
        """배치 처리 성능 테스트 - 1000개 100ms 이내."""
        entries = [
            {"action": "test", "source": "app", "target_id": str(i)}
            for i in range(1000)
        ]

        start = time.time()
        buffer.log_batch(entries)
        elapsed = time.time() - start

        # 1000개 배치가 100ms 이내
        assert elapsed < 0.1, f"1000 entries took {elapsed}s (should be < 0.1s)"


class TestRedisAuditBufferFallback:
    """폴백 버퍼 관련 테스트."""

    @pytest.fixture
    def buffer_with_error_redis(self):
        """오류 발생 Redis를 가진 버퍼."""
        from baldur.adapters.audit.redis_buffer import RedisAuditBuffer

        error_redis = Mock()
        error_redis.pipeline.side_effect = Exception("Redis unavailable")

        return RedisAuditBuffer(
            redis_client=error_redis,
            fallback_adapter=None,
            enable_graceful_shutdown=False,
        )

    def test_fallback_buffer_stores_entries(self, buffer_with_error_redis) -> None:
        """Redis 실패 시 폴백 버퍼에 저장."""
        entries = [
            {"action": "test1"},
            {"action": "test2"},
        ]

        buffer_with_error_redis.log_batch(entries)

        assert buffer_with_error_redis.get_fallback_buffer_size() == 2

    def test_fallback_buffer_size_limit(self) -> None:
        """폴백 버퍼 크기 제한 테스트."""
        from baldur.adapters.audit.redis_buffer import RedisAuditBuffer

        error_redis = Mock()
        error_redis.pipeline.side_effect = Exception("Redis unavailable")

        buffer = RedisAuditBuffer(
            redis_client=error_redis,
            fallback_adapter=None,
        )
        # 강제로 max_fallback 설정 줄이기
        buffer._max_fallback = 5

        # 10개 추가 시도
        for i in range(10):
            buffer.log_batch([{"action": f"test{i}"}])

        # 최대 5개만 유지
        assert buffer.get_fallback_buffer_size() == 5

    def test_retry_fallback_buffer_success(self) -> None:
        """폴백 버퍼 재시도 성공 테스트."""
        from baldur.adapters.audit.redis_buffer import RedisAuditBuffer

        fake_redis = FakeRedis()
        buffer = RedisAuditBuffer(
            redis_client=fake_redis,
            fallback_adapter=None,
        )

        # 먼저 폴백 버퍼에 직접 추가
        buffer._fallback_buffer = [
            {
                "entry": {"action": "test1"},
                "domain": "default",
                "timestamp": "2026-01-01",
            },
            {
                "entry": {"action": "test2"},
                "domain": "default",
                "timestamp": "2026-01-01",
            },
        ]

        recovered = buffer.retry_fallback_buffer()

        assert recovered == 2
        assert buffer.get_fallback_buffer_size() == 0


class TestRedisAuditBufferStats:
    """버퍼 통계 테스트."""

    @pytest.fixture
    def buffer(self) -> Any:
        """테스트용 버퍼."""
        from baldur.adapters.audit.redis_buffer import RedisAuditBuffer

        return RedisAuditBuffer(
            redis_client=FakeRedis(),
            fallback_adapter=None,
            enable_graceful_shutdown=False,
        )

    def test_get_buffer_stats_includes_batch_info(self, buffer) -> None:
        """통계에 배치 정보 포함."""
        entries = [{"action": "test"}]
        buffer.log_batch(entries)

        stats = buffer.get_buffer_stats()

        assert "total_batch_writes" in stats
        assert "total_batch_errors" in stats
        assert "fallback_buffer_size" in stats
        assert stats["total_batch_writes"] == 1
        assert stats["total_batch_errors"] == 0
        assert stats["fallback_buffer_size"] == 0

    def test_consecutive_failures_reset_on_success(self, buffer) -> None:
        """성공 시 연속 실패 카운트 리셋."""
        # 직접 실패 카운트 설정
        buffer._consecutive_failures = 3

        # 성공적인 배치 처리
        buffer.log_batch([{"action": "test"}])

        # 실패 카운트 리셋됨
        assert buffer._consecutive_failures == 0
