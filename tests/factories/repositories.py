"""
In-Memory Repositories for Testing.

테스트용 인메모리 Repository 구현입니다.
실제 DB/Redis 연결 없이 테스트가 가능하도록 합니다.

MockRepository를 개별 테스트 파일에서 반복 정의하던 것을 통합합니다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from tests.factories.constants import DefaultValues
from tests.factories.data_factory import MockCircuitBreakerStateData


class InMemoryCircuitBreakerRepository:
    """
    Circuit Breaker State용 인메모리 Repository.

    CircuitBreakerStateRepository 인터페이스를 구현합니다.
    테스트에서 실제 DB/Redis 없이 CB 상태 관리 테스트 가능.

    Usage:
        repo = InMemoryCircuitBreakerRepository()

        # 상태 조회/생성
        state = repo.get_or_create("payment-api")
        assert state.state == "closed"

        # Force open
        success, prev, new = repo.atomic_force_open(
            "payment-api",
            reason="Maintenance",
            controlled_by_id=1,
            ttl_minutes=30
        )

        # 상태 확인
        state = repo.get_or_create("payment-api")
        assert state.state == "open"
    """

    def __init__(self):
        self._states: dict[str, MockCircuitBreakerStateData] = {}
        self._atomic_success: bool = True  # atomic 연산 성공 여부 제어

    def set_atomic_success(self, success: bool) -> None:
        """atomic 연산 성공 여부 설정 (테스트용)."""
        self._atomic_success = success

    def get_or_create(self, service_name: str) -> MockCircuitBreakerStateData:
        """
        서비스의 CB 상태를 조회하거나 새로 생성.

        Args:
            service_name: 서비스 이름

        Returns:
            MockCircuitBreakerStateData 인스턴스
        """
        if service_name not in self._states:
            self._states[service_name] = MockCircuitBreakerStateData(
                service_name=service_name
            )
        return self._states[service_name]

    def get(self, service_name: str) -> MockCircuitBreakerStateData | None:
        """
        서비스의 CB 상태 조회 (없으면 None).

        Args:
            service_name: 서비스 이름

        Returns:
            MockCircuitBreakerStateData 또는 None
        """
        return self._states.get(service_name)

    def atomic_force_open(
        self,
        service_name: str,
        reason: str,
        controlled_by_id: int,
        ttl_minutes: int,
    ) -> tuple[bool, str | None, str | None]:
        """
        Circuit Breaker를 atomic하게 open.

        Args:
            service_name: 서비스 이름
            reason: open 사유
            controlled_by_id: 제어자 ID
            ttl_minutes: TTL (분)

        Returns:
            (success, previous_state, new_state) 튜플
        """
        if not self._atomic_success:
            return (False, None, None)

        state = self.get_or_create(service_name)
        previous_state = state.state
        state.state = DefaultValues.CB_STATE_OPEN
        state.opened_at = datetime.now(UTC)
        state.opened_by_id = controlled_by_id
        state.opened_reason = reason
        state.manually_controlled = True
        state.controlled_by_id = controlled_by_id
        state.control_reason = reason

        return (True, previous_state, DefaultValues.CB_STATE_OPEN)

    def atomic_force_close(
        self,
        service_name: str,
        reason: str,
        controlled_by_id: int,
    ) -> tuple[bool, str | None, str | None]:
        """
        Circuit Breaker를 atomic하게 close.

        Args:
            service_name: 서비스 이름
            reason: close 사유
            controlled_by_id: 제어자 ID

        Returns:
            (success, previous_state, new_state) 튜플
        """
        if not self._atomic_success:
            return (False, None, None)

        state = self.get_or_create(service_name)
        previous_state = state.state
        state.state = DefaultValues.CB_STATE_CLOSED
        state.manually_controlled = False
        state.controlled_by_id = controlled_by_id
        state.control_reason = reason

        return (True, previous_state, DefaultValues.CB_STATE_CLOSED)

    def atomic_reset(self, service_name: str) -> bool:
        """
        Circuit Breaker를 atomic하게 리셋.

        Args:
            service_name: 서비스 이름

        Returns:
            성공 여부
        """
        state = self.get_or_create(service_name)
        state.state = DefaultValues.CB_STATE_CLOSED
        state.failure_count = 0
        state.success_count = 0
        state.opened_at = None
        state.opened_by_id = None
        state.opened_reason = ""
        state.manually_controlled = False
        state.controlled_by_id = None
        state.control_reason = ""
        return True

    def update_failure_count(
        self,
        service_name: str,
        increment: int = 1,
    ) -> int:
        """
        실패 횟수 업데이트.

        Args:
            service_name: 서비스 이름
            increment: 증가량

        Returns:
            새 실패 횟수
        """
        state = self.get_or_create(service_name)
        state.failure_count += increment
        state.last_failure_at = datetime.now(UTC)
        return state.failure_count

    def update_success_count(
        self,
        service_name: str,
        increment: int = 1,
    ) -> int:
        """
        성공 횟수 업데이트.

        Args:
            service_name: 서비스 이름
            increment: 증가량

        Returns:
            새 성공 횟수
        """
        state = self.get_or_create(service_name)
        state.success_count += increment
        state.last_success_at = datetime.now(UTC)
        return state.success_count

    def list_all(self) -> list[MockCircuitBreakerStateData]:
        """모든 CB 상태 조회."""
        return list(self._states.values())

    def clear(self) -> None:
        """모든 상태 초기화."""
        self._states.clear()

    def reset_half_open_count(self, service_name: str) -> None:
        """476 G8: clear HALF_OPEN counter on a service."""
        state = self._states.get(service_name)
        if state is None:
            return
        if hasattr(state, "half_open_request_count"):
            state.half_open_request_count = 0
        if hasattr(state, "half_open_window_started_at"):
            state.half_open_window_started_at = None

    def try_acquire_half_open_slot(
        self, service_name: str, limit: int, stuck_timeout_seconds: int
    ) -> tuple[bool, str, str]:
        """476 D2: minimal RLock-free state-machine for tests."""
        state = self.get_or_create(service_name)
        current_state = state.state
        count = getattr(state, "half_open_request_count", 0) or 0

        if current_state == DefaultValues.CB_STATE_OPEN:
            state.state = "half_open"
            if hasattr(state, "success_count"):
                state.success_count = 0
            if hasattr(state, "half_open_request_count"):
                state.half_open_request_count = 1
            return (True, DefaultValues.CB_STATE_OPEN, "half_open")

        if current_state == "half_open" and count < limit:
            if hasattr(state, "half_open_request_count"):
                state.half_open_request_count = count + 1
            return (True, "half_open", "half_open")

        if current_state == "half_open":
            return (False, "half_open", "half_open")

        return (False, current_state, current_state)


class InMemoryRateLimitTracker:
    """
    Rate Limit Tracker용 인메모리 구현.

    test_protection.py의 MockRateLimitTracker를 대체합니다.

    Usage:
        tracker = InMemoryRateLimitTracker()

        tracker.record_rate_limit("payment-api")
        tracker.record_request("payment-api")

        count = tracker.get_rate_limit_count("payment-api", window_seconds=60)
    """

    def __init__(self):
        self._rate_limits: dict[str, int] = {}
        self._requests: dict[str, int] = {}
        self._backoff: dict[str, int] = {}

    def record_rate_limit(self, service_name: str) -> None:
        """Rate limit 응답 기록."""
        self._rate_limits.setdefault(service_name, 0)
        self._rate_limits[service_name] += 1

    def record_request(self, service_name: str) -> None:
        """요청 기록."""
        self._requests.setdefault(service_name, 0)
        self._requests[service_name] += 1

    def get_rate_limit_count(self, service_name: str, window_seconds: int) -> int:
        """지정 윈도우 내 rate limit 횟수 조회."""
        return self._rate_limits.get(service_name, 0)

    def get_request_count(self, service_name: str, window_seconds: int) -> int:
        """지정 윈도우 내 요청 횟수 조회."""
        return self._requests.get(service_name, 0)

    def get_backoff_level(self, service_name: str) -> int:
        """현재 backoff 레벨 조회."""
        return self._backoff.get(service_name, 0)

    def increment_backoff(self, service_name: str) -> int:
        """backoff 레벨 증가."""
        self._backoff.setdefault(service_name, 0)
        self._backoff[service_name] += 1
        return self._backoff[service_name]

    def reset_backoff(self, service_name: str) -> None:
        """backoff 레벨 리셋."""
        self._backoff[service_name] = 0

    def clear(self) -> None:
        """모든 데이터 초기화."""
        self._rate_limits.clear()
        self._requests.clear()
        self._backoff.clear()


@dataclass
class MockDLQEntry:
    """DLQ 엔트리 Mock 데이터."""

    id: int
    domain: str
    failure_type: str
    status: str
    retry_count: int = 0
    max_retries: int = 3
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    resolved_at: datetime | None = None
    error_code: str = "TIMEOUT"
    error_message: str = "Connection timed out"
    snapshot_data: dict[str, Any] = field(
        default_factory=lambda: {"order_id": "order-123"}
    )
    request_data: dict[str, Any] = field(default_factory=lambda: {"method": "POST"})
    response_data: dict[str, Any] = field(default_factory=lambda: {"status_code": 500})
    metadata: dict[str, Any] = field(default_factory=dict)


class InMemoryDLQRepository:
    """
    DLQ (Dead Letter Queue)용 인메모리 Repository.

    FailedOperationRepository 인터페이스를 구현합니다.

    Usage:
        repo = InMemoryDLQRepository()

        # 엔트리 추가
        entry = repo.create(
            domain="payment",
            failure_type="PG_TIMEOUT",
            error_message="Connection timed out"
        )

        # 조회
        entry = repo.get_by_id(1)

        # 재시도 횟수 증가
        repo.increment_retry_count(1)
    """

    def __init__(self):
        self._entries: dict[int, MockDLQEntry] = {}
        self._next_id = 1

    def create(
        self,
        domain: str = DefaultValues.DOMAIN_PAYMENT,
        failure_type: str = DefaultValues.FAILURE_PG_TIMEOUT,
        status: str = DefaultValues.STATUS_PENDING,
        error_message: str = "Connection timed out",
        **kwargs,
    ) -> MockDLQEntry:
        """
        새 DLQ 엔트리 생성.

        Args:
            domain: 비즈니스 도메인
            failure_type: 실패 유형
            status: 상태
            error_message: 에러 메시지
            **kwargs: 추가 필드

        Returns:
            생성된 MockDLQEntry
        """
        entry = MockDLQEntry(
            id=self._next_id,
            domain=domain,
            failure_type=failure_type,
            status=status,
            error_message=error_message,
            **kwargs,
        )
        self._entries[self._next_id] = entry
        self._next_id += 1
        return entry

    def get_by_id(self, pk: int) -> MockDLQEntry | None:
        """ID로 엔트리 조회."""
        return self._entries.get(pk)

    def increment_retry_count(self, pk: int) -> bool:
        """재시도 횟수 증가."""
        entry = self._entries.get(pk)
        if entry is None:
            return False
        entry.retry_count += 1
        return True

    def update_status(self, pk: int, status: str) -> bool:
        """상태 업데이트."""
        entry = self._entries.get(pk)
        if entry is None:
            return False
        entry.status = status
        if status == "resolved":
            entry.resolved_at = datetime.now(UTC)
        return True

    def list_pending(
        self,
        domain: str | None = None,
        limit: int = 100,
    ) -> list[MockDLQEntry]:
        """
        Pending 상태 엔트리 목록 조회.

        Args:
            domain: 필터할 도메인 (None이면 전체)
            limit: 최대 개수

        Returns:
            MockDLQEntry 리스트
        """
        entries = [
            e
            for e in self._entries.values()
            if e.status == DefaultValues.STATUS_PENDING
            and (domain is None or e.domain == domain)
        ]
        return entries[:limit]

    def list_all(self) -> list[MockDLQEntry]:
        """모든 엔트리 조회."""
        return list(self._entries.values())

    def delete(self, pk: int) -> bool:
        """엔트리 삭제."""
        if pk in self._entries:
            del self._entries[pk]
            return True
        return False

    def clear(self) -> None:
        """모든 엔트리 초기화."""
        self._entries.clear()
        self._next_id = 1
