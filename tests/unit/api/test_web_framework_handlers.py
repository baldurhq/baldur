"""Unit tests for framework-agnostic handlers (api/handlers/).

Pure functions (RequestContext → ResponseContext) with no Django/DRF dependencies.

Verification techniques:
- Behavior: handler return values, status codes, state transitions
- Dependency interaction: mock service layer calls
- Edge cases: ImportError fallbacks, missing resources
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from baldur.interfaces.web_framework import (
    HttpMethod,
    RequestContext,
    ResponseContext,
)


def _make_ctx(method="GET", path="/test/", query=None, path_params=None):
    """Create a RequestContext for handler testing."""
    return RequestContext(
        method=HttpMethod(method),
        path=path,
        query_params=query or {},
        path_params=path_params or {},
    )


# =============================================================================
# Health Handlers
# =============================================================================


class TestLivenessCheckBehavior:
    """liveness_check() handler behavior."""

    def test_returns_200_with_alive_status(self):
        """Always returns 200 with status='alive'."""
        from baldur.api.handlers.health import liveness_check

        ctx = _make_ctx()
        resp = liveness_check(ctx)
        assert resp.status_code == 200
        assert resp.body == {"status": "alive"}

    def test_returns_response_context_type(self):
        """Return type is ResponseContext."""
        from baldur.api.handlers.health import liveness_check

        resp = liveness_check(_make_ctx())
        assert isinstance(resp, ResponseContext)


class TestReadinessCheckBehavior:
    """readiness_check() handler behavior."""

    def test_returns_200_when_ready(self):
        """Returns 200 when service reports ready."""
        from baldur.api.handlers.health import readiness_check

        mock_readiness = MagicMock()
        mock_readiness.is_ready = True
        mock_readiness.to_dict.return_value = {
            "status": "ready",
            "is_ready": True,
        }

        mock_service = MagicMock()
        mock_service.get_readiness.return_value = mock_readiness

        with patch(
            "baldur.services.health_check.get_health_check_service",
            return_value=mock_service,
        ):
            resp = readiness_check(_make_ctx())

        assert resp.status_code == 200

    def test_returns_503_when_not_ready(self):
        """Returns 503 when service reports not ready."""
        from baldur.api.handlers.health import readiness_check

        mock_readiness = MagicMock()
        mock_readiness.is_ready = False
        mock_readiness.to_dict.return_value = {
            "status": "not_ready",
            "is_ready": False,
        }

        mock_service = MagicMock()
        mock_service.get_readiness.return_value = mock_readiness

        with patch(
            "baldur.services.health_check.get_health_check_service",
            return_value=mock_service,
        ):
            resp = readiness_check(_make_ctx())

        assert resp.status_code == 503

    def test_removes_is_ready_from_response(self):
        """is_ready internal field is removed from response body."""
        from baldur.api.handlers.health import readiness_check

        mock_readiness = MagicMock()
        mock_readiness.is_ready = True
        mock_readiness.to_dict.return_value = {
            "status": "ready",
            "is_ready": True,
        }

        mock_service = MagicMock()
        mock_service.get_readiness.return_value = mock_readiness

        with patch(
            "baldur.services.health_check.get_health_check_service",
            return_value=mock_service,
        ):
            resp = readiness_check(_make_ctx())

        assert "is_ready" not in resp.body


class TestHealthCheckBehavior:
    """health_check() handler behavior."""

    def test_uses_cache_by_default(self):
        """Calls get_cached_health when no nocache param."""
        from baldur.api.handlers.health import health_check

        with (
            patch(
                "baldur.services.precomputed_cache.get_cached_health",
                return_value={"status": "healthy"},
            ) as mock_cached,
            patch(
                "baldur.services.precomputed_cache.compute_health_status",
            ) as mock_compute,
        ):
            resp = health_check(_make_ctx())

        mock_cached.assert_called_once()
        mock_compute.assert_not_called()
        assert resp.status_code == 200

    def test_bypasses_cache_when_nocache_true(self):
        """Calls compute_health_status when nocache=true."""
        from baldur.api.handlers.health import health_check

        with (
            patch(
                "baldur.services.precomputed_cache.get_cached_health",
            ) as mock_cached,
            patch(
                "baldur.services.precomputed_cache.compute_health_status",
                return_value={"status": "healthy"},
            ) as mock_compute,
        ):
            ctx = _make_ctx(query={"nocache": "true"})
            resp = health_check(ctx)

        mock_cached.assert_not_called()
        mock_compute.assert_called_once()
        assert resp.body["_cache"] == {"hit": "BYPASSED"}

    def test_fallback_on_import_error(self):
        """Falls back to HealthCheckService when precomputed_cache unavailable."""
        from baldur.api.handlers.health import health_check

        mock_service = MagicMock()
        mock_health = MagicMock()
        mock_health.to_dict.return_value = {"status": "healthy"}
        mock_service.get_overall_health.return_value = mock_health

        with (
            patch.dict(
                "sys.modules",
                {"baldur.services.precomputed_cache": None},
            ),
            patch(
                "baldur.services.health_check.get_health_check_service",
                return_value=mock_service,
            ),
        ):
            resp = health_check(_make_ctx())

        assert resp.status_code == 200
        assert resp.body == {"status": "healthy"}


class TestHealthCheckHttpStatusMappingBehavior:
    """473 D6: cascade status → HTTP code mapping on both code paths.

    Plan §329 contract:
    - "healthy"     → 200
    - "degraded"    → 200 (watchdog dampening keeps cluster in pool)
    - "unhealthy"   → 503 (DB severed; LB depool)
    - "error"       → 503 (compute_health_status() exception path)
    - "unavailable" → 503 (multi-tier cache CB-OPEN static fallback)
    - unknown       → 200 + WARNING log (avoid silent depool of healthy cluster)
    """

    def _run_cache_path(self, status_value):
        from baldur.api.handlers.health import health_check

        body = {"status": status_value}
        with (
            patch(
                "baldur.services.precomputed_cache.get_cached_health",
                return_value=body,
            ),
            patch("baldur.services.precomputed_cache.compute_health_status"),
        ):
            return health_check(_make_ctx())

    def _run_fallback_path(self, status_value):
        from baldur.api.handlers.health import health_check

        mock_service = MagicMock()
        mock_health = MagicMock()
        mock_health.to_dict.return_value = {"status": status_value}
        mock_service.get_overall_health.return_value = mock_health

        with (
            patch.dict(
                "sys.modules",
                {"baldur.services.precomputed_cache": None},
            ),
            patch(
                "baldur.services.health_check.get_health_check_service",
                return_value=mock_service,
            ),
        ):
            return health_check(_make_ctx())

    @pytest.mark.parametrize(
        ("status_value", "expected_code"),
        [
            ("healthy", 200),
            ("degraded", 200),
            ("unhealthy", 503),
            ("error", 503),
            ("unavailable", 503),
        ],
    )
    def test_cache_path_status_to_http(self, status_value, expected_code):
        resp = self._run_cache_path(status_value)
        assert resp.status_code == expected_code
        assert resp.body["status"] == status_value

    @pytest.mark.parametrize(
        ("status_value", "expected_code"),
        [
            ("healthy", 200),
            ("degraded", 200),
            ("unhealthy", 503),
            ("error", 503),
            ("unavailable", 503),
        ],
    )
    def test_fallback_path_status_to_http(self, status_value, expected_code):
        resp = self._run_fallback_path(status_value)
        assert resp.status_code == expected_code
        assert resp.body["status"] == status_value

    def test_unknown_status_defaults_to_200_and_logs_warning(self, caplog):
        """Unknown cascade status → 200 (don't depool healthy cluster) + WARNING."""
        with caplog.at_level("WARNING"):
            resp = self._run_cache_path("mystery")

        assert resp.status_code == 200
        assert any(
            "health_check.unknown_status_emitted" in r.message for r in caplog.records
        )

    def test_fallback_path_unknown_status_defaults_to_200_and_logs_warning(
        self, caplog
    ):
        """ImportError path also defaults unknown status to 200 + WARNING."""
        with caplog.at_level("WARNING"):
            resp = self._run_fallback_path("mystery")

        assert resp.status_code == 200
        assert any(
            "health_check.unknown_status_emitted" in r.message for r in caplog.records
        )


# =============================================================================
# Bulkhead Handler
# =============================================================================


class TestBulkheadStatusBehavior:
    """bulkhead_status() handler behavior."""

    @pytest.fixture(autouse=True)
    def _require_pro(self):
        pytest.importorskip("baldur_pro")

    def test_returns_all_bulkheads_without_filter(self):
        """Returns all bulkheads when no name filter."""
        from baldur.api.handlers.bulkhead import bulkhead_status

        mock_state = MagicMock()
        mock_state.bulkhead_type.value = "semaphore"
        mock_state.max_concurrent = 10
        mock_state.active_count = 3
        mock_state.waiting_count = 0
        mock_state.rejected_count = 0
        mock_state.available_permits = 7
        mock_state.utilization_percent = 30.0
        mock_state.last_rejection_time = None

        mock_registry = MagicMock()
        mock_registry.get_all_states.return_value = {"db": mock_state}

        with patch(
            "baldur_pro.services.bulkhead.get_bulkhead_registry",
            return_value=mock_registry,
        ):
            resp = bulkhead_status(_make_ctx())

        assert resp.status_code == 200
        assert "db" in resp.body["bulkheads"]
        assert "summary" in resp.body

    def test_returns_404_for_unknown_bulkhead_name(self):
        """Returns 404 when name filter matches nothing."""
        from baldur.api.handlers.bulkhead import bulkhead_status

        mock_registry = MagicMock()
        mock_registry.get_all_states.return_value = {}

        with patch(
            "baldur_pro.services.bulkhead.get_bulkhead_registry",
            return_value=mock_registry,
        ):
            ctx = _make_ctx(query={"name": "nonexistent"})
            resp = bulkhead_status(ctx)

        assert resp.status_code == 404


# =============================================================================
# Throttle Handler
# =============================================================================


def _throttle_stats_stub() -> dict:
    """Realistic AdaptiveThrottle.get_stats() payload (nested maps, JSON-safe)."""
    return {
        "current_limit": 100,
        "min_limit": 10,
        "max_limit": 1000,
        "total_requests": 5000,
        "allowed_requests": 4800,
        "rejected_requests": 200,
        "active_keys": 12,
        "gradient": 0.0,
        "adaptive": True,
        "emergency": {"active": False, "level": 0, "full_stop_active": False},
        "governance": {"kill_switch_active": False, "break_glass_active": False},
        "recovery": {"dampening_active": False, "dampening_step": 0},
    }


class TestThrottleStatusBehavior:
    """throttle_status() handler behavior (live v1.0 throttle state).

    Mock point per Testability Notes: patch the ``adaptive_throttle`` provider
    slot's ``safe_get`` directly (leak-immune against the monorepo PRO
    ProviderRegistry registration leak), not the underlying PRO singleton getter.
    """

    def test_returns_200_with_live_stats_when_provider_present(self):
        """Provider registered -> 200 carrying get_stats() fields + a stamped timestamp."""
        from baldur.api.handlers.throttle import throttle_status
        from baldur.factory.registry import ProviderRegistry

        stub = MagicMock()
        stub.get_stats.return_value = _throttle_stats_stub()

        with patch.object(
            ProviderRegistry.adaptive_throttle, "safe_get", return_value=stub
        ):
            resp = throttle_status(_make_ctx())

        assert resp.status_code == 200
        # Live throttle state is surfaced (not the auto-tuner enabled/strategy shape)
        for field in (
            "current_limit",
            "rejected_requests",
            "emergency",
            "governance",
            "recovery",
        ):
            assert field in resp.body
        # Handler stamps a freshness timestamp (sibling parity)
        assert "timestamp" in resp.body
        stub.get_stats.assert_called_once_with()

    def test_returns_404_when_provider_absent(self):
        """Provider absent (OSS-only / PRO missing) -> 404, no get_stats call."""
        from baldur.api.handlers.throttle import throttle_status
        from baldur.factory.registry import ProviderRegistry

        with patch.object(
            ProviderRegistry.adaptive_throttle, "safe_get", return_value=None
        ):
            resp = throttle_status(_make_ctx())

        assert resp.status_code == 404


# =============================================================================
# Audit Handlers
# =============================================================================


class TestAuditHealthBehavior:
    """audit_health() handler behavior (416 D8: backend table → adapter name)."""

    def test_healthy_status_when_no_issues(self):
        """Returns 'healthy' when no degraded mode and no open circuits."""
        from baldur.api.handlers.audit import audit_health

        mock_manager = MagicMock()
        mock_manager.get_status.return_value = {"degraded": False}
        mock_registry = MagicMock()
        mock_registry.get_all_stats.return_value = {"db": {}}
        mock_registry.get_open_circuits.return_value = []

        with (
            patch(
                "baldur.audit.get_degraded_mode_manager",
                return_value=mock_manager,
            ),
            patch(
                "baldur.audit.resilience.CircuitBreakerRegistry.get_instance",
                return_value=mock_registry,
            ),
            patch(
                "baldur.factory.ProviderRegistry.audit.get_default_name",
                return_value="null",
            ),
        ):
            resp = audit_health(_make_ctx())

        assert resp.status_code == 200
        assert resp.body["status"] == "healthy"
        assert resp.body["backend"] == {"adapter": "null"}

    def test_degraded_status_when_degraded_mode_active(self):
        """Returns 'degraded' when degraded mode is active."""
        from baldur.api.handlers.audit import audit_health

        mock_manager = MagicMock()
        mock_manager.get_status.return_value = {"degraded": True}
        mock_registry = MagicMock()
        mock_registry.get_all_stats.return_value = {}
        mock_registry.get_open_circuits.return_value = []

        with (
            patch(
                "baldur.audit.get_degraded_mode_manager",
                return_value=mock_manager,
            ),
            patch(
                "baldur.audit.resilience.CircuitBreakerRegistry.get_instance",
                return_value=mock_registry,
            ),
        ):
            resp = audit_health(_make_ctx())

        assert resp.body["status"] == "degraded"

    def test_warning_status_when_circuits_open(self):
        """Returns 'warning' when circuits are open but not degraded."""
        from baldur.api.handlers.audit import audit_health

        mock_manager = MagicMock()
        mock_manager.get_status.return_value = {"degraded": False}
        mock_registry = MagicMock()
        mock_registry.get_all_stats.return_value = {"db": {}}
        mock_registry.get_open_circuits.return_value = ["db"]

        with (
            patch(
                "baldur.audit.get_degraded_mode_manager",
                return_value=mock_manager,
            ),
            patch(
                "baldur.audit.resilience.CircuitBreakerRegistry.get_instance",
                return_value=mock_registry,
            ),
        ):
            resp = audit_health(_make_ctx())

        assert resp.body["status"] == "warning"
        assert resp.body["circuit_breakers"]["open_count"] == 1

    def test_returns_500_on_exception(self):
        """Returns 500 when an upstream dependency raises exception."""
        from baldur.api.handlers.audit import audit_health

        with patch(
            "baldur.audit.get_degraded_mode_manager",
            side_effect=RuntimeError("connection failed"),
        ):
            resp = audit_health(_make_ctx())

        assert resp.status_code == 500
        assert "connection failed" in resp.body["error"]


class TestCircuitBreakerStatusBehavior:
    """circuit_breaker_status() handler behavior."""

    def test_list_returns_all_circuit_breakers(self):
        """GET without name returns list of all circuit breakers."""
        from baldur.api.handlers.audit import circuit_breaker_status

        mock_registry = MagicMock()
        mock_registry.get_all_stats.return_value = {"db": {"state": "closed"}}
        mock_registry.get_open_circuits.return_value = []

        with patch(
            "baldur.audit.resilience.CircuitBreakerRegistry.get_instance",
            return_value=mock_registry,
        ):
            resp = circuit_breaker_status(_make_ctx())

        assert resp.status_code == 200
        assert "circuit_breakers" in resp.body
        assert "open_circuits" in resp.body

    def test_detail_returns_single_cb(self):
        """GET with name in path_params returns single CB stats."""
        from baldur.api.handlers.audit import circuit_breaker_status

        mock_cb = MagicMock()
        mock_cb.get_stats.return_value = {"state": "closed", "failures": 0}
        mock_registry = MagicMock()
        mock_registry.get.return_value = mock_cb

        with patch(
            "baldur.audit.resilience.CircuitBreakerRegistry.get_instance",
            return_value=mock_registry,
        ):
            ctx = _make_ctx(path_params={"name": "db"})
            resp = circuit_breaker_status(ctx)

        assert resp.status_code == 200
        assert resp.body["state"] == "closed"

    def test_detail_returns_404_for_unknown_name(self):
        """GET with unknown name returns 404."""
        from baldur.api.handlers.audit import circuit_breaker_status

        mock_registry = MagicMock()
        mock_registry.get.return_value = None

        with patch(
            "baldur.audit.resilience.CircuitBreakerRegistry.get_instance",
            return_value=mock_registry,
        ):
            ctx = _make_ctx(path_params={"name": "unknown"})
            resp = circuit_breaker_status(ctx)

        assert resp.status_code == 404


# =============================================================================
# Dashboard Handler
# =============================================================================


class TestDashboardSummaryBehavior:
    """dashboard_summary() handler behavior."""

    def test_delegates_to_dashboard_service(self):
        """Calls DashboardService.get_summary() and returns its dict."""
        from baldur.api.handlers.dashboard import dashboard_summary

        mock_summary = MagicMock()
        mock_summary.to_dict.return_value = {"total": 42, "healthy": 40}
        mock_service = MagicMock()
        mock_service.get_summary.return_value = mock_summary

        with patch(
            "baldur.services.dashboard_service.get_dashboard_service",
            return_value=mock_service,
        ):
            resp = dashboard_summary(_make_ctx())

        assert resp.status_code == 200
        assert resp.body == {"total": 42, "healthy": 40}
        mock_service.get_summary.assert_called_once()
