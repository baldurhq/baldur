"""
Security 테스트 공통 fixture.

이 디렉토리의 3개 이상 테스트 파일에서 사용하는 공유 fixture 정의.
(§5.1: 2파일 이상 같은 디렉토리 공유 시 conftest.py 배치)
"""

from __future__ import annotations

import pytest

from baldur.services.security.hooks import clear_session_invalidation_hooks


@pytest.fixture(autouse=True)
def _reset_hooks():
    """각 테스트 전후로 콜백 레지스트리 초기화 (격리 목적)."""
    clear_session_invalidation_hooks()
    yield
    clear_session_invalidation_hooks()
