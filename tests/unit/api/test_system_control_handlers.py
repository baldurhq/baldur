"""Unit tests for framework-agnostic system control handlers (429 PR3-phase2a).

Target: ``baldur.api.handlers.system_control`` — status, enable/disable,
dry-run enable/disable. Pure functions (RequestContext → ResponseContext).

Verification techniques applied (§8):
  - §8.2 Exception/edge cases — missing reason / missing confirm → 400
  - §8.4 Side effects — manager state transitions (enable/disable/dry_run)
  - §8.5 Dependency interaction — get_system_control mock argument forwarding
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from baldur.api.handlers.system_control import (
    dry_run_disable,
    dry_run_enable,
    system_disable,
    system_enable,
    system_status,
)
from baldur.interfaces.web_framework import HttpMethod, RequestContext


def _make_ctx(
    method="GET", path="/test/", query=None, path_params=None, json_body=None, user=None
):
    return RequestContext(
        method=HttpMethod(method),
        path=path,
        query_params=query or {},
        path_params=path_params or {},
        json_body=json_body,
        user=user,
    )


def _mock_state(
    enabled: bool = True,
    dry_run: bool = False,
    **extra,
) -> MagicMock:
    """Build a manager.get_state() / enable() / disable() return-value stub."""
    state = MagicMock()
    state.enabled = enabled
    state.dry_run = dry_run
    state.to_dict.return_value = {
        "enabled": enabled,
        "dry_run": dry_run,
        **extra,
    }
    return state


# =============================================================================
# system_status
# =============================================================================


class TestSystemStatusBehavior:
    """system_status() — read-only snapshot composition."""

    def test_status_enabled_when_manager_reports_enabled(self):
        """state.enabled=True -> status='enabled'."""
        manager = MagicMock()
        manager.get_state.return_value = _mock_state(enabled=True)
        manager.get_backend_info.return_value = {"type": "memory"}

        with patch(
            "baldur.api.handlers.system_control.get_system_control",
            return_value=manager,
        ):
            resp = system_status(_make_ctx())

        assert resp.status_code == 200
        assert resp.body["status"] == "enabled"
        assert resp.body["system"] == "baldur"
        assert resp.body["backend"] == {"type": "memory"}

    def test_status_disabled_when_manager_reports_disabled(self):
        """state.enabled=False -> status='disabled'."""
        manager = MagicMock()
        manager.get_state.return_value = _mock_state(enabled=False)
        manager.get_backend_info.return_value = {"type": "redis"}

        with patch(
            "baldur.api.handlers.system_control.get_system_control",
            return_value=manager,
        ):
            resp = system_status(_make_ctx())

        assert resp.body["status"] == "disabled"


# =============================================================================
# system_enable / system_disable
# =============================================================================


class TestSystemEnableBehavior:
    """system_enable() forwards actor/reason to manager.enable()."""

    def test_enable_invokes_manager_with_actor_and_reason(self):
        """Body.reason + ctx.user flows into manager.enable()."""
        manager = MagicMock()
        manager.enable.return_value = _mock_state(enabled=True)

        with patch(
            "baldur.api.handlers.system_control.get_system_control",
            return_value=manager,
        ):
            resp = system_enable(
                _make_ctx(
                    method="POST",
                    json_body={"reason": "maintenance done"},
                    user=SimpleNamespace(username="bob"),
                )
            )

        manager.enable.assert_called_once_with(actor="bob", reason="maintenance done")
        assert resp.status_code == 200
        assert resp.body["success"] is True

    def test_enable_defaults_reason_to_empty_string(self):
        """Empty body -> reason=''."""
        manager = MagicMock()
        manager.enable.return_value = _mock_state(enabled=True)

        with patch(
            "baldur.api.handlers.system_control.get_system_control",
            return_value=manager,
        ):
            system_enable(_make_ctx(method="POST", json_body=None))

        _, kwargs = manager.enable.call_args
        assert kwargs["reason"] == ""


class TestSystemDisableBehavior:
    """system_disable() — kill switch with mandatory reason."""

    def test_missing_reason_returns_400(self):
        """No reason -> 400 without invoking manager."""
        manager = MagicMock()

        with patch(
            "baldur.api.handlers.system_control.get_system_control",
            return_value=manager,
        ):
            resp = system_disable(_make_ctx(method="POST", json_body={"reason": ""}))

        assert resp.status_code == 400
        assert resp.body["success"] is False
        manager.disable.assert_not_called()

    def test_missing_body_returns_400(self):
        """None body -> 400."""
        manager = MagicMock()

        with patch(
            "baldur.api.handlers.system_control.get_system_control",
            return_value=manager,
        ):
            resp = system_disable(_make_ctx(method="POST", json_body=None))

        assert resp.status_code == 400
        manager.disable.assert_not_called()

    def test_valid_reason_invokes_manager_disable(self):
        """Reason present -> manager.disable() called with actor+reason."""
        manager = MagicMock()
        manager.disable.return_value = _mock_state(enabled=False)

        with patch(
            "baldur.api.handlers.system_control.get_system_control",
            return_value=manager,
        ):
            resp = system_disable(
                _make_ctx(
                    method="POST",
                    json_body={"reason": "emergency"},
                    user=SimpleNamespace(username="eve"),
                )
            )

        manager.disable.assert_called_once_with(actor="eve", reason="emergency")
        assert resp.status_code == 200
        assert resp.body["success"] is True
        assert "warning" in resp.body


# =============================================================================
# dry_run_enable / dry_run_disable
# =============================================================================


class TestDryRunEnableBehavior:
    """dry_run_enable() — enables observation-only mode."""

    def test_enable_invokes_manager_with_actor(self):
        manager = MagicMock()
        manager.enable_dry_run.return_value = _mock_state(dry_run=True)

        with patch(
            "baldur.api.handlers.system_control.get_system_control",
            return_value=manager,
        ):
            resp = dry_run_enable(
                _make_ctx(method="POST", user=SimpleNamespace(username="carol"))
            )

        manager.enable_dry_run.assert_called_once_with(actor="carol")
        assert resp.status_code == 200


class TestDryRunDisableBehavior:
    """dry_run_disable() requires explicit confirmation to go LIVE."""

    def test_missing_confirm_returns_400(self):
        """confirm=false -> 400 without invoking manager."""
        manager = MagicMock()

        with patch(
            "baldur.api.handlers.system_control.get_system_control",
            return_value=manager,
        ):
            resp = dry_run_disable(
                _make_ctx(method="POST", json_body={"confirm": False})
            )

        assert resp.status_code == 400
        manager.disable_dry_run.assert_not_called()

    def test_missing_body_returns_400(self):
        """None body -> 400."""
        manager = MagicMock()

        with patch(
            "baldur.api.handlers.system_control.get_system_control",
            return_value=manager,
        ):
            resp = dry_run_disable(_make_ctx(method="POST", json_body=None))

        assert resp.status_code == 400
        manager.disable_dry_run.assert_not_called()

    def test_confirm_true_invokes_manager(self):
        """confirm=true -> manager.disable_dry_run() called."""
        manager = MagicMock()
        manager.disable_dry_run.return_value = _mock_state(dry_run=False)

        with patch(
            "baldur.api.handlers.system_control.get_system_control",
            return_value=manager,
        ):
            resp = dry_run_disable(
                _make_ctx(
                    method="POST",
                    json_body={"confirm": True},
                    user=SimpleNamespace(username="dan"),
                )
            )

        manager.disable_dry_run.assert_called_once_with(actor="dan")
        assert resp.status_code == 200
        assert resp.body["success"] is True
