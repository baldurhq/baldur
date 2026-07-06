"""
Null Air-Gap Storage Adapter.

No-op implementation used when Air-Gap feature is disabled.
All operations are pass-through with no side effects.
"""

from __future__ import annotations

from typing import Any

import structlog

from baldur.adapters.airgap.base import BaseAirGapAdapter

logger = structlog.get_logger()


class NullAirGapAdapter(BaseAirGapAdapter):
    """
    비활성화용 No-op Air-Gap 어댑터.

    Air-Gap 기능이 비활성화된 경우 사용됩니다.
    모든 쓰기 작업은 무시되고, 읽기 작업은 None을 반환합니다.
    기존 로직이 그대로 동작합니다.

    Example:
        >>> adapter = NullAirGapAdapter()
        >>> adapter.write_summary("key", "value")  # 무시됨
        True
        >>> adapter.read_summary("key")  # None 반환
        None
        >>> adapter.is_enabled()
        False
    """

    def __init__(self) -> None:
        """Initialize NullAirGapAdapter."""
        logger.debug("air_gap.nullairgapadapter_initialized_air_gap")

    def write_summary(self, key: str, value: Any, ttl: int | None = None) -> bool:
        """
        쓰기 작업 무시 (no-op).

        Args:
            key: 저장소 키 (무시됨)
            value: 저장할 값 (무시됨)
            ttl: TTL (무시됨)

        Returns:
            항상 True (성공으로 간주)
        """
        return True

    def read_summary(self, key: str) -> Any:
        """
        항상 None 반환.

        Args:
            key: 저장소 키

        Returns:
            항상 None (Air-Gap에 데이터 없음)
        """
        return None

    def delete_summary(self, key: str) -> bool:
        """
        삭제 작업 무시 (no-op).

        Args:
            key: 저장소 키 (무시됨)

        Returns:
            항상 True (성공으로 간주)
        """
        return True

    def read_many(self, keys: list[str]) -> dict[str, Any]:
        """
        모든 키에 대해 None 반환.

        Args:
            keys: 조회할 키 목록

        Returns:
            모든 값이 None인 딕셔너리
        """
        return dict.fromkeys(keys)

    def increment(self, key: str, amount: int = 1) -> int:
        """
        증가 작업 무시 (no-op).

        Args:
            key: 저장소 키 (무시됨)
            amount: 증가량 (무시됨)

        Returns:
            항상 0
        """
        return 0

    def decrement(self, key: str, amount: int = 1) -> int:
        """
        감소 작업 무시 (no-op).

        Args:
            key: 저장소 키 (무시됨)
            amount: 감소량 (무시됨)

        Returns:
            항상 0
        """
        return 0

    def is_enabled(self) -> bool:
        """
        Air-Gap 비활성화 상태.

        Returns:
            항상 False
        """
        return False


__all__ = ["NullAirGapAdapter"]
