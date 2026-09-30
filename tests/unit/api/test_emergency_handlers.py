"""Unit tests for framework-agnostic Emergency Mode handlers (429 PR3-phase2a).

Target: ``baldur.api.handlers.emergency`` — status, trigger/release,
gradual recovery, history, config, levels.

Verification techniques applied (§8):
  - §8.2 Exception/edge cases — invalid level, NORMAL trigger, missing fields
  - §8.4 Side effects — manager state transitions
  - §8.5 Dependency interaction — error mapping (RecoveryNotAllowedError → 409,
    SystemControlStoreError → 503 with ``persisted`` false / null)
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, create_autospec, patch

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
from baldur.core.exceptions import SystemControlStoreError
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
        # A bare exception carries neither field: the body still has both.
        assert resp.body["check_reason"] == ""
        assert resp.body["open_breakers"] == []

    def test_409_body_names_the_open_breakers_and_the_gated_exit(self):
        """The refusal's own sentence and names reach the caller verbatim.

        Negative: the old steer to ``force=true`` as the only way out is gone
        — the hint names the gradual recovery beside the force.
        """
        from baldur_pro.services.emergency_mode.exceptions import (
            RecoveryNotAllowedError,
        )
        from baldur_pro.services.emergency_mode.manager import (
            GracefulDegradationManager,
        )

        check_reason = "Error rate too high: 0.210 > 0.05; open: db, cache"
        manager = MagicMock(spec=GracefulDegradationManager)
        manager.get_state.return_value = _mock_state("LEVEL_3", is_active=True)
        manager.deactivate.side_effect = RecoveryNotAllowedError(
            f"Recovery not allowed: {check_reason}",
            check_reason=check_reason,
            open_breakers=("db", "cache"),
        )
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
        assert resp.body["check_reason"] == check_reason
        assert resp.body["open_breakers"] == ["db", "cache"]
        assert isinstance(resp.body["open_breakers"], list)
        assert "gradual recovery" in resp.body["hint"]
        assert "force=true" in resp.body["hint"]
        assert "Use force=true to override the recovery gate." not in resp.body["hint"]

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


# =============================================================================
# 802 D9 — an emergency change the store did not confirm answers 503
# =============================================================================


def _manager_double(**state_fields):
    from baldur_pro.services.emergency_mode import GracefulDegradationManager
    from baldur_pro.services.emergency_mode.models import EmergencyState

    manager = create_autospec(GracefulDegradationManager, instance=True)
    state = EmergencyState(**state_fields)
    manager.get_state.return_value = state
    manager.activate_manual.return_value = state
    manager.start_gradual_recovery.return_value = state
    manager.stop_gradual_recovery.return_value = state
    return manager


def _call(handler, manager, json_body=None, method="POST"):
    with patch(
        "baldur_pro.services.emergency_mode.get_emergency_manager",
        return_value=manager,
    ):
        return handler(_make_ctx(method=method, json_body=json_body))


_CHANGES = [
    (emergency_trigger, "activate_manual", {"level": "LEVEL_2", "reason": "spike"}),
    (emergency_release, "deactivate", {"reason": "recovered", "force": True}),
    (gradual_recovery_start, "start_gradual_recovery", {"target_level": "NORMAL"}),
    (gradual_recovery_stop, "stop_gradual_recovery", {"reason": "hold"}),
]
_CHANGE_IDS = ["trigger", "release", "recovery_start", "recovery_stop"]


class TestEmergencyHandlersBehavior:
    """Every emergency change says whether the store holds it."""

    @pytest.mark.parametrize(
        ("handler", "method", "json_body"), _CHANGES, ids=_CHANGE_IDS
    )
    @pytest.mark.parametrize("persisted", [False, None], ids=["not_applied", "unknown"])
    def test_unconfirmed_change_answers_503_in_force_nowhere(
        self, handler, method, json_body, persisted
    ):
        """``persisted`` false / null, ``applies: none``, HTTP 503."""
        EmergencyLevel = _get_level_enum()
        manager = _manager_double(level=EmergencyLevel.LEVEL_3, is_active=True)
        getattr(manager, method).side_effect = SystemControlStoreError(
            change=method, persisted=persisted, applies="none"
        )

        resp = _call(handler, manager, json_body)

        assert resp.status_code == 503
        assert resp.body["error"] == "state_store_unavailable"
        assert (resp.body["persisted"], resp.body["applies"]) == (persisted, "none")

    @pytest.mark.parametrize(
        ("handler", "method", "json_body"), _CHANGES, ids=_CHANGE_IDS
    )
    def test_committed_change_answers_200_with_persisted_true(
        self, handler, method, json_body
    ):
        """A change the store confirmed says so."""
        EmergencyLevel = _get_level_enum()
        manager = _manager_double(level=EmergencyLevel.LEVEL_2, is_active=True)

        resp = _call(handler, manager, json_body)

        assert resp.status_code == 200
        assert resp.body["persisted"] is True
        getattr(manager, method).assert_called_once()

    def test_status_carries_this_process_read_health(self):
        """The level's read health reaches the operator."""
        manager = _manager_double()
        manager.get_refresh_status.return_value = {
            "store_reachable": False,
            "state_refreshed_at": None,
            "state_age_seconds": None,
            "last_store_error": "ConnectionError: down",
            "refresher_running": True,
        }

        body = _call(emergency_status, manager, method="GET").body

        assert body["store_reachable"] is False
        assert body["last_store_error"] == "ConnectionError: down"
        assert body["refresher_running"] is True

    def test_status_from_a_manager_without_read_health_omits_the_fields(self):
        """An older PRO manager (no ``get_refresh_status``) still answers."""
        manager = create_autospec(_LegacyEmergencyManager, instance=True)
        manager.get_state.return_value = _manager_double().get_state.return_value

        body = _call(emergency_status, manager, method="GET").body

        assert "store_reachable" not in body
        assert body["level"] == _get_level_enum().NORMAL.value

    def test_status_whose_read_health_raises_omits_the_fields(self):
        """A failing health read never fails the status request."""
        manager = _manager_double()
        manager.get_refresh_status.side_effect = RuntimeError("refresher broken")

        resp = _call(emergency_status, manager, method="GET")

        assert resp.status_code == 200
        assert "store_reachable" not in resp.body


class _LegacyEmergencyManager:
    """The status surface of a PRO manager from before read health existed."""

    def get_state(self):  # pragma: no cover - interface only
        raise NotImplementedError
