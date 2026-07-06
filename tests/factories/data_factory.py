"""
Test Data Factory.

테스트용 데이터 객체를 생성하는 Factory 클래스입니다.
각 메서드는 기본값을 제공하면서 커스터마이징 가능한 테스트 데이터를 생성합니다.

하드코딩된 값들을 중앙에서 관리하여:
- 도메인/서비스명 변경 시 한 곳만 수정
- 테스트 데이터 일관성 보장
- 새 테스트 작성 시간 단축
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import Mock

from baldur.interfaces.repositories import (
    CircuitBreakerStateData,
    FailedOperationData,
)

# 상수는 constants.py에서 관리
from tests.factories.constants import (
    DefaultValues,
    Domains,
    FailureTypes,
    Status,
)


@dataclass
class MockFailedOperationData:
    """Mock Failed Operation (DLQ) data for integration tests."""

    id: int = 1
    domain: str = Domains.PAYMENT
    failure_type: str = FailureTypes.PG_TIMEOUT
    status: str = Status.PENDING
    error_message: str = "Test error"
    retry_count: int = 0
    max_retries: int = 3
    created_at: datetime | None = None
    updated_at: datetime | None = None
    context: dict[str, Any] | None = None

    def __post_init__(self):
        if self.context is None:
            self.context = {}
        if self.created_at is None:
            self.created_at = datetime.now(UTC)
        if self.updated_at is None:
            self.updated_at = self.created_at


@dataclass
class MockCanaryRolloutData:
    """Mock Canary Rollout data for integration tests."""

    id: str = "rollout-test-001"
    config_type: str = "circuit_breaker"
    state: str = "created"
    current_stage_index: int = 0
    new_values: dict[str, Any] | None = None
    created_by: str = "test@example.com"
    reason: str = "Test rollout"
    created_at: datetime | None = None
    updated_at: datetime | None = None

    def __post_init__(self):
        if self.new_values is None:
            self.new_values = {}
        if self.created_at is None:
            self.created_at = datetime.now(UTC)
        if self.updated_at is None:
            self.updated_at = self.created_at


@dataclass
class MockCircuitBreakerStateData:
    """
    테스트용 Mock Circuit Breaker State Data.

    실제 CircuitBreakerStateData와 동일한 인터페이스를 제공하되,
    간소화된 테스트용 구현입니다.
    """

    service_name: str
    state: str = DefaultValues.CB_STATE_CLOSED
    failure_count: int = 0
    success_count: int = 0
    last_failure_time: datetime | None = None
    last_success_time: datetime | None = None
    opened_at: datetime | None = None
    opened_by_id: int | None = None
    opened_reason: str = ""
    half_open_request_count: int = 0
    last_failure_at: datetime | None = None
    last_success_at: datetime | None = None
    manually_controlled: bool = False
    controlled_by_id: int | None = None
    control_reason: str = ""
    manual_override_expires_at: datetime | None = None


class TestDataFactory:
    """
    테스트 데이터 생성 Factory.

    모든 테스트 데이터 생성을 중앙화하여 일관성 유지.

    Usage:
        # Circuit Breaker 상태 데이터
        state = TestDataFactory.circuit_breaker_state(
            service_name="payment-api",
            state="open"
        )

        # Failed Operation 데이터
        entry = TestDataFactory.failed_operation(
            domain="order",
            failure_type="network"
        )

        # Mock 객체로 생성
        mock_entry = TestDataFactory.mock_failed_operation(id=1)
    """

    # =========================================================================
    # Circuit Breaker 관련
    # =========================================================================

    @staticmethod
    def circuit_breaker_state(
        service_name: str = DefaultValues.SERVICE_TEST,
        state: str = DefaultValues.CB_STATE_CLOSED,
        failure_count: int = 0,
        success_count: int = 0,
        opened_at: datetime | None = None,
        opened_by_id: int | None = None,
        opened_reason: str = "",
        **kwargs,
    ) -> MockCircuitBreakerStateData:
        """
        Circuit Breaker 상태 데이터 생성.

        Args:
            service_name: 서비스 이름
            state: CB 상태 (closed, open, half_open)
            failure_count: 실패 횟수
            success_count: 성공 횟수
            opened_at: CB가 open된 시간
            opened_by_id: open한 사용자 ID
            opened_reason: open 사유
            **kwargs: 추가 필드

        Returns:
            MockCircuitBreakerStateData 인스턴스
        """
        return MockCircuitBreakerStateData(
            service_name=service_name,
            state=state,
            failure_count=failure_count,
            success_count=success_count,
            opened_at=opened_at,
            opened_by_id=opened_by_id,
            opened_reason=opened_reason,
            **kwargs,
        )

    @staticmethod
    def circuit_breaker_state_data(
        service_name: str = DefaultValues.SERVICE_TEST,
        state: str = DefaultValues.CB_STATE_CLOSED,
        failure_count: int = 0,
        success_count: int = 0,
        **kwargs,
    ) -> CircuitBreakerStateData:
        """
        실제 CircuitBreakerStateData 객체 생성.

        실제 타입이 필요한 경우 사용.
        """
        return CircuitBreakerStateData(
            service_name=service_name,
            state=state,
            failure_count=failure_count,
            success_count=success_count,
            **kwargs,
        )

    # =========================================================================
    # Failed Operation (DLQ) 관련
    # =========================================================================

    @staticmethod
    def failed_operation(
        id: int = 1,
        domain: str = DefaultValues.DOMAIN_PAYMENT,
        failure_type: str = DefaultValues.FAILURE_PG_TIMEOUT,
        status: str = DefaultValues.STATUS_PENDING,
        retry_count: int = 0,
        max_retries: int = DefaultValues.DEFAULT_MAX_RETRIES,
        created_at: datetime | None = None,
        error_message: str = "Connection timed out",
        error_code: str = "TIMEOUT",
        snapshot_data: dict[str, Any] | None = None,
        entity_type: str = "order",
        entity_id: str = "order-123",
        **kwargs,
    ) -> FailedOperationData:
        """
        Failed Operation 데이터 생성.

        interfaces.FailedOperationData 스키마 사용.

        Args:
            id: 엔트리 ID
            domain: 비즈니스 도메인
            failure_type: 실패 유형
            status: 상태
            retry_count: 재시도 횟수
            max_retries: 최대 재시도 횟수
            created_at: 생성 시간
            error_message: 에러 메시지
            error_code: 에러 코드
            snapshot_data: 스냅샷 데이터
            entity_type: 엔티티 타입
            entity_id: 엔티티 ID
            **kwargs: 추가 필드

        Returns:
            FailedOperationData 인스턴스
        """
        if created_at is None:
            created_at = datetime.now(UTC)

        if snapshot_data is None:
            snapshot_data = {"order_id": 123, "amount": 10000}

        return FailedOperationData(
            # FailedOperationData.id is an opaque string token (538 D1);
            # numeric test ids are stringified at this construction boundary.
            id=str(id),
            domain=domain,
            failure_type=failure_type,
            status=status,
            created_at=created_at,
            error_message=error_message,
            error_code=error_code,
            snapshot_data=snapshot_data,
            entity_type=entity_type,
            entity_id=entity_id,
            retry_count=retry_count,
            max_retries=max_retries,
            **kwargs,
        )

    @staticmethod
    def mock_failed_operation(
        id: int = 1,
        domain: str = DefaultValues.DOMAIN_PAYMENT,
        failure_type: str = DefaultValues.FAILURE_PG_TIMEOUT,
        status: str = DefaultValues.STATUS_PENDING,
        retry_count: int = 0,
        max_retries: int = DefaultValues.DEFAULT_MAX_RETRIES,
        **kwargs,
    ) -> Mock:
        """
        Mock으로 FailedOperationData 생성.

        spec을 사용하여 인터페이스 준수.
        DLQ 테스트에서 주로 사용.

        Args:
            id: 엔트리 ID
            domain: 비즈니스 도메인
            failure_type: 실패 유형
            status: 상태
            retry_count: 재시도 횟수
            max_retries: 최대 재시도 횟수

        Returns:
            Mock 객체 (FailedOperationData spec)
        """
        entry = Mock(spec=FailedOperationData)
        entry.id = id
        entry.domain = domain
        entry.failure_type = failure_type
        entry.status = status
        entry.retry_count = retry_count
        entry.max_retries = max_retries
        entry.created_at = datetime.now(UTC)
        entry.updated_at = datetime.now(UTC)
        entry.resolved_at = None
        entry.error_code = "TIMEOUT"
        entry.error_message = "Connection timed out"
        entry.snapshot_data = {"order_id": "order-123"}
        entry.request_data = {"method": "POST"}
        entry.response_data = {"status_code": 500}
        entry.metadata = {}
        # DLQ 추가 필드
        entry.entity_type = "order"
        entry.entity_id = "order-123"
        entry.resolution_note = ""

        # 추가 필드 설정
        for key, value in kwargs.items():
            setattr(entry, key, value)

        return entry

    # =========================================================================
    # 시간 관련 헬퍼
    # =========================================================================

    @staticmethod
    def now() -> datetime:
        """현재 UTC 시간 반환."""
        return datetime.now(UTC)

    @staticmethod
    def past(seconds: int = 60) -> datetime:
        """과거 시간 반환."""
        return datetime.now(UTC) - timedelta(seconds=seconds)

    @staticmethod
    def future(seconds: int = 60) -> datetime:
        """미래 시간 반환."""
        return datetime.now(UTC) + timedelta(seconds=seconds)

    # =========================================================================
    # Audit 관련
    # =========================================================================

    @staticmethod
    def audit_log_entry(
        event: str = "test_event",
        timestamp: datetime | None = None,
        data: dict[str, Any] | None = None,
        **kwargs,
    ) -> dict[str, Any]:
        """
        감사 로그 엔트리 생성.

        Args:
            event: 이벤트 이름
            timestamp: 타임스탬프
            data: 추가 데이터
            **kwargs: 추가 필드

        Returns:
            감사 로그 딕셔너리
        """
        if timestamp is None:
            timestamp = datetime.now(UTC)

        if data is None:
            data = {"key": "value"}

        entry = {
            "event": event,
            "timestamp": timestamp.isoformat(),
            "data": data,
        }
        entry.update(kwargs)
        return entry
