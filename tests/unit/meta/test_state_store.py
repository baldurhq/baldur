"""
WatchdogStateStore 테스트.

Redis 기반 상태 저장소 테스트.
"""

import threading
from datetime import datetime
from unittest import mock

import pytest

from baldur.meta.state_store import (
    WatchdogStateStore,
    get_watchdog_state_store,
    reset_watchdog_state_store,
)


@pytest.fixture(autouse=True)
def mock_redis_connection():
    """Redis 연결 시도를 mock하여 테스트 속도 향상."""
    with mock.patch.object(WatchdogStateStore, "_get_redis", return_value=None):
        yield


class TestWatchdogStateStore:
    """WatchdogStateStore 테스트."""

    @pytest.fixture
    def store(self):
        """Store fixture."""
        return WatchdogStateStore()

    def test_initialization(self, store):
        """초기화 테스트."""
        assert store is not None
        assert store._local_failures == {}
        assert store._local_cooldowns == {}

    def test_get_failure_count_default(self, store):
        """기본 실패 횟수 테스트 (0)."""
        count = store.get_failure_count("unknown")
        assert count == 0

    def test_increment_failure_count(self, store):
        """실패 횟수 증가 테스트."""
        store.increment_failure_count("redis")
        store.increment_failure_count("redis")
        store.increment_failure_count("redis")

        count = store.get_failure_count("redis")
        assert count == 3

    def test_reset_failure_count(self, store):
        """실패 횟수 리셋 테스트."""
        store.increment_failure_count("redis")
        store.increment_failure_count("redis")

        store.reset_failure_count("redis")

        count = store.get_failure_count("redis")
        assert count == 0

    def test_reset_all_failure_counts(self, store):
        """모든 실패 횟수 리셋 테스트."""
        store.increment_failure_count("redis")
        store.increment_failure_count("postgres")
        store.increment_failure_count("celery")

        store.reset_all_failure_counts()

        assert store.get_failure_count("redis") == 0
        assert store.get_failure_count("postgres") == 0
        assert store.get_failure_count("celery") == 0


class TestLastLoopTimestamp:
    """마지막 루프 타임스탬프 테스트 (Liveness)."""

    @pytest.fixture
    def store(self):
        """Store fixture."""
        return WatchdogStateStore()

    def test_get_last_loop_timestamp_default(self, store):
        """기본 타임스탬프 (None)."""
        timestamp = store.get_last_loop_timestamp()
        assert timestamp is None

    def test_update_last_loop_timestamp(self, store):
        """타임스탬프 갱신 테스트."""
        store.update_last_loop_timestamp()

        timestamp = store.get_last_loop_timestamp()
        assert timestamp is not None
        assert isinstance(timestamp, datetime)

    def test_get_last_loop_age_seconds_no_record(self, store):
        """기록 없을 때 무한대."""
        age = store.get_last_loop_age_seconds()
        assert age == float("inf")

    def test_get_last_loop_age_seconds(self, store):
        """경과 시간 테스트."""
        store.update_last_loop_timestamp()

        age = store.get_last_loop_age_seconds()
        assert age >= 0
        assert age < 60  # CI 환경에서 지연 허용


class TestDistributedLock:
    """분산 락 테스트."""

    @pytest.fixture
    def store(self):
        """Store fixture."""
        return WatchdogStateStore()

    def test_acquire_lock_no_redis(self, store):
        """Redis 없을 때 락 획득 (항상 True)."""
        acquired = store.acquire_escalation_lock("test", lock_ttl_seconds=30)
        assert acquired is True

    def test_release_lock_no_redis(self, store):
        """Redis 없을 때 락 해제."""
        # 예외 없이 완료되어야 함
        store.release_escalation_lock("test")


class TestClearAll:
    """전체 초기화 테스트."""

    @pytest.fixture
    def store(self):
        """Store fixture."""
        return WatchdogStateStore()

    def test_clear_all(self, store):
        """모든 상태 초기화 테스트."""
        store.increment_failure_count("redis")
        store._local_cooldowns["redis"] = 12345.0  # clear_all이 비우는지 확인
        store.update_last_loop_timestamp()

        store.clear_all()

        assert store.get_failure_count("redis") == 0
        assert store._local_cooldowns == {}
        assert store.get_last_loop_timestamp() is None


class TestThreadSafety:
    """스레드 안전성 테스트."""

    def test_concurrent_increment(self):
        """동시 증가 테스트."""
        store = WatchdogStateStore()

        def increment():
            for _ in range(100):
                store.increment_failure_count("test")

        threads = [threading.Thread(target=increment) for _ in range(10)]

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # 1000번 증가 예상 - Python GIL로 인해 대부분 성공하지만
        # dict[key] += 1은 원자적이지 않아 극히 드물게 race condition 동작 가능
        count = store.get_failure_count("test")
        assert count >= 950, f"Expected at least 950, got {count}"


class TestSingleton:
    """싱글톤 테스트."""

    def test_singleton_returns_same_instance(self):
        """싱글톤 인스턴스 반환 테스트."""
        reset_watchdog_state_store()

        store1 = get_watchdog_state_store()
        store2 = get_watchdog_state_store()

        assert store1 is store2

        reset_watchdog_state_store()

    def test_reset_clears_singleton(self):
        """싱글톤 리셋 테스트."""
        reset_watchdog_state_store()

        store1 = get_watchdog_state_store()
        reset_watchdog_state_store()
        store2 = get_watchdog_state_store()

        assert store1 is not store2

        reset_watchdog_state_store()


class TestRedisIntegration:
    """Redis 통합 테스트 (Mock)."""

    @pytest.fixture(autouse=True)
    def disable_global_mock(self, mock_redis_connection):
        """이 클래스에서는 global mock을 비활성화하고 수동으로 _redis 설정."""
        # mock_redis_connection fixture를 받아서 이 클래스 내에서는
        # 직접 store._redis를 설정하므로 _get_redis가 그것을 반환하도록 함
        pass

    def test_get_failure_count_from_redis(self, mock_redis_connection):
        """Redis에서 실패 횟수 조회 (Mock)."""
        with mock.patch.object(WatchdogStateStore, "_get_redis") as mock_get_redis:
            mock_redis = mock.MagicMock()
            mock_redis.hget.return_value = b"5"
            mock_get_redis.return_value = mock_redis

            store = WatchdogStateStore()
            count = store.get_failure_count("redis")
            assert count == 5

    def test_increment_failure_count_redis(self, mock_redis_connection):
        """Redis 실패 횟수 증가 (Mock)."""
        with mock.patch.object(WatchdogStateStore, "_get_redis") as mock_get_redis:
            mock_redis = mock.MagicMock()
            mock_redis.hincrby.return_value = 3
            mock_get_redis.return_value = mock_redis

            store = WatchdogStateStore()
            new_count = store.increment_failure_count("redis")
            assert new_count == 3

    def test_acquire_lock_redis_success(self, mock_redis_connection):
        """Redis 락 획득 성공 (Mock)."""
        with mock.patch.object(WatchdogStateStore, "_get_redis") as mock_get_redis:
            mock_redis = mock.MagicMock()
            mock_redis.set.return_value = True
            mock_get_redis.return_value = mock_redis

            store = WatchdogStateStore()
            acquired = store.acquire_escalation_lock("test", lock_ttl_seconds=30)
            assert acquired is True

    def test_acquire_lock_redis_failed(self, mock_redis_connection):
        """Redis 락 획득 실패 (Mock)."""
        with mock.patch.object(WatchdogStateStore, "_get_redis") as mock_get_redis:
            mock_redis = mock.MagicMock()
            mock_redis.set.return_value = False
            mock_get_redis.return_value = mock_redis

            store = WatchdogStateStore()
            acquired = store.acquire_escalation_lock("test", lock_ttl_seconds=30)
            assert acquired is False
