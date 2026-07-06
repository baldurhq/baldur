"""
UserSessionRegistry 단위 테스트.

user_id → session_key 역방향 매핑의 등록, 제거, 조회, 전체 무효화를 검증한다.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from baldur.services.security.session_registry import (
    UserSessionRegistry,
    get_user_session_registry,
    reset_user_session_registry,
)


@pytest.fixture
def mock_cache():
    """Mock CacheProviderInterface."""
    return MagicMock()


@pytest.fixture
def registry(mock_cache):
    """UserSessionRegistry 인스턴스 (mock cache 주입)."""
    return UserSessionRegistry(cache=mock_cache)


class TestRegisterBehavior:
    """register() 동작 검증."""

    def test_adds_session_key_to_empty_list(self, registry, mock_cache):
        """빈 상태에서 session_key 등록 시 리스트에 추가."""
        mock_cache.get.return_value = None
        registry.register(user_id=1, session_key="abc123")
        mock_cache.set.assert_called_once()
        args = mock_cache.set.call_args
        assert "abc123" in args[0][1]

    def test_appends_to_existing_sessions(self, registry, mock_cache):
        """기존 세션이 있을 때 새 session_key가 추가됨."""
        mock_cache.get.return_value = ["session_a"]
        registry.register(user_id=1, session_key="session_b")
        args = mock_cache.set.call_args
        saved_list = args[0][1]
        assert "session_a" in saved_list
        assert "session_b" in saved_list

    def test_deduplicates_same_session_key(self, registry, mock_cache):
        """동일 session_key 중복 등록 방지."""
        mock_cache.get.return_value = ["abc123"]
        registry.register(user_id=1, session_key="abc123")
        args = mock_cache.set.call_args
        assert args[0][1].count("abc123") == 1

    def test_failure_does_not_raise(self, registry, mock_cache):
        """Redis 장애 시 예외 없이 종료 (graceful)."""
        mock_cache.get.side_effect = Exception("Redis down")
        registry.register(user_id=1, session_key="abc")
        # 예외 없이 종료되어야 함


class TestUnregisterBehavior:
    """unregister() 동작 검증."""

    def test_removes_specific_session_key(self, registry, mock_cache):
        """특정 session_key만 제거."""
        mock_cache.get.return_value = ["keep_this", "remove_this"]
        registry.unregister(user_id=1, session_key="remove_this")
        args = mock_cache.set.call_args
        saved_list = args[0][1]
        assert "keep_this" in saved_list
        assert "remove_this" not in saved_list

    def test_deletes_registry_key_when_last_session_removed(self, registry, mock_cache):
        """마지막 session_key 제거 시 레지스트리 키 자체를 삭제."""
        mock_cache.get.return_value = ["only_session"]
        registry.unregister(user_id=1, session_key="only_session")
        mock_cache.delete.assert_called_once()

    def test_no_op_when_session_key_not_found(self, registry, mock_cache):
        """존재하지 않는 session_key 제거 시도 시 에러 없이 통과."""
        mock_cache.get.return_value = ["other_session"]
        registry.unregister(user_id=1, session_key="nonexistent")
        # set이 호출되어야 함 (기존 리스트 유지)
        mock_cache.set.assert_called_once()


class TestGetSessionKeysBehavior:
    """get_session_keys() 동작 검증."""

    def test_returns_session_key_list(self, registry, mock_cache):
        """등록된 session_key 리스트를 반환."""
        mock_cache.get.return_value = ["s1", "s2"]
        result = registry.get_session_keys(user_id=1)
        assert result == ["s1", "s2"]

    def test_returns_empty_list_when_no_sessions(self, registry, mock_cache):
        """등록된 세션이 없을 때 빈 리스트 반환."""
        mock_cache.get.return_value = None
        assert registry.get_session_keys(user_id=1) == []

    def test_returns_empty_list_on_cache_error(self, registry, mock_cache):
        """캐시 오류 시 빈 리스트 반환."""
        mock_cache.get.side_effect = Exception("Redis error")
        assert registry.get_session_keys(user_id=1) == []


class TestInvalidateAllBehavior:
    """invalidate_all() 동작 검증."""

    def test_deletes_all_sessions_and_registry_key(self, registry, mock_cache):
        """모든 session_key + 레지스트리 키를 삭제."""
        mock_cache.get.return_value = ["s1", "s2"]
        deleted = registry.invalidate_all(user_id=1)
        assert deleted == 2
        # 각 session_key 삭제(x2 패턴) + 레지스트리 키 삭제
        assert mock_cache.delete.call_count >= 3

    def test_returns_zero_when_no_sessions(self, registry, mock_cache):
        """등록된 세션 없을 때 0 반환."""
        mock_cache.get.return_value = None
        deleted = registry.invalidate_all(user_id=1)
        assert deleted == 0


class TestKeyFormatContract:
    """캐시 키 형식 계약 검증."""

    def test_key_prefix_uses_security_namespace(self, registry):
        """키 프리픽스가 security: 네임스페이스를 따르는지 확인."""
        key = registry._key(42)
        assert key == f"{UserSessionRegistry.KEY_PREFIX}42"
        assert key.startswith("security:")


class TestSingletonBehavior:
    """싱글톤 패턴 동작 검증."""

    def test_reset_clears_singleton(self):
        """reset_user_session_registry()가 싱글톤을 초기화하는지 확인."""
        instance1 = get_user_session_registry()
        reset_user_session_registry()
        instance2 = get_user_session_registry()

        assert instance1 is not instance2
