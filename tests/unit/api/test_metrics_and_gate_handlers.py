"""Unit tests for framework-agnostic metrics, Error Budget Gate, and
health-extras handlers (429 PR3-phase2a).

Targets:
  - ``baldur.api.handlers.metrics`` (baldur_metrics, prometheus_text_metrics)
  - ``baldur.api.handlers.error_budget_gate`` (gate_health, config get/update, reset)
  - ``baldur.api.handlers.health`` (pool_health_check, simple_health_ping)

Verification techniques applied (§8):
  - §8.2 Exception/edge cases — ImportError fallback, unknown component
  - §8.4 Side effects — field-filtered update, reset actions
  - §8.5 Dependency interaction — service method invocations
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import pytest

from baldur.api.handlers.error_budget_gate import (
    _ALLOWED_CONFIG_FIELDS,
    gate_config_get,
    gate_config_update,
    gate_health,
    gate_reset,
)
from baldur.api.handlers.health import pool_health_check, simple_health_ping
from baldur.api.handlers.metrics import baldur_metrics, prometheus_text_metrics
from baldur.interfaces.web_framework import HttpMethod, RequestContext


def _make_ctx(method="GET", path="/test/", query=None, json_body=None):
    return RequestContext(
        method=HttpMethod(method),
        path=path,
        query_params=query or {},
        path_params={},
        json_body=json_body,
    )


# =============================================================================
# metrics.baldur_metrics
# =============================================================================


class TestBaldurMetricsBehavior:
    def test_returns_control_api_metrics(self):
        service = MagicMock()
        service.get_metrics.return_value = {
            "total_services": 3,
            "healthy_services": 2,
        }
        with patch(
            "baldur.services.control_api_service.get_control_api_service",
            return_value=service,
        ):
            resp = baldur_metrics(_make_ctx())
        assert resp.status_code == 200
        assert resp.body["total_services"] == 3


# =============================================================================
# metrics.prometheus_text_metrics
# =============================================================================


class TestPrometheusTextMetricsBehavior:
    """Prometheus endpoint returns raw text OR 503 on ImportError."""

    def test_returns_raw_text_when_prometheus_client_available(self):
        """generate_latest output is returned as text/plain body."""
        with patch.dict(
            sys.modules,
            {
                "prometheus_client": MagicMock(
                    CONTENT_TYPE_LATEST="text/plain; version=0.0.4",
                    generate_latest=MagicMock(return_value=b"baldur_up 1\n"),
                )
            },
        ):
            resp = prometheus_text_metrics(_make_ctx())
        assert resp.status_code == 200
        assert resp.body == "baldur_up 1\n"
        assert resp.content_type == "text/plain; version=0.0.4"

    def test_returns_503_when_prometheus_client_missing(self):
        """ImportError -> 503 with machine-readable body."""
        # Force ImportError by setting sys.modules entry to None
        with patch.dict(sys.modules, {"prometheus_client": None}):
            resp = prometheus_text_metrics(_make_ctx())
        assert resp.status_code == 503
        assert "prometheus_client not installed" in resp.body["error"]

    def test_bytes_output_decoded_to_string(self):
        """bytes -> utf-8 decoded string (ResponseContext.raw contract)."""
        with patch.dict(
            sys.modules,
            {
                "prometheus_client": MagicMock(
                    CONTENT_TYPE_LATEST="text/plain",
                    generate_latest=MagicMock(return_value=b"cb_open 0\n"),
                )
            },
        ):
            resp = prometheus_text_metrics(_make_ctx())
        assert isinstance(resp.body, str)


# =============================================================================
# error_budget_gate — Contract on allowed fields list
# =============================================================================


class TestErrorBudgetGateAllowedFieldsContract:
    """The allowed-fields whitelist IS the public update API."""

    def test_allowed_fields_contract_values(self):
        """Whitelist covers: enable flag, thresholds, cache TTL,
        rate limiter, circuit breaker, alerts — all 13 documented fields."""
        assert _ALLOWED_CONFIG_FIELDS == {
            "enabled",
            "critical_threshold_percent",
            "warning_threshold_percent",
            "fail_open",
            "cache_ttl_seconds",
            "fail_open_rate_limit_enabled",
            "fail_open_rate_limit_per_minute",
            "fail_open_rate_limit_window_seconds",
            "circuit_breaker_enabled",
            "circuit_breaker_failure_threshold",
            "circuit_breaker_recovery_timeout",
            "alert_on_fail_open",
            "alert_cooldown_seconds",
        }


# =============================================================================
# error_budget_gate handlers
# =============================================================================


class TestGateHealthBehavior:
    @pytest.fixture(autouse=True)
    def _require_pro(self):
        pytest.importorskip("baldur_pro")

    def test_healthy_returns_200(self):
        gate = MagicMock()
        gate.get_health_status.return_value = {"healthy": True}
        with patch(
            "baldur_pro.services.error_budget_gate.get_error_budget_gate",
            return_value=gate,
        ):
            resp = gate_health(_make_ctx())
        assert resp.status_code == 200

    def test_unhealthy_returns_503(self):
        gate = MagicMock()
        gate.get_health_status.return_value = {"healthy": False}
        with patch(
            "baldur_pro.services.error_budget_gate.get_error_budget_gate",
            return_value=gate,
        ):
            resp = gate_health(_make_ctx())
        assert resp.status_code == 503


class TestGateConfigGetBehavior:
    @pytest.fixture(autouse=True)
    def _require_pro(self):
        pytest.importorskip("baldur_pro")

    def test_returns_config_to_dict(self):
        gate = MagicMock()
        config = MagicMock()
        config.to_dict.return_value = {"enabled": True}
        gate.get_config.return_value = config
        with patch(
            "baldur_pro.services.error_budget_gate.get_error_budget_gate",
            return_value=gate,
        ):
            resp = gate_config_get(_make_ctx())
        assert resp.body["status"] == "success"
        assert resp.body["config"] == {"enabled": True}


class TestGateConfigUpdateBehavior:
    @pytest.fixture(autouse=True)
    def _require_pro(self):
        pytest.importorskip("baldur_pro")

    def test_mixed_valid_and_unknown_fields_rejected(self):
        """A typo riding valid keys rejects the whole body with 400 (nothing applied)."""
        gate = MagicMock()

        with patch(
            "baldur_pro.services.error_budget_gate.get_error_budget_gate",
            return_value=gate,
        ):
            resp = gate_config_update(
                _make_ctx(
                    method="PUT",
                    json_body={
                        "enabled": False,
                        "critical_threshold_percent": 5.0,
                        "unknown_field": "ignored",
                    },
                )
            )

        assert resp.status_code == 400
        assert "unknown_field" in resp.body["unknown_fields"]
        gate.update_config.assert_not_called()

    def test_no_valid_fields_returns_400(self):
        """Body with only unknown fields -> 400 without invoking gate."""
        gate = MagicMock()
        with patch(
            "baldur_pro.services.error_budget_gate.get_error_budget_gate",
            return_value=gate,
        ):
            resp = gate_config_update(_make_ctx(method="PUT", json_body={"unknown": 1}))
        assert resp.status_code == 400
        gate.update_config.assert_not_called()


class TestGateResetBehavior:
    @pytest.fixture(autouse=True)
    def _require_pro(self):
        pytest.importorskip("baldur_pro")

    def test_unknown_component_returns_400(self):
        gate = MagicMock()
        with patch(
            "baldur_pro.services.error_budget_gate.get_error_budget_gate",
            return_value=gate,
        ):
            resp = gate_reset(
                _make_ctx(method="POST", json_body={"component": "nonsense"})
            )
        assert resp.status_code == 400
        gate.clear_cache.assert_not_called()
        gate.reset_rate_limiter.assert_not_called()
        gate.reset_fault_detector.assert_not_called()
        gate.reset_alert_cooldowns.assert_not_called()

    def test_all_resets_every_component(self):
        """component='all' -> clear_cache + reset_rate_limiter + reset_fault_detector + reset_alert_cooldowns."""
        gate = MagicMock()
        with patch(
            "baldur_pro.services.error_budget_gate.get_error_budget_gate",
            return_value=gate,
        ):
            resp = gate_reset(_make_ctx(method="POST", json_body={"component": "all"}))
        gate.clear_cache.assert_called_once()
        gate.reset_rate_limiter.assert_called_once()
        gate.reset_fault_detector.assert_called_once()
        gate.reset_alert_cooldowns.assert_called_once()
        assert resp.status_code == 200
        assert set(resp.body["reset_components"]) == {
            "cache",
            "rate_limiter",
            "circuit_breaker",
            "alerts",
        }

    def test_default_component_is_all(self):
        """Empty body -> defaults to component='all'."""
        gate = MagicMock()
        with patch(
            "baldur_pro.services.error_budget_gate.get_error_budget_gate",
            return_value=gate,
        ):
            resp = gate_reset(_make_ctx(method="POST", json_body=None))
        gate.clear_cache.assert_called_once()
        assert resp.status_code == 200

    def test_specific_component_resets_only_that_one(self):
        """component='cache' -> only clear_cache, not others."""
        gate = MagicMock()
        with patch(
            "baldur_pro.services.error_budget_gate.get_error_budget_gate",
            return_value=gate,
        ):
            resp = gate_reset(
                _make_ctx(method="POST", json_body={"component": "cache"})
            )
        gate.clear_cache.assert_called_once()
        gate.reset_rate_limiter.assert_not_called()
        gate.reset_fault_detector.assert_not_called()
        gate.reset_alert_cooldowns.assert_not_called()
        assert resp.body["reset_components"] == ["cache"]


# =============================================================================
# health — pool_health_check + simple_health_ping
# =============================================================================


class TestPoolHealthCheckBehavior:
    def test_healthy_status_returns_200(self):
        pool_health = MagicMock()
        pool_health.status = "healthy"
        pool_health.to_dict.return_value = {"status": "healthy", "error": None}
        service = MagicMock()
        service.get_pool_health.return_value = pool_health
        with patch(
            "baldur.services.health_check.get_health_check_service",
            return_value=service,
        ):
            resp = pool_health_check(_make_ctx())
        assert resp.status_code == 200
        # None-error field is stripped
        assert "error" not in resp.body

    def test_degraded_status_returns_503(self):
        pool_health = MagicMock()
        pool_health.status = "degraded"
        pool_health.to_dict.return_value = {
            "status": "degraded",
            "error": "high utilization",
        }
        service = MagicMock()
        service.get_pool_health.return_value = pool_health
        with patch(
            "baldur.services.health_check.get_health_check_service",
            return_value=service,
        ):
            resp = pool_health_check(_make_ctx())
        assert resp.status_code == 503

    def test_error_status_returns_503(self):
        pool_health = MagicMock()
        pool_health.status = "error"
        pool_health.to_dict.return_value = {
            "status": "error",
            "error": "connection failed",
        }
        service = MagicMock()
        service.get_pool_health.return_value = pool_health
        with patch(
            "baldur.services.health_check.get_health_check_service",
            return_value=service,
        ):
            resp = pool_health_check(_make_ctx())
        assert resp.status_code == 503


class TestSimpleHealthPingBehavior:
    def test_returns_pong_without_dependencies(self):
        """No patches needed — pure static response."""
        resp = simple_health_ping(_make_ctx())
        assert resp.status_code == 200
        assert resp.body == {"ping": "pong", "status": "alive"}

    def test_includes_cache_control_header(self):
        """Load balancer hint: Cache-Control: max-age=1."""
        resp = simple_health_ping(_make_ctx())
        assert resp.headers.get("Cache-Control") == "max-age=1"

    def test_idempotent_multiple_calls(self):
        """Pure function — repeated calls return identical response."""
        first = simple_health_ping(_make_ctx())
        second = simple_health_ping(_make_ctx())
        assert first.body == second.body
        assert first.status_code == second.status_code
