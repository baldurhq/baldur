"""
_invalidate_user_sessions() 재작성 검증.

Dead Code 제거(user_token: 등), UserSessionRegistry 연동,
SESSION_ENGINE 조건부 체크를 검증한다.
"""

from __future__ import annotations

import inspect
from unittest.mock import MagicMock, patch

import pytest

from baldur.services.security.hooks import register_session_invalidation_hook
from baldur.services.security.models import SecurityConfig
from baldur.services.security.service import SecurityViolationService

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def mock_cache():
    cache = MagicMock()
    cache.get.return_value = None
    cache.delete.return_value = True
    return cache


@pytest.fixture
def service(mock_cache):
    return SecurityViolationService(
        config=SecurityConfig(),
        cache=mock_cache,
    )


# =============================================================================
# Dead Code 제거 검증
# =============================================================================


class TestDeadCodeRemovalContract:
    """이전 Dead Code (user_token:, user_permissions:, user_auth:) 제거 계약 확인."""

    def test_no_user_token_reference(self):
        """_invalidate_user_sessions 소스에 user_token: 문자열이 없어야 함."""
        source = inspect.getsource(SecurityViolationService._invalidate_user_sessions)
        assert "user_token:" not in source

    def test_no_user_permissions_reference(self):
        """_invalidate_user_sessions 소스에 user_permissions: 문자열이 없어야 함."""
        source = inspect.getsource(SecurityViolationService._invalidate_user_sessions)
        assert "user_permissions:" not in source

    def test_no_user_auth_reference(self):
        """_invalidate_user_sessions 소스에 user_auth: 문자열이 없어야 함."""
        source = inspect.getsource(SecurityViolationService._invalidate_user_sessions)
        assert "user_auth:" not in source

    def test_no_user_session_hardcoded_key(self):
        """이전 user_session:{user_id} 하드코딩 캐시 키가 없어야 함."""
        source = inspect.getsource(SecurityViolationService._invalidate_user_sessions)
        assert "user_session:" not in source


# =============================================================================
# UserSessionRegistry 연동 검증
# =============================================================================


class TestUserSessionRegistryIntegrationBehavior:
    """UserSessionRegistry.invalidate_all() 호출 동작 검증."""

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
    @patch("baldur.services.security.session_registry.get_user_session_registry")
    def test_registry_invalidate_all_called(
        self, mock_get_registry, mock_audit, service
    ):
        """invalidate_all()이 user_id와 함께 호출되는지 확인."""
        mock_registry = MagicMock()
        mock_registry.invalidate_all.return_value = 3
        mock_get_registry.return_value = mock_registry

        result = service._invalidate_user_sessions(42)

        mock_registry.invalidate_all.assert_called_once_with(42)
        assert "redis_sessions(3)" in result

    @patch("baldur.services.security.service.log_security_violation_audit")
    @patch("baldur.services.security.session_registry.get_user_session_registry")
    def test_registry_zero_sessions(self, mock_get_registry, mock_audit, service):
        """등록된 세션이 없을 때 no_registered_keys 표시."""
        mock_registry = MagicMock()
        mock_registry.invalidate_all.return_value = 0
        mock_get_registry.return_value = mock_registry

        result = service._invalidate_user_sessions(42)

        assert "no_registered_keys" in result


# =============================================================================
# SESSION_ENGINE 조건부 체크 검증
# =============================================================================


class TestSessionEngineCheckBehavior:
    """SESSION_ENGINE에 따른 DB 스캔 조건부 실행 동작 검증."""

    @patch("baldur.services.security.service.log_security_violation_audit")
    def test_skips_db_scan_for_cache_backend(self, mock_audit, service):
        """SESSION_ENGINE이 cache일 때 django_sessions 결과 미포함."""
        with patch(
            "baldur.services.security.service.getattr",
            side_effect=lambda obj, name, default=None: (
                "django.contrib.sessions.backends.cache"
                if name == "SESSION_ENGINE"
                else getattr(obj, name, default)
            ),
            create=True,
        ):
            result = service._invalidate_user_sessions(42)
            # cache 백엔드에서는 DB 스캔하지 않으므로 django_sessions 미포함
            assert "django_sessions(" not in result or "django_sessions(0" not in result


# =============================================================================
# Hooks 유지 검증
# =============================================================================


class TestHooksStillExecutedBehavior:
    """기존 hooks 동작이 유지되는지 검증."""

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
    def test_hooks_called_after_rewrite(self, mock_audit, service):
        """재작성 후에도 hook이 실행되는지 확인."""
        mock_hook = MagicMock(return_value="jwt_blacklisted(5)")
        register_session_invalidation_hook(mock_hook)

        result = service._invalidate_user_sessions(42)

        mock_hook.assert_called_once_with(42)
        assert "jwt_blacklisted(5)" in result
