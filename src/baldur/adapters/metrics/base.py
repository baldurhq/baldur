"""
Metric Source Adapter Base Interface.

Provides an abstract interface for collecting metrics from various sources
without direct dependency on user's database schema.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Protocol, runtime_checkable


@runtime_checkable
class MetricSourceAdapter(Protocol):
    """
    메트릭 소스 어댑터 인터페이스.

    사용자는 이 인터페이스를 구현하여 자신의 데이터 소스에서
    메트릭 값을 제공합니다. DB, 캐시, 외부 API 등 어떤 소스든 가능합니다.

    Design Principles:
    - Zero DB Dependency: 사용자 DB 스키마에 직접 의존하지 않음
    - Plug & Play: Redis 없이도 동작, 인프라 의존성 최소화

    Example:
        >>> class MyAdapter:
        ...     def get_dlq_pending_count(self, domain: str) -> int:
        ...         return MyDLQModel.objects.filter(domain=domain, status='pending').count()
        ...
        ...     def get_dlq_count_by_status(self, status: str) -> int:
        ...         return MyDLQModel.objects.filter(status=status).count()
        ...
        ...     def get_circuit_breaker_state(self, service: str) -> str:
        ...         return "closed"
        ...
        ...     def get_retry_success_rate(self, domain: str) -> float:
        ...         return 95.0
    """

    def get_dlq_pending_count(self, domain: str) -> int:
        """
        도메인별 대기 중인 DLQ 항목 수 반환.

        Args:
            domain: 도메인 이름 (payment, point, inventory 등)

        Returns:
            대기 중인 DLQ 항목 수
        """
        ...

    def get_dlq_count_by_status(self, status: str) -> int:
        """
        상태별 DLQ 항목 수 반환.

        Args:
            status: 상태 (pending, resolved, failed 등)

        Returns:
            해당 상태의 DLQ 항목 수
        """
        ...

    def get_circuit_breaker_state(self, service: str) -> str:
        """
        서비스의 Circuit Breaker 상태 반환.

        Args:
            service: 서비스 이름

        Returns:
            상태 문자열 (closed, open, half_open)
        """
        ...

    def get_retry_success_rate(self, domain: str) -> float:
        """
        도메인별 재시도 성공률 반환.

        Args:
            domain: 도메인 이름

        Returns:
            성공률 (0.0 ~ 100.0)
        """
        ...


class BaseMetricSourceAdapter(ABC):
    """
    메트릭 소스 어댑터 기본 클래스.

    모든 메서드에 대해 기본 구현 또는 예외를 제공합니다.
    구체적인 어댑터는 이 클래스를 상속하여 필요한 메서드만 오버라이드합니다.
    """

    @abstractmethod
    def get_dlq_pending_count(self, domain: str) -> int:
        """도메인별 대기 중인 DLQ 항목 수 반환."""
        raise NotImplementedError

    @abstractmethod
    def get_dlq_count_by_status(self, status: str) -> int:
        """상태별 DLQ 항목 수 반환."""
        raise NotImplementedError

    def get_circuit_breaker_state(self, service: str) -> str:
        """서비스의 Circuit Breaker 상태 반환. 기본값: closed."""
        return "closed"

    def get_retry_success_rate(self, domain: str) -> float:
        """도메인별 재시도 성공률 반환. 기본값: 0.0."""
        return 0.0


class NullMetricSourceAdapter(BaseMetricSourceAdapter):
    """
    No-op 메트릭 소스 어댑터.

    어댑터가 설정되지 않았을 때 사용됩니다.
    모든 메서드가 기본값(0, "closed")을 반환합니다.
    """

    def get_dlq_pending_count(self, domain: str) -> int:
        """항상 0을 반환합니다."""
        return 0

    def get_dlq_count_by_status(self, status: str) -> int:
        """항상 0을 반환합니다."""
        return 0

    def get_circuit_breaker_state(self, service: str) -> str:
        """항상 'closed'를 반환합니다."""
        return "closed"

    def get_retry_success_rate(self, domain: str) -> float:
        """항상 0.0을 반환합니다."""
        return 0.0


__all__ = [
    "MetricSourceAdapter",
    "BaseMetricSourceAdapter",
    "NullMetricSourceAdapter",
]
