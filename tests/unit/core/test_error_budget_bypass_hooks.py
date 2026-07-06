"""
Error Budget Bypass Hooks 단위 테스트.

테스트 대상:
1. _error_budget_admin_bypass() - Admin API 경로 바이패스
2. _error_budget_critical_path_bypass() - 크리티컬 경로 바이패스
3. register_error_budget_bypass_hooks() - 바이패스 훅 등록
"""

from baldur.core.hooks import (
    BypassRegistry,
    _error_budget_admin_bypass,
    _error_budget_critical_path_bypass,
    register_error_budget_bypass_hooks,
)


class MockHttpRequest:
    """테스트용 HttpRequest Mock."""

    def __init__(self, path: str = "/", headers: dict | None = None):
        self.path = path
        self.headers = headers or {}


class TestErrorBudgetAdminBypass:
    """_error_budget_admin_bypass() 테스트."""

    def test_admin_path_returns_true(self):
        """Admin 경로 시 True 반환."""
        request = MockHttpRequest(path="/admin/dashboard")
        assert _error_budget_admin_bypass(request) is True

    def test_api_admin_path_returns_true(self):
        """API Admin 경로 시 True 반환."""
        request = MockHttpRequest(path="/api/admin/users")
        assert _error_budget_admin_bypass(request) is True

    def test_internal_admin_path_returns_true(self):
        """Internal Admin 경로 시 True 반환."""
        request = MockHttpRequest(path="/_admin/metrics")
        assert _error_budget_admin_bypass(request) is True

    def test_normal_path_returns_false(self):
        """일반 경로 시 False 반환."""
        request = MockHttpRequest(path="/api/v1/products")
        assert _error_budget_admin_bypass(request) is False

    def test_admin_in_middle_returns_false(self):
        """경로 중간에 admin이 있는 경우 False 반환."""
        request = MockHttpRequest(path="/api/v1/admin-panel")
        assert _error_budget_admin_bypass(request) is False

    def test_case_insensitive(self):
        """대소문자 무관하게 처리."""
        request = MockHttpRequest(path="/ADMIN/dashboard")
        assert _error_budget_admin_bypass(request) is True


class TestErrorBudgetCriticalPathBypass:
    """_error_budget_critical_path_bypass() 테스트."""

    def test_health_path_returns_true(self):
        """헬스체크 경로 시 True 반환."""
        request = MockHttpRequest(path="/health")
        assert _error_budget_critical_path_bypass(request) is True

    def test_healthz_path_returns_true(self):
        """Kubernetes healthz 경로 시 True 반환."""
        request = MockHttpRequest(path="/healthz")
        assert _error_budget_critical_path_bypass(request) is True

    def test_ready_path_returns_true(self):
        """readiness 경로 시 True 반환."""
        request = MockHttpRequest(path="/ready")
        assert _error_budget_critical_path_bypass(request) is True

    def test_readyz_path_returns_true(self):
        """Kubernetes readyz 경로 시 True 반환."""
        request = MockHttpRequest(path="/readyz")
        assert _error_budget_critical_path_bypass(request) is True

    def test_live_path_returns_true(self):
        """liveness 경로 시 True 반환."""
        request = MockHttpRequest(path="/live")
        assert _error_budget_critical_path_bypass(request) is True

    def test_livez_path_returns_true(self):
        """Kubernetes livez 경로 시 True 반환."""
        request = MockHttpRequest(path="/livez")
        assert _error_budget_critical_path_bypass(request) is True

    def test_internal_path_returns_true(self):
        """Internal 경로 시 True 반환."""
        request = MockHttpRequest(path="/_internal/status")
        assert _error_budget_critical_path_bypass(request) is True

    def test_ping_path_returns_true(self):
        """Ping 경로 시 True 반환."""
        request = MockHttpRequest(path="/api/v1/ping")
        assert _error_budget_critical_path_bypass(request) is True

    def test_critical_header_returns_true(self):
        """X-Critical-Path 헤더 시 True 반환."""
        request = MockHttpRequest(
            path="/api/v1/products",
            headers={"X-Critical-Path": "true"},
        )
        assert _error_budget_critical_path_bypass(request) is True

    def test_critical_header_case_insensitive(self):
        """X-Critical-Path 헤더 대소문자 무관."""
        request = MockHttpRequest(
            path="/api/v1/products",
            headers={"X-Critical-Path": "TRUE"},
        )
        assert _error_budget_critical_path_bypass(request) is True

    def test_normal_path_returns_false(self):
        """일반 경로 시 False 반환."""
        request = MockHttpRequest(path="/api/v1/products")
        assert _error_budget_critical_path_bypass(request) is False

    def test_health_in_middle_returns_false(self):
        """경로 중간에 health가 있는 경우 False 반환."""
        request = MockHttpRequest(path="/api/v1/health-data")
        assert _error_budget_critical_path_bypass(request) is False


class TestRegisterErrorBudgetBypassHooks:
    """register_error_budget_bypass_hooks() 테스트."""

    def setup_method(self):
        # 기존 훅 제거
        BypassRegistry.clear_all()

    def teardown_method(self):
        BypassRegistry.clear_all()

    def test_registers_admin_bypass_hook(self):
        """Admin 바이패스 훅 등록 확인."""
        register_error_budget_bypass_hooks()

        stats = BypassRegistry.get_statistics()
        hook_names = [h["name"] for h in stats["hooks"]]

        assert "error_budget_admin" in hook_names

    def test_registers_critical_path_bypass_hook(self):
        """Critical Path 바이패스 훅 등록 확인."""
        register_error_budget_bypass_hooks()

        stats = BypassRegistry.get_statistics()
        hook_names = [h["name"] for h in stats["hooks"]]

        assert "error_budget_critical_path" in hook_names

    def test_admin_bypass_has_high_priority(self):
        """Admin 바이패스 우선순위가 950."""
        register_error_budget_bypass_hooks()

        stats = BypassRegistry.get_statistics()
        admin_hook = next(
            h for h in stats["hooks"] if h["name"] == "error_budget_admin"
        )

        assert admin_hook["priority"] == 950

    def test_critical_path_bypass_has_high_priority(self):
        """Critical Path 바이패스 우선순위가 900."""
        register_error_budget_bypass_hooks()

        stats = BypassRegistry.get_statistics()
        critical_hook = next(
            h for h in stats["hooks"] if h["name"] == "error_budget_critical_path"
        )

        assert critical_hook["priority"] == 900


class TestBypassRegistryIntegration:
    """BypassRegistry 연동 통합 테스트."""

    def setup_method(self):
        BypassRegistry.clear_all()
        register_error_budget_bypass_hooks()

    def teardown_method(self):
        BypassRegistry.clear_all()

    def test_admin_request_bypasses(self):
        """Admin 요청이 should_bypass에서 True 반환."""
        request = MockHttpRequest(path="/admin/dashboard")

        result = BypassRegistry.should_bypass(request)

        assert result.bypassed is True
        assert "admin" in result.hook_name.lower()

    def test_health_request_bypasses(self):
        """Health 요청이 should_bypass에서 True 반환."""
        request = MockHttpRequest(path="/healthz")

        result = BypassRegistry.should_bypass(request)

        assert result.bypassed is True
        assert "critical_path" in result.hook_name.lower()

    def test_normal_request_does_not_bypass(self):
        """일반 요청은 바이패스 안됨."""
        request = MockHttpRequest(path="/api/v1/products")

        result = BypassRegistry.should_bypass(request)

        assert result.bypassed is False

    def test_critical_path_header_bypasses(self):
        """X-Critical-Path 헤더로 바이패스."""
        request = MockHttpRequest(
            path="/api/v1/products",
            headers={"X-Critical-Path": "true"},
        )

        result = BypassRegistry.should_bypass(request)

        assert result.bypassed is True
