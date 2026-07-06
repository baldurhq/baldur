"""
Tests for Factory Pattern Implementation.

Phase 1 예제 테스트:
- TestDataFactory 기본 동작
- InMemoryCircuitBreakerRepository 동작
- InMemoryDLQRepository 동작
- MockRedisClient 동작
- 상수 클래스 동작
"""

from datetime import UTC, datetime

import pytest

from baldur.interfaces.repositories import CircuitBreakerStateEnum as CircuitState
from tests.factories import (
    DefaultValues,
    # Constants
    Domains,
    FailureTypes,
    # Repositories
    InMemoryCircuitBreakerRepository,
    InMemoryDLQRepository,
    InMemoryRateLimitTracker,
    MockDistributedLock,
    MockRedisClient,
    Services,
    Status,
    # Data Factory
    TestDataFactory,
    get_fixed_datetime,
    make_datetime_range,
    # Time helpers
    mock_sleep,
)

# =============================================================================
# Constants Tests
# =============================================================================


class TestConstants:
    """상수 클래스 테스트."""

    def test_domains_values(self):
        """도메인 상수 값 확인."""
        assert Domains.ORDER == "order"
        assert Domains.PAYMENT == "payment"
        assert Domains.NOTIFICATION == "notification"

    def test_services_values(self):
        """서비스 상수 값 확인."""
        assert Services.PAYMENT_API == "payment-api"
        assert Services.TEST == "test_service"

    def test_failure_types_values(self):
        """실패 유형 상수 값 확인."""
        assert FailureTypes.NETWORK == "network"
        assert FailureTypes.PG_TIMEOUT == "PG_TIMEOUT"

    def test_status_values(self):
        """상태 상수 값 확인."""
        assert Status.PENDING == "pending"
        assert Status.RESOLVED == "resolved"

    def test_circuit_state_values(self):
        """CB 상태 상수 값 확인."""
        assert CircuitState.CLOSED == "closed"
        assert CircuitState.OPEN == "open"
        assert CircuitState.HALF_OPEN == "half_open"

    def test_default_values_mirrors_individual_classes(self):
        """DefaultValues가 개별 클래스 값을 미러링하는지 확인."""
        assert DefaultValues.DOMAIN_ORDER == Domains.ORDER
        assert DefaultValues.SERVICE_PAYMENT_API == Services.PAYMENT_API
        assert DefaultValues.FAILURE_NETWORK == FailureTypes.NETWORK
        assert DefaultValues.STATUS_PENDING == Status.PENDING
        assert DefaultValues.CB_STATE_CLOSED == CircuitState.CLOSED


# =============================================================================
# TestDataFactory Tests
# =============================================================================


class TestTestDataFactory:
    """TestDataFactory 테스트."""

    def test_circuit_breaker_state_default(self):
        """기본 CB 상태 생성."""
        state = TestDataFactory.circuit_breaker_state()

        assert state.service_name == DefaultValues.SERVICE_TEST
        assert state.state == CircuitState.CLOSED
        assert state.failure_count == 0
        assert state.success_count == 0

    def test_circuit_breaker_state_custom(self):
        """커스텀 CB 상태 생성."""
        now = datetime.now(UTC)
        state = TestDataFactory.circuit_breaker_state(
            service_name="payment-api",
            state=CircuitState.OPEN,
            failure_count=5,
            opened_at=now,
            opened_reason="High failure rate",
        )

        assert state.service_name == "payment-api"
        assert state.state == CircuitState.OPEN
        assert state.failure_count == 5
        assert state.opened_at == now
        assert state.opened_reason == "High failure rate"

    def test_failed_operation_default(self):
        """기본 FailedOperation 생성."""
        entry = TestDataFactory.failed_operation()

        assert entry.id == "1"
        assert entry.domain == Domains.PAYMENT
        assert entry.failure_type == FailureTypes.PG_TIMEOUT
        assert entry.status == Status.PENDING
        assert entry.retry_count == 0
        assert entry.entity_type == "order"
        assert entry.entity_id == "order-123"

    def test_failed_operation_custom(self):
        """커스텀 FailedOperation 생성."""
        entry = TestDataFactory.failed_operation(
            id=10,
            domain=Domains.ORDER,
            failure_type=FailureTypes.NETWORK,
            status=Status.RESOLVED,
            retry_count=3,
        )

        assert entry.id == "10"
        assert entry.domain == Domains.ORDER
        assert entry.failure_type == FailureTypes.NETWORK
        assert entry.status == Status.RESOLVED
        assert entry.retry_count == 3

    def test_mock_failed_operation(self):
        """Mock FailedOperation 생성."""
        mock_entry = TestDataFactory.mock_failed_operation(id=5)

        assert mock_entry.id == 5
        assert mock_entry.domain == DefaultValues.DOMAIN_PAYMENT
        assert hasattr(mock_entry, "status")

    def test_time_helpers(self):
        """시간 헬퍼 메서드 테스트."""
        now = TestDataFactory.now()
        past = TestDataFactory.past(seconds=60)
        future = TestDataFactory.future(seconds=60)

        assert past < now < future


# =============================================================================
# InMemoryCircuitBreakerRepository Tests
# =============================================================================


class TestInMemoryCircuitBreakerRepository:
    """InMemoryCircuitBreakerRepository 테스트."""

    def test_get_or_create_new_service(self):
        """새 서비스 상태 생성."""
        repo = InMemoryCircuitBreakerRepository()

        state = repo.get_or_create("payment-api")

        assert state.service_name == "payment-api"
        assert state.state == CircuitState.CLOSED

    def test_get_or_create_existing_service(self):
        """기존 서비스 상태 조회."""
        repo = InMemoryCircuitBreakerRepository()

        state1 = repo.get_or_create("payment-api")
        state1.failure_count = 5

        state2 = repo.get_or_create("payment-api")

        assert state2.failure_count == 5
        assert state1 is state2

    def test_get_returns_none_for_unknown(self):
        """없는 서비스 조회 시 None 반환."""
        repo = InMemoryCircuitBreakerRepository()

        state = repo.get("unknown-service")

        assert state is None

    def test_atomic_force_open(self):
        """atomic_force_open 동작 확인."""
        repo = InMemoryCircuitBreakerRepository()
        repo.get_or_create("payment-api")

        success, prev, new = repo.atomic_force_open(
            service_name="payment-api",
            reason="Maintenance",
            controlled_by_id=1,
            ttl_minutes=30,
        )

        assert success is True
        assert prev == CircuitState.CLOSED
        assert new == CircuitState.OPEN

        state = repo.get("payment-api")
        assert state.state == CircuitState.OPEN
        assert state.manually_controlled is True

    def test_atomic_force_close(self):
        """atomic_force_close 동작 확인."""
        repo = InMemoryCircuitBreakerRepository()
        state = repo.get_or_create("payment-api")
        state.state = CircuitState.OPEN

        success, prev, new = repo.atomic_force_close(
            service_name="payment-api",
            reason="Recovered",
            controlled_by_id=1,
        )

        assert success is True
        assert prev == CircuitState.OPEN
        assert new == CircuitState.CLOSED

    def test_atomic_reset(self):
        """atomic_reset 동작 확인."""
        repo = InMemoryCircuitBreakerRepository()
        state = repo.get_or_create("payment-api")
        state.failure_count = 10
        state.state = CircuitState.OPEN

        success = repo.atomic_reset("payment-api")

        assert success is True
        state = repo.get("payment-api")
        assert state.failure_count == 0
        assert state.state == CircuitState.CLOSED

    def test_update_failure_count(self):
        """실패 횟수 업데이트."""
        repo = InMemoryCircuitBreakerRepository()

        new_count = repo.update_failure_count("payment-api", 1)
        assert new_count == 1

        new_count = repo.update_failure_count("payment-api", 2)
        assert new_count == 3

    def test_list_all(self):
        """모든 상태 조회."""
        repo = InMemoryCircuitBreakerRepository()
        repo.get_or_create("service-1")
        repo.get_or_create("service-2")

        states = repo.list_all()

        assert len(states) == 2


# =============================================================================
# InMemoryDLQRepository Tests
# =============================================================================


class TestInMemoryDLQRepository:
    """InMemoryDLQRepository 테스트."""

    def test_create_entry(self):
        """엔트리 생성."""
        repo = InMemoryDLQRepository()

        entry = repo.create(
            domain=Domains.PAYMENT,
            failure_type=FailureTypes.PG_TIMEOUT,
            error_message="Payment timeout",
        )

        assert entry.id == 1
        assert entry.domain == Domains.PAYMENT
        assert entry.failure_type == FailureTypes.PG_TIMEOUT

    def test_create_multiple_entries(self):
        """여러 엔트리 생성 시 ID 증가."""
        repo = InMemoryDLQRepository()

        entry1 = repo.create(domain=Domains.ORDER)
        entry2 = repo.create(domain=Domains.PAYMENT)

        assert entry1.id == 1
        assert entry2.id == 2

    def test_get_by_id(self):
        """ID로 조회."""
        repo = InMemoryDLQRepository()
        created = repo.create(domain=Domains.ORDER)

        found = repo.get_by_id(created.id)

        assert found is not None
        assert found.id == created.id

    def test_increment_retry_count(self):
        """재시도 횟수 증가."""
        repo = InMemoryDLQRepository()
        entry = repo.create()

        repo.increment_retry_count(entry.id)
        repo.increment_retry_count(entry.id)

        updated = repo.get_by_id(entry.id)
        assert updated.retry_count == 2

    def test_update_status(self):
        """상태 업데이트."""
        repo = InMemoryDLQRepository()
        entry = repo.create()

        repo.update_status(entry.id, Status.RESOLVED)

        updated = repo.get_by_id(entry.id)
        assert updated.status == Status.RESOLVED
        assert updated.resolved_at is not None

    def test_list_pending(self):
        """Pending 엔트리 조회."""
        repo = InMemoryDLQRepository()
        repo.create(domain=Domains.ORDER)
        repo.create(domain=Domains.PAYMENT)
        entry3 = repo.create(domain=Domains.ORDER)
        repo.update_status(entry3.id, Status.RESOLVED)

        pending = repo.list_pending()

        assert len(pending) == 2

    def test_list_pending_by_domain(self):
        """도메인별 Pending 조회."""
        repo = InMemoryDLQRepository()
        repo.create(domain=Domains.ORDER)
        repo.create(domain=Domains.PAYMENT)

        order_pending = repo.list_pending(domain=Domains.ORDER)

        assert len(order_pending) == 1
        assert order_pending[0].domain == Domains.ORDER


# =============================================================================
# MockRedisClient Tests
# =============================================================================


class TestMockRedisClient:
    """MockRedisClient 테스트."""

    def test_set_and_get(self):
        """SET/GET 동작."""
        redis = MockRedisClient()

        redis.set("key1", "value1")
        result = redis.get("key1")

        assert result == b"value1"

    def test_get_nonexistent_returns_none(self):
        """없는 키 GET 시 None 반환."""
        redis = MockRedisClient()

        result = redis.get("nonexistent")

        assert result is None

    def test_delete(self):
        """DELETE 동작."""
        redis = MockRedisClient()
        redis.set("key1", "value1")

        count = redis.delete("key1")

        assert count == 1
        assert redis.get("key1") is None

    def test_incr_decr(self):
        """INCR/DECR 동작."""
        redis = MockRedisClient()

        val = redis.incr("counter")
        assert val == 1

        val = redis.incr("counter")
        assert val == 2

        val = redis.decr("counter")
        assert val == 1

    def test_hash_operations(self):
        """Hash 명령 동작."""
        redis = MockRedisClient()

        redis.hset("myhash", {"field1": "value1", "field2": "value2"})

        val = redis.hget("myhash", "field1")
        assert val == b"value1"

        all_vals = redis.hgetall("myhash")
        assert len(all_vals) == 2

    def test_list_operations(self):
        """List 명령 동작."""
        redis = MockRedisClient()

        redis.lpush("mylist", "a", "b")
        redis.rpush("mylist", "c")

        length = redis.llen("mylist")
        assert length == 3

        items = redis.lrange("mylist", 0, -1)
        assert len(items) == 3

    def test_pipeline(self):
        """Pipeline 동작."""
        redis = MockRedisClient()

        pipe = redis.pipeline()
        pipe.set("key1", "value1")
        pipe.set("key2", "value2")
        pipe.get("key1")
        results = pipe.execute()

        assert len(results) == 3
        assert results[0] is True
        assert results[2] == b"value1"

    def test_failure_mode(self):
        """실패 모드 동작."""
        redis = MockRedisClient(should_fail=True)

        with pytest.raises(ConnectionError):
            redis.get("key")

    def test_ping(self):
        """PING 동작."""
        redis = MockRedisClient()

        assert redis.ping() is True

    def test_flushdb(self):
        """FLUSHDB 동작."""
        redis = MockRedisClient()
        redis.set("key1", "value1")
        redis.hset("hash1", {"f": "v"})

        redis.flushdb()

        assert redis.get("key1") is None


# =============================================================================
# InMemoryRateLimitTracker Tests
# =============================================================================


class TestInMemoryRateLimitTracker:
    """InMemoryRateLimitTracker 테스트."""

    def test_record_rate_limit(self):
        """Rate limit 기록."""
        tracker = InMemoryRateLimitTracker()

        tracker.record_rate_limit("service-a")
        tracker.record_rate_limit("service-a")

        count = tracker.get_rate_limit_count("service-a", 60)
        assert count == 2

    def test_record_request(self):
        """요청 기록."""
        tracker = InMemoryRateLimitTracker()

        tracker.record_request("service-a")

        count = tracker.get_request_count("service-a", 60)
        assert count == 1

    def test_backoff_level(self):
        """Backoff 레벨 관리."""
        tracker = InMemoryRateLimitTracker()

        level = tracker.increment_backoff("service-a")
        assert level == 1

        level = tracker.increment_backoff("service-a")
        assert level == 2

        tracker.reset_backoff("service-a")
        level = tracker.get_backoff_level("service-a")
        assert level == 0


# =============================================================================
# Time Helpers Tests
# =============================================================================


class TestTimeHelpers:
    """시간 헬퍼 테스트."""

    def test_mock_sleep(self):
        """mock_sleep 동작."""
        import time

        with mock_sleep() as sleep_mock:
            time.sleep(5)
            time.sleep(3)

        assert sleep_mock.call_count == 2
        assert sleep_mock.total_slept == 8

    def test_get_fixed_datetime(self):
        """고정 datetime 생성."""
        dt = get_fixed_datetime(2024, 1, 15, 12, 30, 0)

        assert dt.year == 2024
        assert dt.month == 1
        assert dt.day == 15
        assert dt.hour == 12
        assert dt.minute == 30
        assert dt.tzinfo == UTC

    def test_make_datetime_range(self):
        """datetime 범위 생성."""
        from datetime import timedelta

        start = get_fixed_datetime(2024, 1, 1, 0, 0, 0)
        dts = make_datetime_range(start, 5, timedelta(hours=1))

        assert len(dts) == 5
        assert dts[0] == start
        assert dts[1] == start + timedelta(hours=1)
        assert dts[4] == start + timedelta(hours=4)


# =============================================================================
# MockDistributedLock Tests
# =============================================================================


class TestMockDistributedLock:
    """MockDistributedLock 테스트."""

    def test_acquire_release(self):
        """락 획득/해제."""
        lock = MockDistributedLock("test-lock")

        result = lock.acquire()

        assert result is True

        lock.release()

    def test_context_manager(self):
        """Context manager 사용."""
        lock = MockDistributedLock("test-lock")

        with lock:
            assert lock._acquired is True

        assert lock._acquired is False
