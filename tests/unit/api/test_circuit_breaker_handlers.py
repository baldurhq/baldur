"""Unit tests for framework-agnostic circuit breaker control handlers
(429 PR3-phase2a).

Target: ``baldur.api.handlers.circuit_breaker`` — control action,
status, audit, quick actions (allow/block/reset).

Verification techniques applied (§8):
  - §8.1 Boundary analysis — TTL range, service_name length
  - §8.2 Exception/edge cases — missing fields, unknown values
  - §8.8 State transition — cross-field rules (inject_failure+ops, override+ops)
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from baldur.api.handlers.circuit_breaker import (
    _validate_control_request,
    control_action,
    control_audit,
    control_status,
    quick_allow,
    quick_block,
    quick_reset,
    service_status,
)
from baldur.core.constants import ControlAPIActions, ControlAPIEnvironments
from baldur.interfaces.web_framework import HttpMethod, RequestContext


def _make_ctx(
    method="GET",
    path="/test/",
    query=None,
    path_params=None,
    json_body=None,
    user=None,
):
    return RequestContext(
        method=HttpMethod(method),
        path=path,
        query_params=query or {},
        path_params=path_params or {},
        json_body=json_body,
        user=user,
    )


def _valid_body(**overrides) -> dict:
    base = {
        "service_name": "payment",
        "action": ControlAPIActions.ALLOW,
        "reason": "unit test",
        "environment": ControlAPIEnvironments.OPS,
    }
    base.update(overrides)
    return base


# =============================================================================
# _validate_control_request — Contract (cross-field rules are design spec)
# =============================================================================


class TestValidateControlRequestContract:
    """Cross-field validation rules are part of the API design contract."""

    def test_inject_failure_is_forbidden_in_ops_environment(self):
        """inject_failure + ops -> validation error (design rule)."""
        cleaned, err = _validate_control_request(
            _valid_body(
                action=ControlAPIActions.INJECT_FAILURE,
                environment=ControlAPIEnvironments.OPS,
            )
        )
        assert cleaned == {}
        assert err is not None
        assert "inject_failure" in err
        assert "ops" in err

    def test_inject_failure_allowed_in_non_ops_environments(self):
        """inject_failure + test/chaos -> valid."""
        for env in (ControlAPIEnvironments.TEST, ControlAPIEnvironments.CHAOS):
            cleaned, err = _validate_control_request(
                _valid_body(
                    action=ControlAPIActions.INJECT_FAILURE,
                    environment=env,
                )
            )
            assert err is None, f"env={env} should be valid for inject_failure"
            assert cleaned["action"] == ControlAPIActions.INJECT_FAILURE

    def test_override_in_ops_requires_ttl(self):
        """override + ops without TTL -> validation error."""
        _, err = _validate_control_request(
            _valid_body(
                action=ControlAPIActions.OVERRIDE,
                environment=ControlAPIEnvironments.OPS,
            )
        )
        assert err is not None
        assert "TTL is required" in err

    def test_override_in_ops_ttl_cannot_exceed_60_minutes(self):
        """override + ops + ttl>60 -> validation error (design rule)."""
        _, err = _validate_control_request(
            _valid_body(
                action=ControlAPIActions.OVERRIDE,
                environment=ControlAPIEnvironments.OPS,
                ttl_minutes=61,
            )
        )
        assert err is not None
        assert "60 minutes" in err

    def test_override_in_ops_ttl_exactly_60_is_valid(self):
        """override + ops + ttl=60 -> boundary pass."""
        cleaned, err = _validate_control_request(
            _valid_body(
                action=ControlAPIActions.OVERRIDE,
                environment=ControlAPIEnvironments.OPS,
                ttl_minutes=60,
            )
        )
        assert err is None
        assert cleaned["ttl_minutes"] == 60


class TestValidateControlRequestBehavior:
    """Field-level validation behavior."""

    def test_missing_service_name_fails(self):
        body = _valid_body()
        del body["service_name"]
        _, err = _validate_control_request(body)
        assert err is not None

    def test_missing_action_fails(self):
        body = _valid_body()
        del body["action"]
        _, err = _validate_control_request(body)
        assert err is not None

    def test_unknown_action_fails(self):
        _, err = _validate_control_request(_valid_body(action="nuke"))
        assert err is not None

    def test_unknown_environment_fails(self):
        _, err = _validate_control_request(_valid_body(environment="mars"))
        assert err is not None

    def test_service_name_length_boundary(self):
        """service_name max=100 chars."""
        # 100 chars — pass
        _, err = _validate_control_request(_valid_body(service_name="a" * 100))
        assert err is None
        # 101 chars — fail
        _, err = _validate_control_request(_valid_body(service_name="a" * 101))
        assert err is not None

    def test_ttl_minutes_range_boundaries(self):
        """ttl_minutes must be 1..1440 when provided."""
        # 0 -> invalid
        _, err = _validate_control_request(
            _valid_body(action=ControlAPIActions.ALLOW, ttl_minutes=0)
        )
        assert err is not None
        # 1441 -> invalid
        _, err = _validate_control_request(
            _valid_body(action=ControlAPIActions.ALLOW, ttl_minutes=1441)
        )
        assert err is not None
        # 1 -> valid
        _, err = _validate_control_request(
            _valid_body(
                action=ControlAPIActions.OVERRIDE,
                environment=ControlAPIEnvironments.TEST,
                ttl_minutes=1,
            )
        )
        assert err is None

    def test_ttl_minutes_rejects_bool_masquerading_as_int(self):
        """Python True/False are int subclass; validator must reject."""
        _, err = _validate_control_request(
            _valid_body(action=ControlAPIActions.ALLOW, ttl_minutes=True)
        )
        assert err is not None

    def test_cleaned_payload_preserves_metadata(self):
        cleaned, err = _validate_control_request(
            _valid_body(metadata={"simulate_latency_ms": 200})
        )
        assert err is None
        assert cleaned["metadata"] == {"simulate_latency_ms": 200}

    def test_cleaned_payload_metadata_defaults_to_empty_dict(self):
        cleaned, err = _validate_control_request(_valid_body())
        assert err is None
        assert cleaned["metadata"] == {}


# =============================================================================
# control_action
# =============================================================================


def _mock_response(status: str = "success"):
    resp = MagicMock()
    resp.status = status
    resp.to_dict.return_value = {"status": status}
    return resp


class TestControlActionBehavior:
    """control_action() — validation + response-status → HTTP mapping."""

    def test_validation_error_returns_400(self):
        """Invalid body -> 400 without invoking service."""
        service = MagicMock()
        with patch(
            "baldur.api.handlers.circuit_breaker.get_control_api_service",
            return_value=service,
        ):
            resp = control_action(
                _make_ctx(method="POST", json_body={"action": "nuke"})
            )
        assert resp.status_code == 400
        assert resp.body["status"] == "rejected"
        service.execute.assert_not_called()

    def test_service_rejected_maps_to_403(self):
        """ControlResponse.status='rejected' -> 403."""
        service = MagicMock()
        service.execute.return_value = _mock_response(status="rejected")
        with patch(
            "baldur.api.handlers.circuit_breaker.get_control_api_service",
            return_value=service,
        ):
            resp = control_action(_make_ctx(method="POST", json_body=_valid_body()))
        assert resp.status_code == 403

    def test_service_error_maps_to_500(self):
        """ControlResponse.status='error' -> 500."""
        service = MagicMock()
        service.execute.return_value = _mock_response(status="error")
        with patch(
            "baldur.api.handlers.circuit_breaker.get_control_api_service",
            return_value=service,
        ):
            resp = control_action(_make_ctx(method="POST", json_body=_valid_body()))
        assert resp.status_code == 500

    def test_service_success_maps_to_200(self):
        """Any other status -> 200."""
        service = MagicMock()
        service.execute.return_value = _mock_response(status="success")
        with patch(
            "baldur.api.handlers.circuit_breaker.get_control_api_service",
            return_value=service,
        ):
            resp = control_action(_make_ctx(method="POST", json_body=_valid_body()))
        assert resp.status_code == 200

    def test_staff_user_maps_to_admin_role(self):
        """request.user.is_staff=True -> actor_role='admin'."""
        service = MagicMock()
        service.execute.return_value = _mock_response(status="success")
        user = SimpleNamespace(username="op1", is_staff=True)
        with patch(
            "baldur.api.handlers.circuit_breaker.get_control_api_service",
            return_value=service,
        ):
            control_action(_make_ctx(method="POST", json_body=_valid_body(), user=user))
        _, kwargs = service.execute.call_args
        request_obj = service.execute.call_args[0][0]
        assert request_obj.actor == "op1"
        assert request_obj.actor_role == "admin"

    def test_non_staff_user_maps_to_user_role(self):
        """is_staff=False -> actor_role='user'."""
        service = MagicMock()
        service.execute.return_value = _mock_response(status="success")
        user = SimpleNamespace(username="op2", is_staff=False)
        with patch(
            "baldur.api.handlers.circuit_breaker.get_control_api_service",
            return_value=service,
        ):
            control_action(_make_ctx(method="POST", json_body=_valid_body(), user=user))
        request_obj = service.execute.call_args[0][0]
        assert request_obj.actor_role == "user"


# =============================================================================
# control_status / service_status
# =============================================================================


class TestControlStatusBehavior:
    def test_forwards_environment_query_param(self):
        service = MagicMock()
        service.get_status.return_value = {"services": []}
        with patch(
            "baldur.api.handlers.circuit_breaker.get_control_api_service",
            return_value=service,
        ):
            resp = control_status(_make_ctx(query={"environment": "chaos"}))
        service.get_status.assert_called_once_with(environment="chaos")
        assert resp.status_code == 200

    def test_defaults_to_ops_when_environment_missing(self):
        service = MagicMock()
        service.get_status.return_value = {"services": []}
        with patch(
            "baldur.api.handlers.circuit_breaker.get_control_api_service",
            return_value=service,
        ):
            control_status(_make_ctx())
        service.get_status.assert_called_once_with(environment="ops")


class TestServiceStatusBehavior:
    def test_missing_service_name_returns_400(self):
        with patch(
            "baldur.api.handlers.circuit_breaker.get_control_api_service",
        ) as mock_get:
            resp = service_status(_make_ctx(path_params={"service_name": ""}))
        assert resp.status_code == 400
        mock_get.assert_not_called()

    def test_forwards_service_name_to_service(self):
        service = MagicMock()
        service.get_service_status.return_value = {"service_name": "payment"}
        with patch(
            "baldur.api.handlers.circuit_breaker.get_control_api_service",
            return_value=service,
        ):
            resp = service_status(_make_ctx(path_params={"service_name": "payment"}))
        service.get_service_status.assert_called_once_with("payment")
        assert resp.status_code == 200


# =============================================================================
# control_audit
# =============================================================================


class TestControlAuditBehavior:
    """control_audit() — D19 H1 schema + in-memory filters."""

    def _mock_entry(self, **kwargs):
        entry = MagicMock()
        entry.to_dict.return_value = kwargs
        entry.target_type = kwargs.get("target_type", "threshold")
        entry.actor_id = kwargs.get("actor_id", "user1")
        return entry

    def test_audit_query_uses_h1_schema_version(self):
        """Response always includes schema_version='h1'."""
        mock_adapter = MagicMock()
        mock_adapter.query.return_value = []
        with patch(
            "baldur.factory.ProviderRegistry.get_audit_adapter",
            return_value=mock_adapter,
        ):
            resp = control_audit(_make_ctx())
        assert resp.body["schema_version"] == "h1"

    def test_config_type_filter_applied_in_memory(self):
        """config_type filter narrows to matching target_type."""
        entries = [
            self._mock_entry(target_type="threshold"),
            self._mock_entry(target_type="feature_flag"),
            self._mock_entry(target_type="threshold"),
        ]
        mock_adapter = MagicMock()
        mock_adapter.query.return_value = entries
        with patch(
            "baldur.factory.ProviderRegistry.get_audit_adapter",
            return_value=mock_adapter,
        ):
            resp = control_audit(_make_ctx(query={"config_type": "threshold"}))
        assert resp.body["total_count"] == 2
        assert resp.body["filters"]["config_type"] == "threshold"

    def test_user_filter_applied_in_memory(self):
        """user filter narrows to matching actor_id."""
        entries = [
            self._mock_entry(actor_id="alice"),
            self._mock_entry(actor_id="bob"),
        ]
        mock_adapter = MagicMock()
        mock_adapter.query.return_value = entries
        with patch(
            "baldur.factory.ProviderRegistry.get_audit_adapter",
            return_value=mock_adapter,
        ):
            resp = control_audit(_make_ctx(query={"user": "alice"}))
        assert resp.body["total_count"] == 1

    def test_adapter_exception_returns_graceful_empty_response(self):
        """Audit adapter failure -> empty H1-shaped response (no 5xx)."""
        with patch(
            "baldur.factory.ProviderRegistry.get_audit_adapter",
            side_effect=Exception("backend down"),
        ):
            resp = control_audit(_make_ctx())
        assert resp.status_code == 200
        assert resp.body["logs"] == []
        assert resp.body["total_count"] == 0
        assert "error" in resp.body
        assert resp.body["schema_version"] == "h1"

    def test_invalid_page_query_falls_back_to_one(self):
        """Non-int page query param -> page=1 (graceful)."""
        mock_adapter = MagicMock()
        mock_adapter.query.return_value = []
        with patch(
            "baldur.factory.ProviderRegistry.get_audit_adapter",
            return_value=mock_adapter,
        ):
            resp = control_audit(_make_ctx(query={"page": "notanumber"}))
        assert resp.body["page"] == 1


# =============================================================================
# Quick actions — allow / block / reset
# =============================================================================


class TestQuickActionsBehavior:
    """_quick_action() forwards service_name + default reason/TTL."""

    def test_quick_allow_requires_service_name(self):
        with patch(
            "baldur.api.handlers.circuit_breaker.get_control_api_service",
        ) as mock_get:
            resp = quick_allow(
                _make_ctx(method="POST", path_params={"service_name": ""})
            )
        assert resp.status_code == 400
        mock_get.assert_not_called()

    def test_quick_allow_sends_allow_action(self):
        service = MagicMock()
        service.execute.return_value = _mock_response()
        with patch(
            "baldur.api.handlers.circuit_breaker.get_control_api_service",
            return_value=service,
        ):
            quick_allow(
                _make_ctx(method="POST", path_params={"service_name": "payment"})
            )
        req = service.execute.call_args[0][0]
        assert req.action == ControlAPIActions.ALLOW
        assert req.service_name == "payment"

    def test_quick_block_applies_default_ttl(self):
        """Block action gets default ttl_minutes=90 if not overridden."""
        service = MagicMock()
        service.execute.return_value = _mock_response()
        with patch(
            "baldur.api.handlers.circuit_breaker.get_control_api_service",
            return_value=service,
        ):
            quick_block(
                _make_ctx(method="POST", path_params={"service_name": "payment"})
            )
        req = service.execute.call_args[0][0]
        assert req.action == ControlAPIActions.BLOCK
        assert req.ttl_minutes == 90

    def test_quick_block_respects_user_override_ttl(self):
        """Body ttl_minutes overrides the default."""
        service = MagicMock()
        service.execute.return_value = _mock_response()
        with patch(
            "baldur.api.handlers.circuit_breaker.get_control_api_service",
            return_value=service,
        ):
            quick_block(
                _make_ctx(
                    method="POST",
                    path_params={"service_name": "payment"},
                    json_body={"ttl_minutes": 15},
                )
            )
        req = service.execute.call_args[0][0]
        assert req.ttl_minutes == 15

    def test_quick_reset_sends_reset_action(self):
        service = MagicMock()
        service.execute.return_value = _mock_response()
        with patch(
            "baldur.api.handlers.circuit_breaker.get_control_api_service",
            return_value=service,
        ):
            quick_reset(
                _make_ctx(method="POST", path_params={"service_name": "payment"})
            )
        req = service.execute.call_args[0][0]
        assert req.action == ControlAPIActions.RESET
