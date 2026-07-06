"""Unit tests for framework-agnostic Emergency Mode handlers (429 PR3-phase2a).

Target: ``baldur.api.handlers.emergency`` — status, trigger/release,
gradual recovery, history, config, levels.

Verification techniques applied (§8):
  - §8.2 Exception/edge cases — invalid level, NORMAL trigger, missing fields
  - §8.4 Side effects — manager state transitions
  - §8.5 Dependency interaction — error mapping (RecoveryNotAllowedError → 409)
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from baldur.api.handlers.emergency import (
    emergency_config_get,
    emergency_config_update,
    emergency_history,
    emergency_levels,
    emergency_release,
    emergency_status,
    emergency_trigger,
    gradual_recovery_start,
    gradual_recovery_stop,
)
from baldur.interfaces.web_framework import HttpMethod, RequestContext

# Every test in this module exercises the PRO emergency-mode manager (function-body
# ``baldur_pro`` imports / patches). With baldur_pro absent (public mirror) the
# whole module is skipped; no pure-OSS test here would otherwise run.
pytest.importorskip("baldur_pro")

pytestmark = pytest.mark.requires_pro


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


def _get_level_enum():
    from baldur_pro.services.emergency_mode.enums import EmergencyLevel

    return EmergencyLevel


def _mock_state(level_name: str = "LEVEL_1", is_active: bool = True) -> MagicMock:
    EmergencyLevel = _get_level_enum()
    state = MagicMock()
    state.is_active = is_active
    state.level = EmergencyLevel[level_name]
    state.activated_at = "2026-04-01T00:00:00+00:00"
    state.activated_by = "admin"
    state.activation_reason = "test"
    state.expires_at = None
    state.is_auto_triggered = False
    state.is_recovering = False
    state.recovery_started_at = None
    state.target_level = None
    state.deactivated_at = None
    state.deactivated_by = None
    return state


# =============================================================================
# emergency_status / emergency_levels
# =============================================================================


class TestEmergencyStatusBehavior:
    def test_state_level_included_in_response(self):
        EmergencyLevel = _get_level_enum()
        manager = MagicMock()
        manager.get_state.return_value = _mock_state("LEVEL_2")
        with patch(
            "baldur_pro.services.emergency_mode.get_emergency_manager",
            return_value=manager,
        ):
            resp = emergency_status(_make_ctx())
        assert resp.status_code == 200
        assert resp.body["level"] == EmergencyLevel.LEVEL_2.value
        assert resp.body["is_active"] is True
        assert "tier_multipliers" in resp.body
        assert "available_levels" in resp.body


class TestEmergencyLevelsBehavior:
    def test_returns_all_four_levels(self):
        """Response contains NORMAL + LEVEL_1..LEVEL_3."""
        EmergencyLevel = _get_level_enum()
        resp = emergency_levels(_make_ctx())
        level_names = {entry["name"] for entry in resp.body["levels"]}
        expected = {level.value for level in EmergencyLevel}
        assert level_names == expected


# =============================================================================
# emergency_trigger
# =============================================================================


class TestEmergencyTriggerBehavior:
    def test_missing_reason_returns_400(self):
        with patch(
            "baldur_pro.services.emergency_mode.get_emergency_manager"
        ) as mock_get:
            resp = emergency_trigger(
                _make_ctx(method="POST", json_body={"level": "LEVEL_1"})
            )
        assert resp.status_code == 400
        mock_get.assert_not_called()

    def test_invalid_level_returns_400(self):
        with patch(
            "baldur_pro.services.emergency_mode.get_emergency_manager"
        ) as mock_get:
            resp = emergency_trigger(
                _make_ctx(
                    method="POST",
                    json_body={"level": "LEVEL_99", "reason": "x"},
                )
            )
        assert resp.status_code == 400
        mock_get.assert_not_called()

    def test_normal_level_rejected(self):
        """NORMAL is not a triggerable emergency level."""
        with patch(
            "baldur_pro.services.emergency_mode.get_emergency_manager"
        ) as mock_get:
            resp = emergency_trigger(
                _make_ctx(
                    method="POST",
                    json_body={"level": "NORMAL", "reason": "x"},
                )
            )
        assert resp.status_code == 400
        mock_get.assert_not_called()

    def test_valid_level_invokes_manager_activate(self):
        manager = MagicMock()
        manager.activate_manual.return_value = _mock_state("LEVEL_2")
        with patch(
            "baldur_pro.services.emergency_mode.get_emergency_manager",
            return_value=manager,
        ):
            resp = emergency_trigger(
                _make_ctx(
                    method="POST",
                    json_body={
                        "level": "LEVEL_2",
                        "reason": "high error rate",
                        "duration_minutes": 30,
                    },
                    user=SimpleNamespace(username="oncall"),
                )
            )
        manager.activate_manual.assert_called_once()
        _, kwargs = manager.activate_manual.call_args
        assert kwargs["reason"] == "high error rate"
        assert kwargs["activated_by"] == "oncall"
        assert kwargs["duration_minutes"] == 30
        assert resp.status_code == 200
        assert resp.body["status"] == "activated"


# =============================================================================
# emergency_release
# =============================================================================


class TestEmergencyReleaseBehavior:
    def test_release_when_not_active_returns_400(self):
        manager = MagicMock()
        manager.get_state.return_value = _mock_state("NORMAL", is_active=False)
        with patch(
            "baldur_pro.services.emergency_mode.get_emergency_manager",
            return_value=manager,
        ):
            resp = emergency_release(_make_ctx(method="POST", json_body={}))
        assert resp.status_code == 400
        manager.deactivate.assert_not_called()

    def test_recovery_not_allowed_maps_to_409(self):
        """RecoveryNotAllowedError -> 409 Conflict."""
        from baldur_pro.services.emergency_mode.exceptions import (
            RecoveryNotAllowedError,
        )

        manager = MagicMock()
        manager.get_state.return_value = _mock_state("LEVEL_2", is_active=True)
        manager.deactivate.side_effect = RecoveryNotAllowedError("metrics not stable")
        with patch(
            "baldur_pro.services.emergency_mode.get_emergency_manager",
            return_value=manager,
        ):
            resp = emergency_release(
                _make_ctx(
                    method="POST",
                    json_body={"reason": "manual"},
                    user=SimpleNamespace(username="ops"),
                )
            )
        assert resp.status_code == 409
        assert resp.body["error"] == "recovery_blocked"
        assert "hint" in resp.body

    def test_force_flag_forwarded_to_manager(self):
        manager = MagicMock()
        manager.get_state.return_value = _mock_state("LEVEL_2", is_active=True)
        manager.deactivate.return_value = _mock_state("NORMAL", is_active=False)
        with patch(
            "baldur_pro.services.emergency_mode.get_emergency_manager",
            return_value=manager,
        ):
            resp = emergency_release(
                _make_ctx(
                    method="POST",
                    json_body={"force": True, "reason": "override"},
                    user=SimpleNamespace(username="ops"),
                )
            )
        _, kwargs = manager.deactivate.call_args
        assert kwargs["force"] is True
        assert resp.body["forced"] is True


# =============================================================================
# gradual_recovery_start / gradual_recovery_stop
# =============================================================================


class TestGradualRecoveryStartBehavior:
    def test_invalid_target_level_returns_400(self):
        with patch(
            "baldur_pro.services.emergency_mode.get_emergency_manager"
        ) as mock_get:
            resp = gradual_recovery_start(
                _make_ctx(method="POST", json_body={"target_level": "BOGUS"})
            )
        assert resp.status_code == 400
        mock_get.assert_not_called()

    def test_emergency_state_error_maps_to_400(self):
        """EmergencyStateError during start -> 400."""
        from baldur_pro.services.emergency_mode.exceptions import (
            EmergencyStateError,
        )

        manager = MagicMock()
        manager.start_gradual_recovery.side_effect = EmergencyStateError(
            "no active emergency"
        )
        with patch(
            "baldur_pro.services.emergency_mode.get_emergency_manager",
            return_value=manager,
        ):
            resp = gradual_recovery_start(_make_ctx(method="POST", json_body={}))
        assert resp.status_code == 400
        assert resp.body["error"] == "invalid_request"

    def test_target_level_defaults_to_normal(self):
        EmergencyLevel = _get_level_enum()
        manager = MagicMock()
        manager.start_gradual_recovery.return_value = _mock_state("LEVEL_1")
        with patch(
            "baldur_pro.services.emergency_mode.get_emergency_manager",
            return_value=manager,
        ):
            resp = gradual_recovery_start(_make_ctx(method="POST", json_body={}))
        _, kwargs = manager.start_gradual_recovery.call_args
        assert kwargs["target_level"] == EmergencyLevel.NORMAL
        assert resp.status_code == 200


class TestGradualRecoveryStopBehavior:
    def test_invokes_stop_gradual_recovery(self):
        manager = MagicMock()
        manager.stop_gradual_recovery.return_value = _mock_state("LEVEL_2")
        with patch(
            "baldur_pro.services.emergency_mode.get_emergency_manager",
            return_value=manager,
        ):
            resp = gradual_recovery_stop(
                _make_ctx(
                    method="POST",
                    json_body={"reason": "manual"},
                    user=SimpleNamespace(username="ops"),
                )
            )
        manager.stop_gradual_recovery.assert_called_once_with(
            stopped_by="ops", reason="manual"
        )
        assert resp.status_code == 200


# =============================================================================
# emergency_history / emergency_config_get / emergency_config_update
# =============================================================================


class TestEmergencyHistoryBehavior:
    def test_default_limit_is_50(self):
        manager = MagicMock()
        manager.get_history.return_value = []
        with patch(
            "baldur_pro.services.emergency_mode.get_emergency_manager",
            return_value=manager,
        ):
            emergency_history(_make_ctx())
        manager.get_history.assert_called_once_with(limit=50)

    def test_limit_query_param_forwarded(self):
        manager = MagicMock()
        manager.get_history.return_value = []
        with patch(
            "baldur_pro.services.emergency_mode.get_emergency_manager",
            return_value=manager,
        ):
            emergency_history(_make_ctx(query={"limit": "10"}))
        manager.get_history.assert_called_once_with(limit=10)

    def test_non_numeric_limit_falls_back_to_50(self):
        manager = MagicMock()
        manager.get_history.return_value = []
        with patch(
            "baldur_pro.services.emergency_mode.get_emergency_manager",
            return_value=manager,
        ):
            emergency_history(_make_ctx(query={"limit": "abc"}))
        manager.get_history.assert_called_once_with(limit=50)


class TestEmergencyConfigBehavior:
    def test_config_get_returns_dict(self):
        manager = MagicMock()
        config = MagicMock()
        config.to_dict.return_value = {"stabilization_period_seconds": 300}
        manager.get_recovery_gate_config.return_value = config
        with patch(
            "baldur_pro.services.emergency_mode.get_emergency_manager",
            return_value=manager,
        ):
            resp = emergency_config_get(_make_ctx())
        assert resp.status_code == 200
        assert resp.body["config"]["stabilization_period_seconds"] == 300

    def test_config_update_forwards_actor_and_config(self):
        manager = MagicMock()

        mock_config_class = MagicMock()
        mock_config_instance = MagicMock()
        mock_config_instance.to_dict.return_value = {
            "stabilization_period_seconds": 600
        }
        mock_config_class.from_dict.return_value = mock_config_instance

        with (
            patch(
                "baldur_pro.services.emergency_mode.get_emergency_manager",
                return_value=manager,
            ),
            patch(
                "baldur.models.recovery.RecoveryGateConfig",
                mock_config_class,
            ),
        ):
            resp = emergency_config_update(
                _make_ctx(
                    method="PUT",
                    json_body={"stabilization_period_seconds": 600},
                    user=SimpleNamespace(username="admin"),
                )
            )

        mock_config_class.from_dict.assert_called_once_with(
            {"stabilization_period_seconds": 600}
        )
        manager.set_recovery_gate_config.assert_called_once_with(
            mock_config_instance, changed_by="admin"
        )
        assert resp.status_code == 200
        assert resp.body["changed_by"] == "admin"

    def test_config_update_invalid_body_returns_400(self):
        """Malformed body (from_dict raises) -> 400, not 500 with stack trace."""
        manager = MagicMock()

        mock_config_class = MagicMock()
        mock_config_class.from_dict.side_effect = TypeError(
            "stabilization_period_seconds must be int"
        )

        with (
            patch(
                "baldur_pro.services.emergency_mode.get_emergency_manager",
                return_value=manager,
            ),
            patch(
                "baldur.models.recovery.RecoveryGateConfig",
                mock_config_class,
            ),
        ):
            resp = emergency_config_update(
                _make_ctx(
                    method="PUT",
                    json_body={"stabilization_period_seconds": "not-an-int"},
                    user=SimpleNamespace(username="admin"),
                )
            )

        assert resp.status_code == 400
        assert resp.body["success"] is False
        assert resp.body["error"] == "invalid_config"
        assert "stabilization_period_seconds" in resp.body["message"]
        manager.set_recovery_gate_config.assert_not_called()
