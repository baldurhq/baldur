"""AdminRegistry Phase 2a unit tests — 429 PR3-phase2a.

Verification targets:
- All Phase 2a handlers are auto-registered on singleton init
- No duplicate (method, path) pairs across Phase 1 + Phase 2a
- Every registered route can be resolved by its exact (method, path)
- Path-parameter routes extract their params correctly
- Per-module sub-registers are individually fault-isolated (one module's
  ImportError does not drop other modules' routes)
"""

from __future__ import annotations

import importlib.util

import pytest

from baldur.api.admin.registry import (
    AdminRegistry,
    get_admin_registry,
    reset_admin_registry,
)
from baldur.api.admin.routes.circuit_breaker import (
    _register_circuit_breaker_control_routes,
)
from baldur.api.admin.routes.daily_report import _register_daily_report_routes
from baldur.api.admin.routes.dlq import _register_dlq_routes
from baldur.api.admin.routes.emergency import _register_emergency_routes
from baldur.api.admin.routes.health import _register_health_routes
from baldur.api.admin.routes.runtime_config import _register_config_routes
from baldur.api.admin.routes.system_control import _register_system_control_routes
from baldur.interfaces.web_framework import PermissionLevel


@pytest.fixture(autouse=True)
def _reset_registry():
    reset_admin_registry()
    yield
    reset_admin_registry()


# =============================================================================
# Contract — Phase 2a registration completeness
# =============================================================================


class TestPhase2aRegistrationContract:
    """Phase 2a paths are the admin API surface contract."""

    def test_system_control_routes_registered(self):
        """5 system control routes (status/enable/disable/dry-run enable/disable)."""
        reg = get_admin_registry()
        paths = {(r.method.value, r.path) for r in reg.all_routes()}
        expected = {
            ("GET", "/system/status"),
            ("POST", "/system/enable"),
            ("POST", "/system/disable"),
            ("POST", "/system/dry-run/enable"),
            ("POST", "/system/dry-run/disable"),
        }
        assert expected.issubset(paths)

    def test_daily_report_routes_registered(self):
        """3 daily report routes (list / trend / detail)."""
        reg = get_admin_registry()
        paths = {(r.method.value, r.path) for r in reg.all_routes()}
        expected = {
            ("GET", "/reports/daily"),
            ("GET", "/reports/daily/trend"),
            ("GET", "/reports/daily/{date}"),
        }
        assert expected.issubset(paths)

    def test_circuit_breaker_control_routes_registered(self):
        """7 CB control routes (action + status/status-by-name + audit + 3 quick)."""
        reg = get_admin_registry()
        paths = {(r.method.value, r.path) for r in reg.all_routes()}
        expected = {
            ("POST", "/control"),
            ("GET", "/control/status"),
            ("GET", "/control/status/{service_name}"),
            ("GET", "/control/audit"),
            ("POST", "/control/allow/{service_name}"),
            ("POST", "/control/block/{service_name}"),
            ("POST", "/control/reset/{service_name}"),
        }
        assert expected.issubset(paths)

    def test_dlq_routes_registered(self):
        """DLQ routes — the OSS-set (read + single-entry actions) always
        registers; the PRO-set (batch replay + management lifecycle) registers
        only when ``baldur_pro`` is installed (pure OSS → absent → 404)."""
        reg = get_admin_registry()
        paths = {(r.method.value, r.path) for r in reg.all_routes()}
        expected_oss = {
            ("GET", "/dlq/cleanup/stats"),
            ("GET", "/dlq/list"),
            ("GET", "/dlq/facets"),
            ("GET", "/dlq/{pk}"),
            ("POST", "/dlq/{pk}/retry"),
            ("POST", "/dlq/{pk}/resolve"),
            ("POST", "/dlq/{pk}/force-redrive"),
        }
        expected_pro = {
            ("POST", "/dlq/replay"),
            ("POST", "/dlq/cleanup/archive"),
            ("POST", "/dlq/cleanup/purge"),
            ("POST", "/dlq/test/create"),
        }
        assert expected_oss.issubset(paths)
        if importlib.util.find_spec("baldur_pro") is not None:
            assert expected_pro.issubset(paths)
        else:
            assert not (expected_pro & paths)

    def test_emergency_routes_registered(self):
        """9 emergency routes (status + trigger + release + recovery + history
        + config GET/PUT + levels)."""
        reg = get_admin_registry()
        paths = {(r.method.value, r.path) for r in reg.all_routes()}
        expected = {
            ("GET", "/emergency/status"),
            ("POST", "/emergency/trigger"),
            ("POST", "/emergency/release"),
            ("POST", "/emergency/gradual-recovery"),
            ("POST", "/emergency/stop-recovery"),
            ("GET", "/emergency/history"),
            ("GET", "/emergency/config"),
            ("PUT", "/emergency/config"),
            ("GET", "/emergency/levels"),
        }
        assert expected.issubset(paths)

    def test_config_admin_routes_registered(self):
        """4 config admin routes (all / reset / pending / cancel)."""
        reg = get_admin_registry()
        paths = {(r.method.value, r.path) for r in reg.all_routes()}
        expected = {
            ("GET", "/config"),
            ("POST", "/config/reset"),
            ("GET", "/config/pending"),
            ("POST", "/config/pending/{pending_id}/cancel"),
        }
        assert expected.issubset(paths)

    def test_config_section_routes_have_get_and_put_pairs(self):
        """Each config section has both GET (viewer+) and PUT (admin)."""
        reg = get_admin_registry()
        paths = {(r.method.value, r.path) for r in reg.all_routes()}
        sections = (
            "circuit-breaker",
            "dlq",
            "retry",
            "sla",
            "rate-limit",
            "security",
            "idempotency",
            "notification",
            "forensic",
            "metrics",
            "error-budget",
            "replay-automation",
            "logging",
        )
        for section in sections:
            assert ("GET", f"/config/{section}") in paths, (
                f"missing GET /config/{section}"
            )
            assert ("PUT", f"/config/{section}") in paths, (
                f"missing PUT /config/{section}"
            )

    def test_slo_routes_include_delete(self):
        """SLO supports DELETE for name-based removal."""
        reg = get_admin_registry()
        paths = {(r.method.value, r.path) for r in reg.all_routes()}
        assert ("GET", "/config/slo") in paths
        assert ("PUT", "/config/slo") in paths
        assert ("DELETE", "/config/slo") in paths

    def test_health_phase2a_extras_registered(self):
        """Phase 2a adds pool health + ping + metrics + prometheus + gate routes."""
        reg = get_admin_registry()
        paths = {(r.method.value, r.path) for r in reg.all_routes()}
        expected = {
            ("GET", "/health/pool"),
            ("GET", "/health/ping"),
            ("GET", "/metrics"),
            ("GET", "/prometheus"),
            ("GET", "/health/gate"),
            ("GET", "/config/gate"),
            ("PUT", "/config/gate"),
            ("POST", "/gate/reset"),
        }
        assert expected.issubset(paths)


# =============================================================================
# Behavior — registry integrity (no duplicates, all resolvable)
# =============================================================================


class TestRegistryIntegrityBehavior:
    """Phase 1 + Phase 2a must form a consistent routing table."""

    def test_no_duplicate_method_path_pairs(self):
        """Each (method, path) pair occurs exactly once in the registry."""
        reg = get_admin_registry()
        routes = reg.all_routes()
        pairs = [(r.method.value, r.path) for r in routes]
        # If there are duplicates, the registry kept only the last registration
        # (per .register()'s replace-on-duplicate semantics). We still assert
        # the invariant that all_routes() never contains duplicates.
        assert len(pairs) == len(set(pairs))

    def test_every_literal_route_is_resolvable(self):
        """Every route without path params resolves via its exact path."""
        reg = get_admin_registry()
        for route in reg.all_routes():
            if "{" in route.path:
                continue
            result = reg.resolve(route.method.value, route.path)
            assert result is not None, (
                f"route {route.method.value} {route.path} cannot be resolved"
            )

    def test_cb_service_status_path_param_resolves(self):
        reg = get_admin_registry()
        result = reg.resolve("GET", "/control/status/payment")
        assert result is not None
        route, params = result
        assert route.permission_level == PermissionLevel.VIEWER
        assert params == {"service_name": "payment"}

    def test_dlq_pk_path_param_resolves(self):
        reg = get_admin_registry()
        result = reg.resolve("GET", "/dlq/42")
        assert result is not None
        route, params = result
        assert params == {"pk": "42"}

    def test_daily_report_date_path_param_resolves(self):
        reg = get_admin_registry()
        result = reg.resolve("GET", "/reports/daily/2026-04-15")
        assert result is not None
        route, params = result
        assert params == {"date": "2026-04-15"}

    def test_cancel_pending_change_param_resolves(self):
        reg = get_admin_registry()
        result = reg.resolve("POST", "/config/pending/abc123/cancel")
        assert result is not None
        _, params = result
        assert params == {"pending_id": "abc123"}


# =============================================================================
# Behavior — per-module fault isolation
# =============================================================================


class TestPerModuleFaultIsolationBehavior:
    """Each sub-register is wrapped in try/except so a missing import in
    one module does not drop other modules' routes."""

    def test_sub_register_is_idempotent(self):
        """Re-invoking a sub-register on the same registry does not add
        duplicate routes (replace semantics from AdminRegistry.register)."""
        reg = AdminRegistry()
        _register_daily_report_routes(reg)
        first_count = len(reg.all_routes())
        _register_daily_report_routes(reg)
        second_count = len(reg.all_routes())
        assert first_count == second_count

    def test_sub_register_runs_without_full_bootstrap(self):
        """Each sub-register can operate on an empty AdminRegistry without
        requiring Phase 1 to have run first."""
        for sub in (
            _register_health_routes,
            _register_system_control_routes,
            _register_daily_report_routes,
            _register_circuit_breaker_control_routes,
            _register_dlq_routes,
            _register_emergency_routes,
            _register_config_routes,
        ):
            reg = AdminRegistry()
            sub(reg)
            # Every module should contribute at least one route
            assert len(reg.all_routes()) >= 1, f"{sub.__name__} registered zero routes"


# =============================================================================
# Behavior — total route count sanity
# =============================================================================


class TestTotalRouteCountBehavior:
    """Phase 1 (8) + Phase 2a (~70) should all be present."""

    def test_total_route_count_meets_minimum(self):
        """Combined registration yields at least 70 routes (Phase 1 + 2a)."""
        reg = get_admin_registry()
        assert len(reg.all_routes()) >= 70

    def test_phase1_liveness_still_resolvable_after_phase2a(self):
        """Phase 2a registration must not shadow or remove Phase 1 routes."""
        reg = get_admin_registry()
        result = reg.resolve("GET", "/liveness")
        assert result is not None
        route, _ = result
        assert route.permission_level == PermissionLevel.PUBLIC
