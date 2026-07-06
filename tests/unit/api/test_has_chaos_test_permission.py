"""
HasChaosTestPermission 권한 클래스 단위 테스트.

X-Test/Chaos API 2중 보안 장치 중 1차 Django RBAC 권한을 테스트합니다.

테스트 케이스:
- test_chaos_tester_group_allowed: baldur_chaos_tester 그룹 멤버 허용
- test_admin_group_allowed: baldur_admin 그룹 멤버 허용
- test_superuser_always_allowed: Django superuser 자동 허용
- test_anonymous_denied: 미인증 사용자 거부
- test_authenticated_no_group_denied: 인증되었지만 그룹 없음 거부
- test_production_always_denied: 프로덕션 환경 무조건 거부
- test_auth_disabled_bypass: DISABLE_BALDUR_AUTH=true 시 바이패스
"""

import os
from unittest.mock import MagicMock, patch

# Django 설정 구성 (테스트용)
import django
import pytest
from django.conf import settings

if not settings.configured:
    settings.configure(
        DEBUG=True,
        DATABASES={},
        INSTALLED_APPS=[
            "django.contrib.contenttypes",
            "django.contrib.auth",
        ],
        REST_FRAMEWORK={},
        SECRET_KEY="test-secret-key",
    )
    django.setup()


class TestHasChaosTestPermission:
    """HasChaosTestPermission 권한 클래스 테스트."""

    @pytest.fixture(autouse=True)
    def _runtime_isolation(self):
        """Drop the runtime around each test so per-test ``@patch.dict``
        ``BALDUR_ENVIRONMENT`` values are observed at construction time
        (463 D1 — runtime eager-reads the env var)."""
        from baldur.runtime import reset_runtime

        reset_runtime()
        yield
        reset_runtime()

    @pytest.fixture
    def permission_class(self):
        """HasChaosTestPermission 인스턴스 반환."""
        from baldur.api.django.permissions import HasChaosTestPermission

        return HasChaosTestPermission()

    @pytest.fixture
    def mock_request(self):
        """기본 mock request 객체 생성."""
        request = MagicMock()
        request.path = "/api/baldur/xtest/test/"
        request.META = {}
        return request

    @pytest.fixture
    def mock_view(self):
        """mock view 객체 생성."""
        return MagicMock()

    @pytest.fixture
    def mock_user(self):
        """기본 mock user 객체 생성."""
        user = MagicMock()
        user.is_authenticated = True
        user.is_superuser = False
        user.groups = MagicMock()
        return user

    @pytest.fixture
    def mock_chaos_tester_group(self, mock_user):
        """baldur_chaos_tester 그룹에 속한 사용자 mock."""
        # groups.filter().exists() 체인 모킹
        filter_qs = MagicMock()
        filter_qs.exists.return_value = True
        filter_qs.values_list.return_value = ["baldur_chaos_tester"]
        mock_user.groups.filter.return_value = filter_qs
        return mock_user

    @pytest.fixture
    def mock_admin_group(self, mock_user):
        """baldur_admin 그룹에 속한 사용자 mock."""
        filter_qs = MagicMock()
        filter_qs.exists.return_value = True
        filter_qs.values_list.return_value = ["baldur_admin"]
        mock_user.groups.filter.return_value = filter_qs
        return mock_user

    @pytest.fixture
    def mock_no_group_user(self, mock_user):
        """그룹이 없는 인증된 사용자 mock."""
        filter_qs = MagicMock()
        filter_qs.exists.return_value = False
        mock_user.groups.filter.return_value = filter_qs
        return mock_user

    @pytest.fixture
    def mock_superuser(self, mock_user):
        """superuser mock."""
        mock_user.is_superuser = True
        return mock_user

    @pytest.fixture
    def mock_anonymous_user(self):
        """미인증 사용자 mock."""
        user = MagicMock()
        user.is_authenticated = False
        return user

    # =========================================================================
    # 그룹 기반 권한 테스트
    # =========================================================================

    @patch.dict(
        os.environ, {"BALDUR_ENVIRONMENT": "development", "DISABLE_BALDUR_AUTH": ""}
    )
    def test_chaos_tester_group_allowed(
        self, permission_class, mock_request, mock_view, mock_chaos_tester_group
    ):
        """baldur_chaos_tester 그룹 멤버는 허용된다."""
        mock_request.user = mock_chaos_tester_group

        result = permission_class.has_permission(mock_request, mock_view)

        assert result is True

    @patch.dict(
        os.environ, {"BALDUR_ENVIRONMENT": "development", "DISABLE_BALDUR_AUTH": ""}
    )
    def test_admin_group_allowed(
        self, permission_class, mock_request, mock_view, mock_admin_group
    ):
        """baldur_admin 그룹 멤버는 허용된다."""
        mock_request.user = mock_admin_group

        result = permission_class.has_permission(mock_request, mock_view)

        assert result is True

    # =========================================================================
    # Superuser 테스트
    # =========================================================================

    @patch.dict(
        os.environ, {"BALDUR_ENVIRONMENT": "development", "DISABLE_BALDUR_AUTH": ""}
    )
    def test_superuser_always_allowed(
        self, permission_class, mock_request, mock_view, mock_superuser
    ):
        """Django superuser는 자동 허용된다."""
        mock_request.user = mock_superuser

        result = permission_class.has_permission(mock_request, mock_view)

        assert result is True

    # =========================================================================
    # 인증 실패 테스트
    # =========================================================================

    @patch.dict(
        os.environ, {"BALDUR_ENVIRONMENT": "development", "DISABLE_BALDUR_AUTH": ""}
    )
    def test_anonymous_denied(
        self, permission_class, mock_request, mock_view, mock_anonymous_user
    ):
        """미인증 사용자는 거부된다."""
        mock_request.user = mock_anonymous_user

        result = permission_class.has_permission(mock_request, mock_view)

        assert result is False
        assert "Authentication required" in permission_class.message

    @patch.dict(
        os.environ, {"BALDUR_ENVIRONMENT": "development", "DISABLE_BALDUR_AUTH": ""}
    )
    def test_authenticated_no_group_denied(
        self, permission_class, mock_request, mock_view, mock_no_group_user
    ):
        """인증되었지만 그룹이 없으면 거부된다."""
        mock_request.user = mock_no_group_user

        result = permission_class.has_permission(mock_request, mock_view)

        assert result is False

    @patch.dict(
        os.environ, {"BALDUR_ENVIRONMENT": "development", "DISABLE_BALDUR_AUTH": ""}
    )
    def test_none_user_denied(self, permission_class, mock_request, mock_view):
        """request.user가 None이면 거부된다."""
        mock_request.user = None

        result = permission_class.has_permission(mock_request, mock_view)

        assert result is False

    # =========================================================================
    # 프로덕션 환경 차단 테스트
    # =========================================================================

    @patch.dict(
        os.environ, {"BALDUR_ENVIRONMENT": "production", "DISABLE_BALDUR_AUTH": ""}
    )
    def test_production_always_denied(
        self, permission_class, mock_request, mock_view, mock_superuser
    ):
        """In production, even superusers are denied (Fail-Secure)."""
        from baldur.runtime import reset_runtime

        # 463 D1: runtime eager-reads BALDUR_ENVIRONMENT at construction.
        # Reset so the @patch.dict-applied env value is observed.
        reset_runtime()
        mock_request.user = mock_superuser

        result = permission_class.has_permission(mock_request, mock_view)

        assert result is False
        assert "not available in production" in permission_class.message

    @patch.dict(
        os.environ, {"BALDUR_ENVIRONMENT": "production", "DISABLE_BALDUR_AUTH": ""}
    )
    def test_production_chaos_tester_denied(
        self, permission_class, mock_request, mock_view, mock_chaos_tester_group
    ):
        """In production, chaos_tester group members are denied as well."""
        from baldur.runtime import reset_runtime

        reset_runtime()
        mock_request.user = mock_chaos_tester_group

        result = permission_class.has_permission(mock_request, mock_view)

        assert result is False
        assert "not available in production" in permission_class.message

    # =========================================================================
    # 테스트 바이패스 테스트
    # =========================================================================

    @patch.dict(
        os.environ, {"BALDUR_ENVIRONMENT": "production", "DISABLE_BALDUR_AUTH": "true"}
    )
    def test_auth_disabled_blocked_in_production(
        self, permission_class, mock_request, mock_view, mock_anonymous_user
    ):
        """In production, DISABLE_BALDUR_AUTH=true cannot bypass auth (Fail-Secure).

        ``_is_auth_disabled()`` returns False unconditionally in production,
        so ``HasChaosTestPermission`` enters the production-block branch
        and must return False.
        """
        from baldur.runtime import reset_runtime

        reset_runtime()
        mock_request.user = mock_anonymous_user

        result = permission_class.has_permission(mock_request, mock_view)

        assert result is False

    @patch.dict(
        os.environ, {"BALDUR_ENVIRONMENT": "development", "DISABLE_BALDUR_AUTH": "1"}
    )
    def test_auth_disabled_bypass_with_1(
        self, permission_class, mock_request, mock_view, mock_anonymous_user
    ):
        """DISABLE_BALDUR_AUTH=1 시 바이패스된다."""
        mock_request.user = mock_anonymous_user

        result = permission_class.has_permission(mock_request, mock_view)

        assert result is True

    @patch.dict(
        os.environ, {"BALDUR_ENVIRONMENT": "development", "DISABLE_BALDUR_AUTH": "yes"}
    )
    def test_auth_disabled_bypass_with_yes(
        self, permission_class, mock_request, mock_view, mock_anonymous_user
    ):
        """DISABLE_BALDUR_AUTH=yes 시 바이패스된다."""
        mock_request.user = mock_anonymous_user

        result = permission_class.has_permission(mock_request, mock_view)

        assert result is True

    # =========================================================================
    # Fail-Secure 테스트
    # =========================================================================

    @patch.dict(
        os.environ, {"BALDUR_ENVIRONMENT": "development", "DISABLE_BALDUR_AUTH": ""}
    )
    def test_exception_during_group_check_denied(
        self, permission_class, mock_request, mock_view, mock_user
    ):
        """그룹 체크 중 예외 발생 시 거부된다 (Fail-Secure)."""
        mock_request.user = mock_user
        mock_user.groups.filter.side_effect = Exception("Database error")

        result = permission_class.has_permission(mock_request, mock_view)

        assert result is False
        assert "Permission check failed" in permission_class.message

    # =========================================================================
    # 환경 변수 케이스 테스트
    # =========================================================================

    @patch.dict(
        os.environ, {"BALDUR_ENVIRONMENT": "staging", "DISABLE_BALDUR_AUTH": ""}
    )
    def test_staging_environment_allowed(
        self, permission_class, mock_request, mock_view, mock_chaos_tester_group
    ):
        """staging 환경에서는 허용된다."""
        mock_request.user = mock_chaos_tester_group

        result = permission_class.has_permission(mock_request, mock_view)

        assert result is True

    @patch.dict(os.environ, {"DISABLE_BALDUR_AUTH": ""}, clear=True)
    def test_default_environment_allowed(
        self, permission_class, mock_request, mock_view, mock_chaos_tester_group
    ):
        """ENVIRONMENT 미설정 시 development로 간주하여 허용된다."""
        # ENVIRONMENT 환경변수 제거
        if "ENVIRONMENT" in os.environ:
            del os.environ["ENVIRONMENT"]

        mock_request.user = mock_chaos_tester_group

        result = permission_class.has_permission(mock_request, mock_view)

        assert result is True
