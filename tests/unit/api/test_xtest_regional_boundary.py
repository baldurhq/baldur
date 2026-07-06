"""
X-Test Regional Boundary (리전 스코프 강제) 단위 테스트.

X-Test API 호출이 현재 리전 범위를 벗어나지 않도록 강제하는 기능을 테스트합니다.

테스트 케이스:
- test_local_scope_no_header_required: LOCAL scope API는 X-Region 헤더 불필요
- test_global_scope_header_required: GLOBAL scope API는 X-Region 헤더 필수
- test_region_mismatch_denied: 리전 불일치 시 403 거부
- test_region_match_allowed: 리전 일치 시 허용
- test_region_not_configured_global_denied: 리전 미설정 시 GLOBAL scope 거부
- test_development_env_no_region_warning: 개발 환경에서 리전 미설정 시 경고만
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


class TestXTestRegionalBoundary:
    """X-Test 리전 경계 강제 테스트."""

    @pytest.fixture
    def mixin(self):
        """XTestModeMixin 인스턴스 반환."""
        from baldur.api.django.views.xtest.base import XTestModeMixin

        class TestView(XTestModeMixin):
            pass

        return TestView()

    @pytest.fixture
    def mock_local_request(self):
        """LOCAL scope API 요청 mock 생성."""
        request = MagicMock()
        request.path = "/api/baldur/xtest/dlq/inject/"
        request.headers = {
            "X-Test-Mode": "chaos-monkey",
        }
        return request

    @pytest.fixture
    def mock_global_request(self):
        """GLOBAL scope API 요청 mock 생성."""
        request = MagicMock()
        request.path = "/api/baldur/xtest/emergency/global/set/"
        request.headers = {
            "X-Test-Mode": "chaos-monkey",
        }
        return request

    @pytest.fixture
    def mock_isolation_request(self):
        """리전 격리 API 요청 mock 생성."""
        request = MagicMock()
        request.path = "/api/baldur/xtest/isolation/region/isolate/"
        request.headers = {
            "X-Test-Mode": "chaos-monkey",
        }
        return request

    # =========================================================================
    # is_global_scope_endpoint 테스트
    # =========================================================================

    def test_local_scope_endpoint_detection(self, mixin, mock_local_request):
        """LOCAL scope API는 is_global_scope_endpoint가 False 반환."""
        result = mixin.is_global_scope_endpoint(mock_local_request)
        assert result is False

    def test_global_scope_endpoint_detection_emergency(
        self, mixin, mock_global_request
    ):
        """GLOBAL scope emergency API는 is_global_scope_endpoint가 True 반환."""
        result = mixin.is_global_scope_endpoint(mock_global_request)
        assert result is True

    def test_global_scope_endpoint_detection_isolation(
        self, mixin, mock_isolation_request
    ):
        """GLOBAL scope isolation API는 is_global_scope_endpoint가 True 반환."""
        result = mixin.is_global_scope_endpoint(mock_isolation_request)
        assert result is True

    def test_global_scope_endpoint_governance(self, mixin):
        """GLOBAL scope governance API는 is_global_scope_endpoint가 True 반환."""
        request = MagicMock()
        request.path = "/api/baldur/xtest/governance/global/update/"

        result = mixin.is_global_scope_endpoint(request)
        assert result is True

    # =========================================================================
    # get_current_region 테스트
    # =========================================================================

    @patch.dict(os.environ, {"BALDUR_NAMESPACE_REGION": "seoul"}, clear=False)
    def test_get_current_region_from_env(self, mixin):
        """환경변수에서 리전 조회."""
        result = mixin.get_current_region()
        assert result == "seoul"

    @patch.dict(os.environ, {}, clear=True)
    def test_get_current_region_from_cluster_identity(self, mixin):
        """ClusterIdentity에서 리전 조회."""
        mock_identity = MagicMock()
        mock_identity.region = "tokyo"

        with patch(
            "baldur.core.cluster_identity.get_cluster_identity",
            return_value=mock_identity,
        ):
            result = mixin.get_current_region()
            assert result == "tokyo"

    @patch.dict(os.environ, {}, clear=True)
    def test_get_current_region_none_when_not_configured(self, mixin):
        """리전 미설정 시 None 반환."""
        with patch(
            "baldur.core.cluster_identity.get_cluster_identity",
            side_effect=Exception("Identity not available"),
        ):
            result = mixin.get_current_region()
            assert result is None

    # =========================================================================
    # check_regional_scope 테스트
    # =========================================================================

    def test_local_scope_no_header_required(self, mixin, mock_local_request):
        """LOCAL scope API는 X-Region 헤더 없이도 허용."""
        is_allowed, response = mixin.check_regional_scope(mock_local_request)

        assert is_allowed is True
        assert response is None

    @patch.dict(os.environ, {"BALDUR_NAMESPACE_REGION": "seoul"}, clear=False)
    def test_global_scope_header_required(self, mixin, mock_global_request):
        """GLOBAL scope API는 X-Region 헤더 필수."""
        # X-Region 헤더 없음
        is_allowed, response = mixin.check_regional_scope(mock_global_request)

        assert is_allowed is False
        assert response is not None
        assert response.status_code == 403
        assert response.data["error"] == "missing_region_header"
        assert response.data["current_region"] == "seoul"

    @patch.dict(os.environ, {"BALDUR_NAMESPACE_REGION": "seoul"}, clear=False)
    def test_region_mismatch_denied(self, mixin, mock_global_request):
        """리전 불일치 시 403 거부."""
        mock_global_request.headers["X-Region"] = "tokyo"

        is_allowed, response = mixin.check_regional_scope(mock_global_request)

        assert is_allowed is False
        assert response is not None
        assert response.status_code == 403
        assert response.data["error"] == "cross_region_xtest_denied"
        assert response.data["current_region"] == "seoul"
        assert response.data["target_region"] == "tokyo"

    @patch.dict(os.environ, {"BALDUR_NAMESPACE_REGION": "seoul"}, clear=False)
    def test_region_match_allowed(self, mixin, mock_global_request):
        """리전 일치 시 허용."""
        mock_global_request.headers["X-Region"] = "seoul"

        is_allowed, response = mixin.check_regional_scope(mock_global_request)

        assert is_allowed is True
        assert response is None

    @patch.dict(os.environ, {"BALDUR_NAMESPACE_REGION": "SEOUL"}, clear=False)
    def test_region_match_case_insensitive(self, mixin, mock_global_request):
        """리전 비교는 대소문자 무시."""
        mock_global_request.headers["X-Region"] = "seoul"

        is_allowed, response = mixin.check_regional_scope(mock_global_request)

        assert is_allowed is True
        assert response is None

    @patch.dict(os.environ, {"ENVIRONMENT": "staging"}, clear=True)
    def test_region_not_configured_global_denied(self, mixin, mock_global_request):
        """리전 미설정 + 비개발 환경에서 GLOBAL scope 거부."""
        with patch.object(mixin, "get_current_region", return_value=None):
            is_allowed, response = mixin.check_regional_scope(mock_global_request)

            assert is_allowed is False
            assert response is not None
            assert response.status_code == 403
            assert response.data["error"] == "region_not_configured"

    @patch.dict(os.environ, {"ENVIRONMENT": "development"}, clear=True)
    def test_development_env_no_region_warning(self, mixin, mock_global_request):
        """개발 환경에서 리전 미설정 시 경고만 출력하고 허용."""
        with patch.object(mixin, "get_current_region", return_value=None):
            is_allowed, response = mixin.check_regional_scope(mock_global_request)

            assert is_allowed is True
            assert response is None

    # =========================================================================
    # check_chaos_permission 통합 테스트
    # =========================================================================

    @patch.dict(
        os.environ,
        {
            "BALDUR_NAMESPACE_REGION": "seoul",
            "CHAOS_ENABLED": "true",
            "ENVIRONMENT": "staging",
        },
        clear=False,
    )
    def test_check_chaos_permission_includes_regional_check(
        self, mixin, mock_global_request
    ):
        """check_chaos_permission이 리전 체크를 포함."""
        pytest.importorskip("baldur_pro")
        # Resource guard가 safe 상태를 반환하도록 mock
        mock_guard = MagicMock()
        mock_result = MagicMock()
        mock_result.is_safe = True
        mock_guard.is_safe_for_chaos.return_value = mock_result

        # 유효한 chaos 헤더, 하지만 리전 헤더 누락
        mock_global_request.user = MagicMock()

        with patch(
            "baldur_pro.services.chaos.safety_guard.get_resource_guard",
            return_value=mock_guard,
        ):
            response = mixin.check_chaos_permission(mock_global_request)

        assert response is not None
        assert response.status_code == 403
        assert response.data["error"] == "missing_region_header"

    @patch.dict(
        os.environ,
        {
            "BALDUR_NAMESPACE_REGION": "seoul",
            "CHAOS_ENABLED": "true",
            "ENVIRONMENT": "staging",
        },
        clear=False,
    )
    def test_check_chaos_permission_cross_region_denied(
        self, mixin, mock_global_request
    ):
        """check_chaos_permission이 cross-region 요청 거부."""
        pytest.importorskip("baldur_pro")
        # Resource guard가 safe 상태를 반환하도록 mock
        mock_guard = MagicMock()
        mock_result = MagicMock()
        mock_result.is_safe = True
        mock_guard.is_safe_for_chaos.return_value = mock_result

        mock_global_request.headers["X-Region"] = "tokyo"
        mock_global_request.user = MagicMock()

        with patch(
            "baldur_pro.services.chaos.safety_guard.get_resource_guard",
            return_value=mock_guard,
        ):
            response = mixin.check_chaos_permission(mock_global_request)

        assert response is not None
        assert response.status_code == 403
        assert response.data["error"] == "cross_region_xtest_denied"

    @patch.dict(
        os.environ,
        {
            "BALDUR_NAMESPACE_REGION": "seoul",
            "CHAOS_ENABLED": "true",
            "ENVIRONMENT": "staging",
        },
        clear=False,
    )
    def test_check_chaos_permission_same_region_allowed(
        self, mixin, mock_global_request
    ):
        """check_chaos_permission이 동일 리전 요청 허용."""
        pytest.importorskip("baldur_pro")
        # Resource guard가 safe 상태를 반환하도록 mock
        mock_guard = MagicMock()
        mock_result = MagicMock()
        mock_result.is_safe = True
        mock_guard.is_safe_for_chaos.return_value = mock_result

        mock_global_request.headers["X-Region"] = "seoul"
        mock_global_request.user = MagicMock()

        with patch(
            "baldur_pro.services.chaos.safety_guard.get_resource_guard",
            return_value=mock_guard,
        ):
            response = mixin.check_chaos_permission(mock_global_request)

        assert response is None

    @patch.dict(
        os.environ,
        {
            "BALDUR_NAMESPACE_REGION": "seoul",
            "CHAOS_ENABLED": "true",
            "ENVIRONMENT": "staging",
        },
        clear=False,
    )
    def test_check_chaos_permission_local_scope_no_region_header(
        self, mixin, mock_local_request
    ):
        """check_chaos_permission이 LOCAL scope API에서 리전 헤더 불필요."""
        pytest.importorskip("baldur_pro")
        # Resource guard가 safe 상태를 반환하도록 mock
        mock_guard = MagicMock()
        mock_result = MagicMock()
        mock_result.is_safe = True
        mock_guard.is_safe_for_chaos.return_value = mock_result

        mock_local_request.user = MagicMock()

        with patch(
            "baldur_pro.services.chaos.safety_guard.get_resource_guard",
            return_value=mock_guard,
        ):
            response = mixin.check_chaos_permission(mock_local_request)

        assert response is None


class TestGlobalScopeEndpointPatterns:
    """GLOBAL_SCOPE_ENDPOINT_PATTERNS 상수 테스트."""

    def test_patterns_exported(self):
        """GLOBAL_SCOPE_ENDPOINT_PATTERNS가 export됨."""
        from baldur.api.django.views.xtest import GLOBAL_SCOPE_ENDPOINT_PATTERNS

        assert isinstance(GLOBAL_SCOPE_ENDPOINT_PATTERNS, list)
        assert len(GLOBAL_SCOPE_ENDPOINT_PATTERNS) == 3

    def test_patterns_contain_expected_endpoints(self):
        """예상되는 GLOBAL scope 엔드포인트 패턴 포함."""
        from baldur.api.django.views.xtest import GLOBAL_SCOPE_ENDPOINT_PATTERNS

        patterns_str = " ".join(GLOBAL_SCOPE_ENDPOINT_PATTERNS)

        assert "emergency" in patterns_str
        assert "isolation" in patterns_str
        assert "governance" in patterns_str
