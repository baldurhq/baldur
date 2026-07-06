"""
SecurityViolationService 세션 무효화 콜백 호출 테스트.

_invalidate_user_sessions()에서 등록된 콜백이 올바르게 호출되고,
실패 시에도 나머지 무효화가 계속 진행되는지 검증합니다.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from baldur.services.security.hooks import (
    register_session_invalidation_hook,
)
from baldur.services.security.models import SecurityConfig
from baldur.services.security.service import SecurityViolationService

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def mock_cache():
    """Mock cache provider."""
    cache = MagicMock()
    cache.get.return_value = None
    cache.delete.return_value = True
    return cache


@pytest.fixture
def service(mock_cache):
    """SecurityViolationService 인스턴스 (mock cache 주입)."""
    return SecurityViolationService(
        config=SecurityConfig(),
        cache=mock_cache,
    )


# =============================================================================
# Tests
# =============================================================================


class TestInvalidateUserSessionsWithHooksBehavior:
    """_invalidate_user_sessions() 콜백 호출 동작 검증."""

    @pytest.fixture(autouse=True)
    def _skip_django_db_sessions(self):
        """Django DB 세션 삭제의 DB 연결 시도를 방지 (테스트 대상 아님)."""
        with patch.object(
            SecurityViolationService,
            "_invalidate_django_db_sessions",
            return_value=[],
        ):
            yield

    @patch("baldur.services.security.service.log_security_violation_audit")
    def test_hook_called_on_invalidation(self, mock_audit, service):
        """_invalidate_user_sessions() 호출 시 등록된 콜백이 실행되는지 확인."""
        mock_hook = MagicMock(return_value="jwt_blacklisted(3)")
        register_session_invalidation_hook(mock_hook)

        service._invalidate_user_sessions(42)

        mock_hook.assert_called_once_with(42)

    @patch("baldur.services.security.service.log_security_violation_audit")
    def test_hook_result_in_invalidated_items(self, mock_audit, service):
        """콜백 반환값이 결과에 포함되는지 확인."""
        register_session_invalidation_hook(lambda uid: "jwt_blacklisted(5)")

        result = service._invalidate_user_sessions(42)

        assert "jwt_blacklisted(5)" in result

    @patch("baldur.services.security.service.log_security_violation_audit")
    def test_hook_empty_result_not_in_items(self, mock_audit, service):
        """콜백이 빈 문자열 반환 시 결과에 포함되지 않는지 확인."""
        register_session_invalidation_hook(lambda uid: "")

        result = service._invalidate_user_sessions(42)

        assert "jwt_blacklisted" not in result

    @patch("baldur.services.security.service.log_security_violation_audit")
    def test_hook_failure_does_not_block(self, mock_audit, service):
        """콜백 실패 시 나머지 무효화가 계속 진행되는지 확인."""

        def failing_hook(uid: int) -> str:
            raise RuntimeError("Hook crashed")

        success_hook = MagicMock(return_value="second_hook_ok")

        register_session_invalidation_hook(failing_hook)
        register_session_invalidation_hook(success_hook)

        result = service._invalidate_user_sessions(42)

        # 첫 번째 콜백은 실패하지만 두 번째는 실행됨
        success_hook.assert_called_once_with(42)
        assert "second_hook_ok" in result
        # 세션 무효화(UserSessionRegistry)는 여전히 포함
        assert "redis_sessions" in result

    @patch("baldur.services.security.service.log_security_violation_audit")
    def test_no_hooks_behaves_same_as_before(self, mock_audit, service):
        """콜백 미등록 시 기존 동작과 동일한지 확인."""
        # 콜백 미등록 상태
        result = service._invalidate_user_sessions(42)

        assert "redis_sessions" in result
        assert "jwt_blacklisted" not in result

    @patch("baldur.services.security.service.log_security_violation_audit")
    def test_multiple_hooks_all_called(self, mock_audit, service):
        """여러 콜백이 모두 순서대로 호출되는지 확인."""
        results = []

        def hook_a(uid: int) -> str:
            results.append("a")
            return "hook_a_done"

        def hook_b(uid: int) -> str:
            results.append("b")
            return "hook_b_done"

        register_session_invalidation_hook(hook_a)
        register_session_invalidation_hook(hook_b)

        service._invalidate_user_sessions(42)

        assert results == ["a", "b"]

    @patch("baldur.services.security.service.log_security_violation_audit")
    def test_hook_result_in_audit_details(self, mock_audit, service):
        """콜백 결과가 audit 기록의 invalidated 목록에 포함되는지 확인."""
        register_session_invalidation_hook(lambda uid: "jwt_blacklisted(2)")

        service._invalidate_user_sessions(42)

        # audit 호출에서 details 확인
        mock_audit.assert_called()
        call_kwargs = mock_audit.call_args
        invalidated = call_kwargs.kwargs.get("details", {}).get("invalidated", [])
        assert "jwt_blacklisted(2)" in invalidated
