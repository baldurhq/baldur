"""
Air-Gap Storage Adapter Base Interface.

Provides an abstract interface for Air-Gap storage between
Baldur engine and business database.

Design Principles:
- Complete DB isolation: Engine never touches business DB
- Plug & Play: Enable/disable via configuration
- Graceful Fallback: Works without Redis (uses NullAdapter)
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class AirGapStorageAdapter(Protocol):
    """
    Air-Gap 저장소 어댑터 인터페이스.

    비즈니스 레이어는 DB 변경 시 이 어댑터를 통해 요약 상태를 기록하고,
    Baldur 엔진은 이 어댑터를 통해서만 상태를 조회합니다.

    Architecture:
        ┌──────────────┐
        │ Business DB  │  ← Baldur 엔진 접근 금지
        └──────────────┘
               │
               │ (비즈니스 레이어가 요약 기록)
               ▼
        ┌──────────────┐
        │  Air-Gap     │  ← Redis 또는 다른 캐시
        │  Storage     │
        └──────────────┘
               │
               │ (Baldur 엔진 읽기 전용)
               ▼
        ┌──────────────┐
        │ Baldur │
        │    Engine    │
        └──────────────┘

    Example:
        >>> class MyAirGapAdapter:
        ...     def write_summary(self, key: str, value: Any, ttl: int = None) -> bool:
        ...         redis.set(f"sh:airgap:{key}", value, ex=ttl)
        ...         return True
        ...
        ...     def read_summary(self, key: str) -> Any:
        ...         return redis.get(f"sh:airgap:{key}")
        ...
        ...     def is_enabled(self) -> bool:
        ...         return True
    """

    def write_summary(self, key: str, value: Any, ttl: int | None = None) -> bool:
        """
        요약 상태를 Air-Gap 저장소에 기록.

        비즈니스 레이어에서 DB 변경 시 호출합니다.

        Args:
            key: 저장소 키 (예: "dlq:payment:pending", "cb:toss:state")
            value: 저장할 값 (직렬화 가능해야 함)
            ttl: Time-to-live in seconds (optional)

        Returns:
            성공 여부
        """
        ...

    def read_summary(self, key: str) -> Any:
        """
        Air-Gap 저장소에서 요약 상태 조회.

        Baldur 엔진에서 메트릭 조회 시 호출합니다.

        Args:
            key: 저장소 키

        Returns:
            저장된 값 또는 None
        """
        ...

    def delete_summary(self, key: str) -> bool:
        """
        Air-Gap 저장소에서 요약 상태 삭제.

        Args:
            key: 저장소 키

        Returns:
            성공 여부
        """
        ...

    def read_many(self, keys: list[str]) -> dict[str, Any]:
        """
        여러 키의 값을 한 번에 조회.

        Args:
            keys: 조회할 키 목록

        Returns:
            키-값 딕셔너리
        """
        ...

    def increment(self, key: str, amount: int = 1) -> int:
        """
        카운터 값 증가.

        Args:
            key: 저장소 키
            amount: 증가량

        Returns:
            증가 후 값
        """
        ...

    def decrement(self, key: str, amount: int = 1) -> int:
        """
        카운터 값 감소 (음수 방지).

        Args:
            key: 저장소 키
            amount: 감소량

        Returns:
            감소 후 값 (최소 0)
        """
        ...

    def is_enabled(self) -> bool:
        """
        Air-Gap 기능 활성화 여부.

        Returns:
            True if enabled, False otherwise
        """
        ...


class BaseAirGapAdapter(ABC):
    """
    Air-Gap 저장소 어댑터 기본 클래스.

    구체적인 어댑터는 이 클래스를 상속하여 구현합니다.
    """

    @abstractmethod
    def write_summary(self, key: str, value: Any, ttl: int | None = None) -> bool:
        """요약 상태를 Air-Gap 저장소에 기록."""
        raise NotImplementedError

    @abstractmethod
    def read_summary(self, key: str) -> Any:
        """Air-Gap 저장소에서 요약 상태 조회."""
        raise NotImplementedError

    @abstractmethod
    def delete_summary(self, key: str) -> bool:
        """Air-Gap 저장소에서 요약 상태 삭제."""
        raise NotImplementedError

    def read_many(self, keys: list[str]) -> dict[str, Any]:
        """여러 키의 값을 한 번에 조회. 기본 구현은 개별 조회."""
        return {key: self.read_summary(key) for key in keys}

    def increment(self, key: str, amount: int = 1) -> int:
        """카운터 값 증가. 기본 구현은 read-modify-write."""
        current = self.read_summary(key)
        new_value = (int(current) if current else 0) + amount
        self.write_summary(key, new_value)
        return new_value

    def decrement(self, key: str, amount: int = 1) -> int:
        """카운터 값 감소 (음수 방지). 기본 구현은 read-modify-write."""
        current = self.read_summary(key)
        current_int = int(current) if current else 0
        new_value = max(0, current_int - amount)
        self.write_summary(key, new_value)
        return new_value

    @abstractmethod
    def is_enabled(self) -> bool:
        """Air-Gap 기능 활성화 여부."""
        raise NotImplementedError


# Key 생성 헬퍼 함수들
class AirGapKeys:
    """Air-Gap 저장소 키 생성 헬퍼."""

    PREFIX = "sh:airgap:"

    @classmethod
    def dlq_pending(cls, domain: str) -> str:
        """DLQ pending count 키."""
        return f"{cls.PREFIX}dlq:{domain}:pending"

    @classmethod
    def dlq_status(cls, domain: str, status: str) -> str:
        """DLQ status count 키."""
        return f"{cls.PREFIX}dlq:{domain}:{status}"

    @classmethod
    def circuit_breaker_state(cls, service: str) -> str:
        """Circuit breaker state 키."""
        return f"{cls.PREFIX}cb:{service}:state"

    @classmethod
    def circuit_breaker_failure_count(cls, service: str) -> str:
        """Circuit breaker failure count 키."""
        return f"{cls.PREFIX}cb:{service}:failures"

    @classmethod
    def retry_success_count(cls, domain: str) -> str:
        """Retry success count 키."""
        return f"{cls.PREFIX}retry:{domain}:success"

    @classmethod
    def retry_failure_count(cls, domain: str) -> str:
        """Retry failure count 키."""
        return f"{cls.PREFIX}retry:{domain}:failure"


__all__ = [
    "AirGapStorageAdapter",
    "BaseAirGapAdapter",
    "AirGapKeys",
]
