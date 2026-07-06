"""Unit tests for Audit API Views (369 — Audit API Relocation).

Tests audit_resilience.py (9 Views) and continuous_audit.py (10 Views).
All views delegate to service layer — tests verify HTTP-layer behavior
(status codes, response structure, error handling).

Verification techniques:
- Contract: HTTP status codes, response keys, URL name mappings
- Behavior: delegation to service layer, error handling, format switching
- Dependency interaction: mock service calls with correct args
- Edge cases: invalid log IDs, unknown actions, missing CB names
"""

from __future__ import annotations

import json

import pytest

pytest.importorskip("django")

# Ensure Django apps are fully loaded before importing views with LoginRequiredMixin
import django

django.setup()

# AuditHealthView and CircuitBreakerStatusView now use HandlerAPIView (DRF-based)
# with PermissionLevel.VIEWER — bypass auth for unit tests


@pytest.fixture(autouse=True)
def _bypass_baldur_auth(monkeypatch):
    """Bypass DRF auth for HandlerAPIView-based views in unit tests."""
    monkeypatch.setenv("DISABLE_BALDUR_AUTH", "true")


def _make_get(path="/test/", **query_params):
    """Create a Django GET request with query parameters."""
    from django.test import RequestFactory

    factory = RequestFactory()
    return factory.get(path, data=query_params)


def _make_post(path="/test/", body=None, content_type="application/json"):
    """Create a Django POST request with JSON body."""
    from django.test import RequestFactory

    factory = RequestFactory()
    data = json.dumps(body) if body else ""
    return factory.post(path, data=data, content_type=content_type)


# =============================================================================
# Audit Resilience Views — audit_resilience.py
# =============================================================================


class TestAuditHealthViewBehavior:
    """AuditHealthView response behavior."""

    def test_healthy_status_when_no_issues(self):
        """Returns 'healthy' when no degraded mode and no open circuits."""
        from unittest.mock import MagicMock, patch

        from baldur.api.django.views.audit_resilience import AuditHealthView

        mock_logger = MagicMock()
        mock_logger.get_backend_health.return_value = {"status": "ok"}

        mock_manager = MagicMock()
        mock_manager.get_status.return_value = {"degraded": False}

        mock_registry = MagicMock()
        mock_registry.get_all_stats.return_value = {"backend_a": {}}
        mock_registry.get_open_circuits.return_value = []

        with (
            patch(
                "baldur.api.django.views.audit_resilience.AuditHealthView.get.__wrapped__",
                side_effect=None,
            )
            if False
            else patch(
                "baldur.audit.get_audit_logger",
                return_value=mock_logger,
            ),
            patch(
                "baldur.audit.get_degraded_mode_manager",
                return_value=mock_manager,
            ),
            patch(
                "baldur.audit.resilience.CircuitBreakerRegistry.get_instance",
                return_value=mock_registry,
            ),
        ):
            request = _make_get("/audit/resilience/health/")
            response = AuditHealthView.as_view()(request)

        assert response.status_code == 200
        body = json.loads(response.content)
        assert body["status"] == "healthy"
        assert "backend" in body
        assert "circuit_breakers" in body
        assert body["circuit_breakers"]["open_count"] == 0

    def test_degraded_status_when_degraded_mode_active(self):
        """Returns 'degraded' when degraded mode is active."""
        from unittest.mock import MagicMock, patch

        from baldur.api.django.views.audit_resilience import AuditHealthView

        mock_logger = MagicMock()
        mock_logger.get_backend_health.return_value = {}
        mock_manager = MagicMock()
        mock_manager.get_status.return_value = {"degraded": True}
        mock_registry = MagicMock()
        mock_registry.get_all_stats.return_value = {}
        mock_registry.get_open_circuits.return_value = []

        with (
            patch("baldur.audit.get_audit_logger", return_value=mock_logger),
            patch(
                "baldur.audit.get_degraded_mode_manager",
                return_value=mock_manager,
            ),
            patch(
                "baldur.audit.resilience.CircuitBreakerRegistry.get_instance",
                return_value=mock_registry,
            ),
        ):
            response = AuditHealthView.as_view()(_make_get())

        body = json.loads(response.content)
        assert body["status"] == "degraded"

    def test_warning_status_when_circuits_open(self):
        """Returns 'warning' when circuits are open but not degraded."""
        from unittest.mock import MagicMock, patch

        from baldur.api.django.views.audit_resilience import AuditHealthView

        mock_logger = MagicMock()
        mock_logger.get_backend_health.return_value = {}
        mock_manager = MagicMock()
        mock_manager.get_status.return_value = {"degraded": False}
        mock_registry = MagicMock()
        mock_registry.get_all_stats.return_value = {"db": {}}
        mock_registry.get_open_circuits.return_value = ["db"]

        with (
            patch("baldur.audit.get_audit_logger", return_value=mock_logger),
            patch(
                "baldur.audit.get_degraded_mode_manager",
                return_value=mock_manager,
            ),
            patch(
                "baldur.audit.resilience.CircuitBreakerRegistry.get_instance",
                return_value=mock_registry,
            ),
        ):
            response = AuditHealthView.as_view()(_make_get())

        body = json.loads(response.content)
        assert body["status"] == "warning"
        assert body["circuit_breakers"]["open_count"] == 1


class TestCircuitBreakerStatusViewBehavior:
    """CircuitBreakerStatusView list and detail behavior."""

    def test_list_returns_all_circuit_breakers(self):
        """GET without name returns list of all circuit breakers."""
        from unittest.mock import MagicMock, patch

        from baldur.api.django.views.audit_resilience import (
            CircuitBreakerStatusView,
        )

        mock_registry = MagicMock()
        mock_registry.get_all_stats.return_value = {"db": {"state": "closed"}}
        mock_registry.get_open_circuits.return_value = []

        with patch(
            "baldur.audit.resilience.CircuitBreakerRegistry.get_instance",
            return_value=mock_registry,
        ):
            response = CircuitBreakerStatusView.as_view()(_make_get())

        assert response.status_code == 200
        body = json.loads(response.content)
        assert "circuit_breakers" in body
        assert "open_circuits" in body

    def test_detail_returns_single_cb_stats(self):
        """GET with name returns single CB stats."""
        from unittest.mock import MagicMock, patch

        from baldur.api.django.views.audit_resilience import (
            CircuitBreakerStatusView,
        )

        mock_cb = MagicMock()
        mock_cb.get_stats.return_value = {"state": "closed", "failures": 0}
        mock_registry = MagicMock()
        mock_registry.get.return_value = mock_cb

        with patch(
            "baldur.audit.resilience.CircuitBreakerRegistry.get_instance",
            return_value=mock_registry,
        ):
            response = CircuitBreakerStatusView.as_view()(_make_get(), name="db")

        assert response.status_code == 200
        body = json.loads(response.content)
        assert body["state"] == "closed"

    def test_detail_returns_404_for_unknown_cb(self):
        """GET with unknown name returns 404."""
        from unittest.mock import MagicMock, patch

        from baldur.api.django.views.audit_resilience import (
            CircuitBreakerStatusView,
        )

        mock_registry = MagicMock()
        mock_registry.get.return_value = None

        with patch(
            "baldur.audit.resilience.CircuitBreakerRegistry.get_instance",
            return_value=mock_registry,
        ):
            response = CircuitBreakerStatusView.as_view()(_make_get(), name="unknown")

        assert response.status_code == 404


class TestAuditMetricsViewBehavior:
    """AuditMetricsView format switching behavior."""

    def test_json_format_returns_json_response(self):
        """format=json returns JsonResponse with metrics."""
        from unittest.mock import MagicMock, patch

        from baldur.api.django.views.audit_resilience import AuditMetricsView

        mock_metrics = MagicMock()
        mock_metrics.get_metrics.return_value = {"total": 42}

        with patch("baldur.audit.get_audit_metrics", return_value=mock_metrics):
            response = AuditMetricsView.as_view()(_make_get(format="json"))

        assert response.status_code == 200
        body = json.loads(response.content)
        assert body["total"] == 42

    def test_prometheus_format_returns_text_plain(self):
        """Default (prometheus) format returns text/plain."""
        from unittest.mock import MagicMock, patch

        from baldur.api.django.views.audit_resilience import AuditMetricsView

        mock_metrics = MagicMock()
        mock_metrics.get_prometheus_format.return_value = "# HELP audit_total\n"

        with patch("baldur.audit.get_audit_metrics", return_value=mock_metrics):
            response = AuditMetricsView.as_view()(_make_get())

        assert response.status_code == 200
        assert "text/plain" in response["Content-Type"]


class TestDegradedModeForceViewBehavior:
    """DegradedModeForceView action handling."""

    def test_enter_action_calls_force_degraded(self):
        """POST with action='enter' calls force_degraded."""
        from unittest.mock import MagicMock, patch

        from baldur.api.django.views.audit_resilience import DegradedModeForceView

        mock_manager = MagicMock()
        mock_manager.get_status.return_value = {"degraded": True}

        with patch(
            "baldur.audit.get_degraded_mode_manager",
            return_value=mock_manager,
        ):
            request = _make_post(body={"reason": "test reason"})
            response = DegradedModeForceView.as_view()(request, action="enter")

        assert response.status_code == 200
        mock_manager.force_degraded.assert_called_once_with("test reason")

    def test_exit_action_calls_force_normal(self):
        """POST with action='exit' calls force_normal."""
        from unittest.mock import MagicMock, patch

        from baldur.api.django.views.audit_resilience import DegradedModeForceView

        mock_manager = MagicMock()
        mock_manager.get_status.return_value = {"degraded": False}

        with patch(
            "baldur.audit.get_degraded_mode_manager",
            return_value=mock_manager,
        ):
            response = DegradedModeForceView.as_view()(_make_post(), action="exit")

        assert response.status_code == 200
        mock_manager.force_normal.assert_called_once()

    def test_unknown_action_returns_400(self):
        """POST with unknown action returns 400."""
        from unittest.mock import MagicMock, patch

        from baldur.api.django.views.audit_resilience import DegradedModeForceView

        mock_manager = MagicMock()
        with patch(
            "baldur.audit.get_degraded_mode_manager",
            return_value=mock_manager,
        ):
            response = DegradedModeForceView.as_view()(_make_post(), action="invalid")

        assert response.status_code == 400
        body = json.loads(response.content)
        assert "Unknown action" in body["error"]


# =============================================================================
# Continuous Audit Views — continuous_audit.py
# =============================================================================


class TestContinuousAuditQueryViewBehavior:
    """ContinuousAuditQueryView query parameter handling."""

    def test_returns_entries_with_count(self):
        """GET returns entries list with count."""
        from unittest.mock import MagicMock, patch

        from baldur.api.django.views.continuous_audit import (
            ContinuousAuditQueryView,
        )

        mock_recorder = MagicMock()
        mock_recorder.query.return_value = [{"action": "test"}]

        with patch(
            "baldur.api.handlers.continuous_audit._recorder",
            return_value=mock_recorder,
        ):
            response = ContinuousAuditQueryView.as_view()(_make_get(limit="50"))

        assert response.status_code == 200
        body = json.loads(response.content)
        assert body["count"] == 1
        assert body["filters"]["limit"] == 50

    def test_passes_query_params_to_recorder(self):
        """Query parameters are forwarded to recorder.query()."""
        from unittest.mock import MagicMock, patch

        from baldur.api.django.views.continuous_audit import (
            ContinuousAuditQueryView,
        )

        mock_recorder = MagicMock()
        mock_recorder.query.return_value = []

        with patch(
            "baldur.api.handlers.continuous_audit._recorder",
            return_value=mock_recorder,
        ):
            ContinuousAuditQueryView.as_view()(
                _make_get(target_type="runtime_config", target_id="timeout_ms")
            )

        call_kwargs = mock_recorder.query.call_args[1]
        assert call_kwargs["target_type"] == "runtime_config"
        assert call_kwargs["target_id"] == "timeout_ms"


class TestContinuousAuditDetailViewBehavior:
    """ContinuousAuditDetailView ID parsing and lookup."""

    def test_invalid_id_format_returns_400(self):
        """Invalid log ID format returns 400."""
        from baldur.api.django.views.continuous_audit import (
            ContinuousAuditDetailView,
        )

        response = ContinuousAuditDetailView.as_view()(
            _make_get(), log_id="invalid-format"
        )
        assert response.status_code == 400

    def test_wrong_prefix_returns_400(self):
        """Log ID not starting with 'audit' returns 400."""
        from baldur.api.django.views.continuous_audit import (
            ContinuousAuditDetailView,
        )

        response = ContinuousAuditDetailView.as_view()(
            _make_get(), log_id="log-20260320120000-000001"
        )
        assert response.status_code == 400

    def test_invalid_timestamp_returns_400(self):
        """Log ID with invalid timestamp returns 400."""
        from baldur.api.django.views.continuous_audit import (
            ContinuousAuditDetailView,
        )

        response = ContinuousAuditDetailView.as_view()(
            _make_get(), log_id="audit-notadate-000001"
        )
        assert response.status_code == 400

    def test_not_found_returns_404(self):
        """Valid ID format but no matching entry returns 404."""
        from unittest.mock import MagicMock, patch

        from baldur.api.django.views.continuous_audit import (
            ContinuousAuditDetailView,
        )

        mock_recorder = MagicMock()
        mock_recorder.query.return_value = []

        with patch(
            "baldur.api.handlers.continuous_audit._recorder",
            return_value=mock_recorder,
        ):
            response = ContinuousAuditDetailView.as_view()(
                _make_get(), log_id="audit-20260320120000-000001"
            )

        assert response.status_code == 404

    def test_matching_entry_returns_200(self):
        """Matching sequence returns entry."""
        from unittest.mock import MagicMock, patch

        from baldur.api.django.views.continuous_audit import (
            ContinuousAuditDetailView,
        )

        mock_recorder = MagicMock()
        mock_recorder.query.return_value = [
            {"details": {"integrity": {"sequence": 1}}, "action": "test"}
        ]

        with patch(
            "baldur.api.handlers.continuous_audit._recorder",
            return_value=mock_recorder,
        ):
            response = ContinuousAuditDetailView.as_view()(
                _make_get(), log_id="audit-20260320120000-000001"
            )

        assert response.status_code == 200
        body = json.loads(response.content)
        assert "entry" in body


class TestIntegrityVerifyViewBehavior:
    """IntegrityVerifyView status code branching."""

    def test_verified_returns_200(self):
        """Verified integrity returns HTTP 200."""
        from unittest.mock import MagicMock, patch

        from baldur.api.django.views.continuous_audit import IntegrityVerifyView

        mock_recorder = MagicMock()
        mock_recorder.verify_integrity.return_value = {"verified": True}

        with patch(
            "baldur.api.handlers.continuous_audit._recorder",
            return_value=mock_recorder,
        ):
            response = IntegrityVerifyView.as_view()(_make_get())

        assert response.status_code == 200

    def test_not_verified_returns_400(self):
        """Failed integrity verification returns HTTP 400."""
        from unittest.mock import MagicMock, patch

        from baldur.api.django.views.continuous_audit import IntegrityVerifyView

        mock_recorder = MagicMock()
        mock_recorder.verify_integrity.return_value = {
            "verified": False,
            "error": "chain broken",
        }

        with patch(
            "baldur.api.handlers.continuous_audit._recorder",
            return_value=mock_recorder,
        ):
            response = IntegrityVerifyView.as_view()(_make_get())

        assert response.status_code == 400


class TestChainStateViewBehavior:
    """ChainStateView response structure."""

    def test_returns_chain_state_and_timestamp(self):
        """Response contains chain_state and timestamp fields."""
        from unittest.mock import MagicMock, patch

        from baldur.api.django.views.continuous_audit import ChainStateView

        mock_recorder = MagicMock()
        mock_recorder.get_chain_state.return_value = {"sequence": 42, "hash": "abc"}

        with patch(
            "baldur.api.handlers.continuous_audit._recorder",
            return_value=mock_recorder,
        ):
            response = ChainStateView.as_view()(_make_get())

        assert response.status_code == 200
        body = json.loads(response.content)
        assert body["chain_state"]["sequence"] == 42
        assert "timestamp" in body


class TestExportJSONLViewBehavior:
    """ExportJSONLView streaming response."""

    def test_returns_streaming_response_with_ndjson_content_type(self):
        """Response is StreamingHttpResponse with application/x-ndjson."""
        from unittest.mock import MagicMock, patch

        from baldur.api.django.views.continuous_audit import ExportJSONLView

        mock_recorder = MagicMock()
        mock_recorder.export_jsonl.return_value = iter(['{"action":"test"}'])

        with patch(
            "baldur.api.handlers.continuous_audit._recorder",
            return_value=mock_recorder,
        ):
            response = ExportJSONLView.as_view()(_make_get())

        assert response.status_code == 200
        assert response["Content-Type"] == "application/x-ndjson"
        assert "audit_export.jsonl" in response["Content-Disposition"]


class TestExportCSVViewBehavior:
    """ExportCSVView streaming response."""

    def test_returns_streaming_response_with_csv_content_type(self):
        """Response is StreamingHttpResponse with text/csv."""
        from unittest.mock import MagicMock, patch

        from baldur.api.django.views.continuous_audit import ExportCSVView

        mock_recorder = MagicMock()
        mock_recorder.export_csv_compatible.return_value = iter([])

        with patch(
            "baldur.api.handlers.continuous_audit._recorder",
            return_value=mock_recorder,
        ):
            response = ExportCSVView.as_view()(_make_get())

        assert response.status_code == 200
        assert response["Content-Type"] == "text/csv"
        assert "audit_export.csv" in response["Content-Disposition"]


# =============================================================================
# URL Routing Contract — 369 URL Design (final per doc §3.3)
# =============================================================================


class TestAuditUrlPatternsContract:
    """369 URL pattern contract — these URLs are final (370 won't change them)."""

    @pytest.fixture(scope="class")
    def url_names(self):
        """Collect all URL names from baldur urlpatterns."""
        from baldur.api.django.urls import urlpatterns

        return {
            p.name: str(p.pattern) for p in urlpatterns if hasattr(p, "name") and p.name
        }

    # -- ControlAuditView rename --

    def test_control_audit_log_renamed(self, url_names):
        """ControlAuditView relocated to control/audit-log/ (was audit/)."""
        assert "control-audit-log" in url_names
        assert url_names["control-audit-log"] == "control/audit-log/"

    def test_old_audit_name_removed(self, url_names):
        """Old 'audit' URL name no longer exists."""
        assert "audit" not in url_names

    # -- Audit Resilience URLs (10 patterns) --

    def test_audit_health_url(self, url_names):
        """audit/resilience/health/ registered."""
        assert "audit-health" in url_names

    def test_audit_metrics_url(self, url_names):
        """audit/resilience/metrics/ registered."""
        assert "audit-metrics" in url_names

    def test_audit_metrics_reset_url(self, url_names):
        """audit/resilience/metrics/reset/ registered."""
        assert "audit-metrics-reset" in url_names

    def test_audit_circuit_breakers_list_url(self, url_names):
        """audit/resilience/circuit-breakers/ registered."""
        assert "audit-circuit-breakers-list" in url_names

    def test_audit_circuit_breaker_detail_url(self, url_names):
        """audit/resilience/circuit-breakers/<name>/ registered."""
        assert "audit-circuit-breaker-detail" in url_names

    def test_audit_circuit_breaker_reset_url(self, url_names):
        """audit/resilience/circuit-breakers/<name>/reset/ registered."""
        assert "audit-circuit-breaker-reset" in url_names

    def test_audit_circuit_breaker_force_open_url(self, url_names):
        """audit/resilience/circuit-breakers/<name>/force-open/ registered."""
        assert "audit-circuit-breaker-force-open" in url_names

    def test_audit_circuit_breakers_reset_all_url(self, url_names):
        """audit/resilience/circuit-breakers/reset-all/ registered."""
        assert "audit-circuit-breakers-reset-all" in url_names

    def test_audit_degraded_mode_status_url(self, url_names):
        """audit/resilience/degraded-mode/ registered."""
        assert "audit-degraded-mode-status" in url_names

    def test_audit_degraded_mode_action_url(self, url_names):
        """audit/resilience/degraded-mode/<action>/ registered."""
        assert "audit-degraded-mode-action" in url_names

    # -- Continuous Audit URLs (10 patterns) --

    def test_audit_logs_url(self, url_names):
        """audit/logs/ registered."""
        assert "audit-logs" in url_names

    def test_audit_log_detail_url(self, url_names):
        """audit/logs/<log_id>/ registered."""
        assert "audit-log-detail" in url_names

    def test_audit_auto_tuning_url(self, url_names):
        """audit/auto-tuning/ registered."""
        assert "audit-auto-tuning" in url_names

    def test_audit_drift_url(self, url_names):
        """audit/drift/ registered."""
        assert "audit-drift" in url_names

    def test_audit_compliance_url(self, url_names):
        """audit/compliance/ registered."""
        assert "audit-compliance" in url_names

    def test_audit_integrity_verify_url(self, url_names):
        """audit/integrity/verify/ registered."""
        assert "audit-integrity-verify" in url_names

    def test_audit_chain_state_url(self, url_names):
        """audit/integrity/state/ registered."""
        assert "audit-chain-state" in url_names

    def test_audit_export_jsonl_url(self, url_names):
        """audit/export/jsonl/ registered."""
        assert "audit-export-jsonl" in url_names

    def test_audit_export_csv_url(self, url_names):
        """audit/export/csv/ registered."""
        assert "audit-export-csv" in url_names

    def test_audit_config_url(self, url_names):
        """audit/config/ registered."""
        assert "audit-config" in url_names

    # -- Total count --

    def test_total_audit_url_count(self, url_names):
        """21 audit-related URLs exist (1 renamed + 10 resilience + 10 continuous)."""
        audit_urls = [
            name
            for name in url_names
            if name.startswith("audit-") or name == "control-audit-log"
        ]
        assert len(audit_urls) == 21
