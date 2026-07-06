"""
Cascade Auditor 공통 헬퍼.

인덱스 접근, backend 조회 등 반복 패턴을 통합합니다.
"""

from __future__ import annotations

from typing import Any


def get_index_ids(backend: Any, index_key: str) -> list[str]:
    """
    인덱스에서 ID 목록을 추출하는 통합 헬퍼.

    기존 코드에서 6회 반복되던 패턴을 통합:
        index_data if isinstance(index_data, list) else index_data.get("ids", [])

    Args:
        backend: State backend 인스턴스
        index_key: 인덱스 Redis 키

    Returns:
        cascade ID 목록
    """
    index_data = backend.get(index_key)
    if not index_data:
        return []
    return index_data if isinstance(index_data, list) else index_data.get("ids", [])
