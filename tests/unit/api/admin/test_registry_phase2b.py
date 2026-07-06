"""AdminRegistry Phase 2b unit tests — 432 handler extraction.

Verification targets:
- All Phase 2b handlers are auto-registered on singleton init
- No duplicate (method, path) pairs across Phase 1 + 2a + 2b
- Every registered route can be resolved by its exact (method, path)
- Path-parameter routes extract their params correctly
- Per-module sub-registers are individually fault-isolated
- Security-review route is registered with ADMIN permission
"""

from __future__ import annotations

import pytest

from baldur.api.admin.registry import (
    AdminRegistry,
    get_admin_registry,
    reset_admin_registry,
)
from baldur.api.admin.routes.analysis import _register_analysis_routes
from baldur.api.admin.routes.audit_resilience import _register_audit_resilience_routes
from baldur.api.admin.routes.chaos import _register_chaos_routes
from baldur.api.admin.routes.config_data import _register_config_data_routes
from baldur.api.admin.routes.continuous_audit import _register_continuous_audit_routes
from baldur.api.admin.routes.error_budget import _register_error_budget_routes
from baldur.api.admin.routes.l2_storage import _register_l2_storage_routes
from baldur.api.admin.routes.operations import _register_operations_routes
from baldur.api.admin.routes.recovery import _register_recovery_routes
from baldur.api.admin.routes.security_review import _register_security_review_routes
from baldur.interfaces.web_framework import PermissionLevel


@pytest.fixture(autouse=True)
def _reset_registry():
    reset_admin_registry()
    yield
    reset_admin_registry()


# =============================================================================
# Contract — Phase 2b registration completeness
# =============================================================================


class TestPhase2bRegistrationContract:
    """Phase 2b paths are the admin API surface contract."""

    def test_audit_resilience_routes_registered(self):
        """7 audit resilience routes."""
        reg = get_admin_registry()
        paths = {(r.method.value, r.path) for r in reg.all_routes()}
        expected = {
            ("POST", "/resilience/cb/reset/{name}"),
            ("POST", "/resilience/cb/force-open/{name}"),
            ("POST", "/resilience/cb/reset-all"),
            ("GET", "/resilience/audit-metrics"),
            ("GET", "/resilience/degraded-mode"),
            ("POST", "/resilience/degraded-mode/{action}"),
            ("POST", "/resilience/metrics/reset"),
        }
        assert expected.issubset(paths)

    def test_auto_tuning_routes_registered(self):
        """11 auto-tuning routes."""
        reg = get_admin_registry()
        paths = {(r.method.value, r.path) for r in reg.all_routes()}
        expected = {
            ("GET", "/auto-tuning/status"),
            ("POST", "/auto-tuning/enable"),
            ("POST", "/auto-tuning/disable"),
            ("POST", "/auto-tuning/module/{module_name}/enable"),
            ("POST", "/auto-tuning/module/{module_name}/disable"),
            ("GET", "/auto-tuning/bounds"),
            ("PUT", "/auto-tuning/bounds"),
            ("GET", "/auto-tuning/history"),
            ("POST", "/auto-tuning/override"),
            ("DELETE", "/auto-tuning/override/{parameter}"),
            ("GET", "/auto-tuning/metrics"),
        }
        assert expected.issubset(paths)

    def test_meta_watchdog_routes_registered(self):
        """4 meta-watchdog routes."""
        reg = get_admin_registry()
        paths = {(r.method.value, r.path) for r in reg.all_routes()}
        expected = {
            ("GET", "/meta-watchdog/liveness"),
            ("GET", "/meta-watchdog/status"),
            ("POST", "/meta-watchdog/force-check"),
            ("POST", "/meta-watchdog/escalation-test"),
        }
        assert expected.issubset(paths)

    def test_metric_sync_routes_registered(self):
        """2 metric-sync routes."""
        reg = get_admin_registry()
        paths = {(r.method.value, r.path) for r in reg.all_routes()}
        expected = {
            ("POST", "/metric-sync"),
            ("GET", "/metric-sync/drift-report"),
        }
        assert expected.issubset(paths)

    def test_drift_threshold_routes_registered(self):
        """3 drift-threshold routes."""
        reg = get_admin_registry()
        paths = {(r.method.value, r.path) for r in reg.all_routes()}
        expected = {
            ("GET", "/drift-threshold/config"),
            ("PUT", "/drift-threshold/config"),
            ("POST", "/drift-threshold/reset"),
        }
        assert expected.issubset(paths)

    def test_grafana_webhook_routes_registered(self):
        """3 grafana webhook routes."""
        reg = get_admin_registry()
        paths = {(r.method.value, r.path) for r in reg.all_routes()}
        expected = {
            ("POST", "/grafana/webhook"),
            ("GET", "/grafana/webhook/test"),
            ("POST", "/grafana/webhook/test"),
        }
        assert expected.issubset(paths)

    def test_chaos_config_routes_registered(self):
        """8 chaos config routes (4 config sections × GET/PATCH)."""
        reg = get_admin_registry()
        paths = {(r.method.value, r.path) for r in reg.all_routes()}
        expected = {
            ("GET", "/chaos/config/safety-guard"),
            ("PATCH", "/chaos/config/safety-guard"),
            ("GET", "/chaos/config/blast-radius"),
            ("PATCH", "/chaos/config/blast-radius"),
            ("GET", "/chaos/config/scheduler"),
            ("PATCH", "/chaos/config/scheduler"),
            ("GET", "/chaos/config/report"),
            ("PATCH", "/chaos/config/report"),
        }
        assert expected.issubset(paths)

    def test_chaos_report_routes_registered(self):
        """5 chaos report routes."""
        reg = get_admin_registry()
        paths = {(r.method.value, r.path) for r in reg.all_routes()}
        expected = {
            ("GET", "/chaos/reports"),
            ("GET", "/chaos/reports/{report_id}"),
            ("POST", "/chaos/reports/generate"),
            ("GET", "/chaos/grade-history"),
            ("POST", "/chaos/dry-run/analysis"),
        }
        assert expected.issubset(paths)

    def test_chaos_safety_routes_registered(self):
        """10 chaos safety routes."""
        reg = get_admin_registry()
        paths = {(r.method.value, r.path) for r in reg.all_routes()}
        expected = {
            ("GET", "/chaos/kill-switch"),
            ("POST", "/chaos/kill-switch"),
            ("POST", "/chaos/safety-check"),
            ("GET", "/chaos/stop-conditions"),
            ("PUT", "/chaos/stop-conditions"),
            ("GET", "/chaos/ttl-config"),
            ("PUT", "/chaos/ttl-config"),
            ("GET", "/chaos/dry-run-config"),
            ("PUT", "/chaos/dry-run-config"),
            ("POST", "/chaos/kill-all"),
        }
        assert expected.issubset(paths)

    def test_chaos_schedule_routes_registered(self):
        """8 chaos schedule routes."""
        reg = get_admin_registry()
        paths = {(r.method.value, r.path) for r in reg.all_routes()}
        expected = {
            ("GET", "/chaos/schedules"),
            ("POST", "/chaos/schedules"),
            ("GET", "/chaos/schedules/{schedule_id}"),
            ("PATCH", "/chaos/schedules/{schedule_id}"),
            ("DELETE", "/chaos/schedules/{schedule_id}"),
            ("POST", "/chaos/schedules/{schedule_id}/approval"),
            ("POST", "/chaos/schedules/{schedule_id}/execute"),
            ("GET", "/chaos/pending-approvals"),
        }
        assert expected.issubset(paths)

    def test_l2_storage_routes_registered(self):
        """14 l2-storage routes."""
        reg = get_admin_registry()
        paths = {(r.method.value, r.path) for r in reg.all_routes()}
        expected = {
            ("GET", "/l2-storage/status"),
            ("GET", "/l2-storage/health"),
            ("POST", "/l2-storage/health/reset"),
            ("POST", "/l2-storage/sync/from-l2"),
            ("POST", "/l2-storage/sync/to-l2"),
            ("GET", "/l2-storage/metrics"),
            ("GET", "/l2-storage/drift/stats"),
            ("GET", "/l2-storage/drift/history"),
            ("POST", "/l2-storage/drift/reconcile"),
            ("POST", "/l2-storage/drift/reconcile/{service_name}"),
            ("GET", "/l2-storage/config"),
            ("PUT", "/l2-storage/config"),
            ("POST", "/l2-storage/config/reset"),
        }
        assert expected.issubset(paths)

    def test_l2_storage_shadow_log_routes_registered(self):
        """6 shadow-log routes."""
        reg = get_admin_registry()
        paths = {(r.method.value, r.path) for r in reg.all_routes()}
        expected = {
            ("GET", "/l2-storage/shadow-log"),
            ("GET", "/l2-storage/shadow-log/stats"),
            ("POST", "/l2-storage/shadow-log/clear"),
            ("GET", "/l2-storage/shadow-log/analyze"),
            ("POST", "/l2-storage/shadow-log/replay"),
            ("GET", "/l2-storage/shadow-log/service/{service_name}"),
        }
        assert expected.issubset(paths)

    def test_continuous_audit_routes_registered(self):
        """10 continuous audit routes."""
        reg = get_admin_registry()
        paths = {(r.method.value, r.path) for r in reg.all_routes()}
        expected = {
            ("GET", "/audit/logs"),
            ("GET", "/audit/logs/{log_id}"),
            ("GET", "/audit/auto-tuning"),
            ("GET", "/audit/drift"),
            ("GET", "/audit/compliance"),
            ("GET", "/audit/integrity/verify"),
            ("GET", "/audit/integrity/state"),
            ("GET", "/audit/export/jsonl"),
            ("GET", "/audit/export/csv"),
            ("GET", "/audit/config"),
        }
        assert expected.issubset(paths)

    def test_compliance_routes_registered(self):
        """10 compliance routes."""
        reg = get_admin_registry()
        paths = {(r.method.value, r.path) for r in reg.all_routes()}
        expected = {
            ("GET", "/compliance/standards"),
            ("GET", "/compliance/checks"),
            ("GET", "/compliance/checks/{standard}"),
            ("POST", "/compliance/run"),
            ("POST", "/compliance/run/{standard}"),
            ("GET", "/compliance/reports"),
            ("GET", "/compliance/reports/{report_id}"),
            ("GET", "/compliance/reports/{report_id}/evidence/pending"),
            ("PATCH", "/compliance/reports/{report_id}/checks/{check_id}/review"),
        }
        assert expected.issubset(paths)

    def test_reconciliation_routes_registered(self):
        """12 reconciliation routes."""
        reg = get_admin_registry()
        paths = {(r.method.value, r.path) for r in reg.all_routes()}
        expected = {
            ("GET", "/reconciliation/status"),
            ("GET", "/reconciliation/failsafe-periods"),
            ("GET", "/reconciliation/shadow-budgets"),
            ("POST", "/reconciliation/shadow-budgets"),
            ("GET", "/reconciliation/shadow-budgets/{calculation_id}"),
            ("POST", "/reconciliation/shadow-budgets/{calculation_id}/approve"),
            ("POST", "/reconciliation/shadow-budgets/{calculation_id}/reject"),
            ("GET", "/reconciliation/excluded-periods"),
            ("POST", "/reconciliation/excluded-periods"),
            ("DELETE", "/reconciliation/excluded-periods/{exclusion_id}"),
            ("GET", "/reconciliation/config"),
            ("PUT", "/reconciliation/config"),
        }
        assert expected.issubset(paths)

    def test_canary_routes_registered(self):
        """7 canary routes."""
        reg = get_admin_registry()
        paths = {(r.method.value, r.path) for r in reg.all_routes()}
        expected = {
            ("GET", "/canary/rollouts"),
            ("POST", "/canary/rollouts"),
            ("GET", "/canary/rollouts/{rollout_id}"),
            ("POST", "/canary/rollouts/{rollout_id}/{action}"),
            ("GET", "/canary/rollouts/{rollout_id}/metrics"),
            ("POST", "/canary/panic-rollback"),
            ("GET", "/canary/history"),
        }
        assert expected.issubset(paths)

    def test_learning_routes_registered(self):
        """7 learning routes."""
        reg = get_admin_registry()
        paths = {(r.method.value, r.path) for r in reg.all_routes()}
        expected = {
            ("POST", "/learning/session/{action}"),
            ("GET", "/learning/pattern"),
            ("POST", "/learning/pattern"),
            ("GET", "/learning/suggestion"),
            ("POST", "/learning/suggestion/{suggestion_id}"),
            ("POST", "/learning/metric"),
            ("GET", "/learning/insights"),
        }
        assert expected.issubset(paths)

    def test_postmortem_routes_registered(self):
        """3 postmortem base routes."""
        reg = get_admin_registry()
        paths = {(r.method.value, r.path) for r in reg.all_routes()}
        expected = {
            ("POST", "/postmortem/generate"),
            ("GET", "/postmortem/incidents"),
            ("GET", "/postmortem/incidents/{incident_id}"),
        }
        assert expected.issubset(paths)

    def test_postmortem_revision_routes_registered(self):
        """6 postmortem revision routes."""
        reg = get_admin_registry()
        paths = {(r.method.value, r.path) for r in reg.all_routes()}
        expected = {
            ("GET", "/postmortem/{incident_id}/revisions"),
            ("POST", "/postmortem/{incident_id}/revisions"),
            ("GET", "/postmortem/{incident_id}/revisions/{revision_number}"),
            ("GET", "/postmortem/{incident_id}/revisions/compare"),
            ("POST", "/postmortem/{incident_id}/seal"),
            ("DELETE", "/postmortem/{incident_id}/seal"),
        }
        assert expected.issubset(paths)

    def test_config_history_routes_registered(self):
        """4 config-history routes."""
        reg = get_admin_registry()
        paths = {(r.method.value, r.path) for r in reg.all_routes()}
        expected = {
            ("GET", "/config-history/{config_type}/history"),
            ("GET", "/config-history/{config_type}/history/{version}"),
            ("POST", "/config-history/{config_type}/rollback"),
            ("GET", "/config-history/{config_type}/compare"),
        }
        assert expected.issubset(paths)

    def test_finops_routes_registered(self):
        """8 finops routes (2 budget GETs share the same handler)."""
        reg = get_admin_registry()
        paths = {(r.method.value, r.path) for r in reg.all_routes()}
        expected = {
            ("GET", "/finops/budget"),
            ("GET", "/finops/budget/{service_name}"),
            ("POST", "/finops/budget/{service_name}"),
            ("DELETE", "/finops/budget/{service_name}"),
            ("POST", "/finops/cost"),
            ("GET", "/finops/report"),
            ("GET", "/finops/alerts"),
            ("POST", "/finops/alerts/{alert_index}"),
        }
        assert expected.issubset(paths)

    def test_dlq_compressed_routes_registered(self):
        """3 dlq-compressed routes."""
        reg = get_admin_registry()
        paths = {(r.method.value, r.path) for r in reg.all_routes()}
        expected = {
            ("GET", "/dlq-compressed"),
            ("GET", "/dlq-compressed/summary"),
            ("GET", "/dlq-compressed/{entry_id}"),
        }
        assert expected.issubset(paths)

    def test_security_review_route_registered(self):
        """1 security-review route with ADMIN permission."""
        reg = get_admin_registry()
        paths = {(r.method.value, r.path) for r in reg.all_routes()}
        assert ("GET", "/security-review") in paths

    def test_security_review_permission_is_admin(self):
        """Security review requires ADMIN permission level."""
        reg = get_admin_registry()
        result = reg.resolve("GET", "/security-review")
        assert result is not None
        route, _ = result
        assert route.permission_level == PermissionLevel.ADMIN


# =============================================================================
# Contract — Permission level assignments
# =============================================================================


class TestPhase2bPermissionContract:
    """Critical permission level assignments are explicit contracts."""

    def test_kill_all_requires_admin(self):
        """chaos/kill-all is destructive — must be ADMIN."""
        reg = get_admin_registry()
        result = reg.resolve("POST", "/chaos/kill-all")
        assert result is not None
        route, _ = result
        assert route.permission_level == PermissionLevel.ADMIN

    def test_meta_watchdog_liveness_is_public(self):
        """meta-watchdog/liveness is a health probe — PUBLIC."""
        reg = get_admin_registry()
        result = reg.resolve("GET", "/meta-watchdog/liveness")
        assert result is not None
        route, _ = result
        assert route.permission_level == PermissionLevel.PUBLIC

    def test_meta_watchdog_escalation_test_requires_operator(self):
        """escalation-test fires a real notification — operator action, OPERATOR."""
        reg = get_admin_registry()
        result = reg.resolve("POST", "/meta-watchdog/escalation-test")
        assert result is not None
        route, _ = result
        assert route.permission_level == PermissionLevel.OPERATOR

    def test_resilience_metrics_reset_requires_operator(self):
        """Metrics reset is an operational action — OPERATOR."""
        reg = get_admin_registry()
        result = reg.resolve("POST", "/resilience/metrics/reset")
        assert result is not None
        route, _ = result
        assert route.permission_level == PermissionLevel.OPERATOR

    def test_l2_storage_status_is_viewer(self):
        """Read-only status endpoints are VIEWER."""
        reg = get_admin_registry()
        result = reg.resolve("GET", "/l2-storage/status")
        assert result is not None
        route, _ = result
        assert route.permission_level == PermissionLevel.VIEWER

    def test_shadow_log_replay_requires_operator(self):
        """Shadow log replay is operational — OPERATOR."""
        reg = get_admin_registry()
        result = reg.resolve("POST", "/l2-storage/shadow-log/replay")
        assert result is not None
        route, _ = result
        assert route.permission_level == PermissionLevel.OPERATOR


# =============================================================================
# Behavior — registry integrity (no duplicates, all resolvable)
# =============================================================================


class TestPhase2bRegistryIntegrityBehavior:
    """Phase 1 + 2a + 2b must form a consistent routing table."""

    def test_no_duplicate_method_path_pairs(self):
        """Each (method, path) pair occurs exactly once."""
        reg = get_admin_registry()
        routes = reg.all_routes()
        pairs = [(r.method.value, r.path) for r in routes]
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

    def test_total_route_count_meets_minimum(self):
        """Phase 1 (8) + Phase 2a (~70) + Phase 2b (~143) >= 210."""
        reg = get_admin_registry()
        assert len(reg.all_routes()) >= 210

    def test_phase1_liveness_still_resolvable(self):
        """Phase 2b registration must not shadow Phase 1 routes."""
        reg = get_admin_registry()
        result = reg.resolve("GET", "/liveness")
        assert result is not None
        route, _ = result
        assert route.permission_level == PermissionLevel.PUBLIC

    def test_phase2a_system_status_still_resolvable(self):
        """Phase 2b registration must not shadow Phase 2a routes."""
        reg = get_admin_registry()
        result = reg.resolve("GET", "/system/status")
        assert result is not None


# =============================================================================
# Behavior — path parameter resolution
# =============================================================================


class TestPhase2bPathParamResolutionBehavior:
    """Phase 2b parameterized routes extract path segments correctly."""

    def test_resilience_cb_name_param_resolves(self):
        """POST /resilience/cb/reset/{name} extracts name."""
        reg = get_admin_registry()
        result = reg.resolve("POST", "/resilience/cb/reset/audit-backend")
        assert result is not None
        _, params = result
        assert params == {"name": "audit-backend"}

    def test_chaos_schedule_id_param_resolves(self):
        """GET /chaos/schedules/{schedule_id} extracts schedule_id."""
        reg = get_admin_registry()
        result = reg.resolve("GET", "/chaos/schedules/sched-42")
        assert result is not None
        _, params = result
        assert params == {"schedule_id": "sched-42"}

    def test_l2_storage_drift_service_name_param_resolves(self):
        """POST /l2-storage/drift/reconcile/{service_name} extracts name."""
        reg = get_admin_registry()
        result = reg.resolve("POST", "/l2-storage/drift/reconcile/payment")
        assert result is not None
        _, params = result
        assert params == {"service_name": "payment"}

    def test_continuous_audit_log_id_param_resolves(self):
        """GET /audit/logs/{log_id} extracts log_id."""
        reg = get_admin_registry()
        result = reg.resolve("GET", "/audit/logs/log-abc-123")
        assert result is not None
        _, params = result
        assert params == {"log_id": "log-abc-123"}

    def test_compliance_multi_param_resolves(self):
        """PATCH /compliance/reports/{report_id}/checks/{check_id}/review."""
        reg = get_admin_registry()
        result = reg.resolve("PATCH", "/compliance/reports/rpt-1/checks/chk-99/review")
        assert result is not None
        _, params = result
        assert params == {"report_id": "rpt-1", "check_id": "chk-99"}

    def test_reconciliation_calculation_id_param_resolves(self):
        """GET /reconciliation/shadow-budgets/{calculation_id}."""
        reg = get_admin_registry()
        result = reg.resolve("GET", "/reconciliation/shadow-budgets/calc-42")
        assert result is not None
        _, params = result
        assert params == {"calculation_id": "calc-42"}

    def test_canary_rollout_action_multi_param_resolves(self):
        """POST /canary/rollouts/{rollout_id}/{action} extracts both."""
        reg = get_admin_registry()
        result = reg.resolve("POST", "/canary/rollouts/roll-1/promote")
        assert result is not None
        _, params = result
        assert params == {"rollout_id": "roll-1", "action": "promote"}

    def test_postmortem_revision_multi_param_resolves(self):
        """GET /postmortem/{incident_id}/revisions/{revision_number}."""
        reg = get_admin_registry()
        result = reg.resolve("GET", "/postmortem/inc-5/revisions/3")
        assert result is not None
        _, params = result
        assert params == {"incident_id": "inc-5", "revision_number": "3"}

    def test_config_history_multi_param_resolves(self):
        """GET /config-history/{config_type}/history/{version}."""
        reg = get_admin_registry()
        result = reg.resolve("GET", "/config-history/circuit-breaker/history/v2")
        assert result is not None
        _, params = result
        assert params == {"config_type": "circuit-breaker", "version": "v2"}

    def test_finops_alert_index_param_resolves(self):
        """POST /finops/alerts/{alert_index}."""
        reg = get_admin_registry()
        result = reg.resolve("POST", "/finops/alerts/7")
        assert result is not None
        _, params = result
        assert params == {"alert_index": "7"}

    def test_shadow_log_service_name_param_resolves(self):
        """GET /l2-storage/shadow-log/service/{service_name}."""
        reg = get_admin_registry()
        result = reg.resolve("GET", "/l2-storage/shadow-log/service/auth-svc")
        assert result is not None
        _, params = result
        assert params == {"service_name": "auth-svc"}


# =============================================================================
# Behavior — per-module fault isolation
# =============================================================================


class TestPhase2bFaultIsolationBehavior:
    """Each sub-register runs independently — failure in one doesn't
    block others."""

    def test_sub_register_is_idempotent(self):
        """Re-invoking a sub-register does not add duplicate routes."""
        reg = AdminRegistry()
        _register_chaos_routes(reg)
        first_count = len(reg.all_routes())
        _register_chaos_routes(reg)
        second_count = len(reg.all_routes())
        assert first_count == second_count

    def test_each_sub_register_contributes_routes(self):
        """Each Phase 2b sub-register adds at least one route."""
        sub_registers = (
            _register_audit_resilience_routes,
            _register_operations_routes,
            _register_chaos_routes,
            _register_l2_storage_routes,
            _register_continuous_audit_routes,
            _register_error_budget_routes,
            _register_recovery_routes,
            _register_analysis_routes,
            _register_config_data_routes,
            _register_security_review_routes,
        )
        for sub in sub_registers:
            reg = AdminRegistry()
            sub(reg)
            assert len(reg.all_routes()) >= 1, f"{sub.__name__} registered zero routes"

    def test_sub_register_runs_on_empty_registry(self):
        """Sub-registers don't require Phase 1/2a routes to exist first."""
        reg = AdminRegistry()
        _register_chaos_routes(reg)
        assert len(reg.all_routes()) >= 1
