"""Unit tests for framework-agnostic system control handlers (429 PR3-phase2a).

Target: ``baldur.api.handlers.system_control`` — status, enable/disable,
dry-run enable/disable. Pure functions (RequestContext → ResponseContext).

Verification techniques applied (§8):
  - §8.2 Exception/edge cases — missing reason / missing confirm → 400
  - §8.4 Side effects — manager state transitions (enable/disable/dry_run)
  - §8.5 Dependency interaction — get_system_control mock argument forwarding
  - §8.12 Branch outcome (802 D9) — committed → 200; held → 503 in this
    process; not applied / unknown release → 503 ``persisted`` false / null
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, create_autospec, patch

import pytest

from baldur.api.handlers import system_control as handlers_module
from baldur.api.handlers.system_control import (
    dry_run_disable,
    dry_run_enable,
    system_disable,
    system_enable,
    system_status,
)
from baldur.core.exceptions import SystemControlStoreError
from baldur.core.state_backend import (
    FileStateBackend,
    configure_state_backend,
    reset_state_backend,
)
from baldur.interfaces.web_framework import HttpMethod, RequestContext
from baldur.services.system_control import (
    SYSTEM_CONTROL_REFRESH_INTERVAL_SECONDS,
    SystemControlChange,
    SystemControlManager,
    SystemState,
    get_system_control,
)
from baldur.settings.system_control import reset_system_control_settings


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


def _committed(state: MagicMock) -> SystemControlChange:
    """A flip outcome the store confirmed."""
    return SystemControlChange(state=state, persisted=True, applies="everywhere")


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


class TestSystemStatusPersistDirtyContract:
    """The status response exposes whether this node diverged from the backend."""

    def _status_body(self, persist_dirty: bool) -> dict:
        from baldur.services.system_control import SystemControlManager

        manager = MagicMock(spec=SystemControlManager)
        manager.get_state.return_value = _mock_state(enabled=False)
        manager.get_backend_info.return_value = {"type": "redis"}
        manager.is_persist_dirty.return_value = persist_dirty

        with patch(
            "baldur.api.handlers.system_control.get_system_control",
            return_value=manager,
        ):
            return system_status(_make_ctx()).body

    def test_status_reports_persist_dirty_true_when_the_last_write_failed(self):
        """A dirty node still serves its own flip while other nodes read the
        pre-flip value - the operator needs to see that from the status page."""
        assert self._status_body(True)["persist_dirty"] is True

    def test_status_reports_persist_dirty_false_when_the_state_is_persisted(self):
        """Negative twin: the field is sourced from the manager, not a constant."""
        assert self._status_body(False)["persist_dirty"] is False


# =============================================================================
# system_enable / system_disable
# =============================================================================


class TestSystemEnableBehavior:
    """system_enable() forwards actor/reason to manager.enable()."""

    def test_enable_invokes_manager_with_actor_and_reason(self):
        """Body.reason + ctx.user flows into manager.enable()."""
        manager = MagicMock()
        manager.enable.return_value = _committed(_mock_state(enabled=True))

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
        assert resp.body["persisted"] is True
        assert resp.body["applies"] == "everywhere"

    def test_enable_the_store_did_not_confirm_answers_503(self):
        """A re-enable that did not reach the store answers 503 with where it applies."""
        manager = MagicMock()
        manager.enable.side_effect = SystemControlStoreError(
            change="enable", persisted=False, applies="none", withdrew_held_change=True
        )
        manager.get_state.return_value = _mock_state(enabled=False)

        with patch(
            "baldur.api.handlers.system_control.get_system_control",
            return_value=manager,
        ):
            resp = system_enable(_make_ctx(method="POST", json_body={}))

        assert resp.status_code == 503
        assert resp.body["persisted"] is False
        assert resp.body["applies"] == "none"
        assert resp.body["withdrew_held_change"] is True

    def test_enable_defaults_reason_to_empty_string(self):
        """Empty body -> reason=''."""
        manager = MagicMock()
        manager.enable.return_value = _committed(_mock_state(enabled=True))

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
        manager.disable.return_value = _committed(_mock_state(enabled=False))

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
        assert "effect" in resp.body
        assert "All baldur operations are now stopped" not in str(resp.body)

    def test_held_disable_answers_503_this_process(self):
        """A kill switch held in this process only answers 503 and says so."""
        manager = MagicMock()
        manager.disable.return_value = SystemControlChange(
            state=_mock_state(enabled=False), persisted=False, applies="this_process"
        )

        with patch(
            "baldur.api.handlers.system_control.get_system_control",
            return_value=manager,
        ):
            resp = system_disable(
                _make_ctx(method="POST", json_body={"reason": "store down"})
            )

        assert resp.status_code == 503
        assert resp.body["persisted"] is False
        assert resp.body["applies"] == "this_process"


# =============================================================================
# dry_run_enable / dry_run_disable
# =============================================================================


class TestDryRunEnableBehavior:
    """dry_run_enable() — enables observation-only mode."""

    def test_enable_invokes_manager_with_actor(self):
        manager = MagicMock()
        manager.enable_dry_run.return_value = _committed(_mock_state(dry_run=True))

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
        manager.disable_dry_run.return_value = _committed(_mock_state(dry_run=False))

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


# =============================================================================
# 802 D1, D9 — a change reports only the effect that happened
# =============================================================================


def _manager_double() -> SystemControlManager:
    manager = create_autospec(SystemControlManager, instance=True)
    manager.get_state.return_value = SystemState(enabled=False)
    return manager


def _call(handler, manager, json_body):
    with patch(
        "baldur.api.handlers.system_control.get_system_control",
        return_value=manager,
    ):
        return handler(_make_ctx(method="POST", json_body=json_body))


class TestSystemControlHandlersBehavior:
    """503 whenever the store did not confirm; the fields say what did happen."""

    @pytest.mark.parametrize(
        ("handler", "method", "json_body"),
        [
            (system_enable, "enable", {"reason": "resolved"}),
            (dry_run_disable, "disable_dry_run", {"confirm": True}),
        ],
        ids=["enable", "dry_run_disable"],
    )
    def test_release_with_an_unknown_outcome_answers_503_with_persisted_null(
        self, handler, method, json_body
    ):
        """``persisted: null``, ``applies: none`` and the withdrawal fields."""
        manager = _manager_double()
        getattr(manager, method).side_effect = SystemControlStoreError(
            change=method,
            persisted=None,
            applies="none",
            withdrew_held_change=True,
            may_still_land=True,
        )

        resp = _call(handler, manager, json_body)

        assert resp.status_code == 503
        assert resp.body["error"] == "state_store_unavailable"
        assert (resp.body["persisted"], resp.body["applies"]) == (None, "none")
        assert resp.body["withdrew_held_change"] is True
        assert resp.body["may_still_land"] is True
        assert resp.body["state"]["enabled"] is False
        manager.get_state.assert_called_once_with(refresh=False)

    @pytest.mark.parametrize(
        ("handler", "method", "json_body"),
        [
            (system_disable, "disable", {"reason": "incident"}),
            (dry_run_enable, "enable_dry_run", None),
        ],
        ids=["disable", "dry_run_enable"],
    )
    @pytest.mark.parametrize("persisted", [False, None], ids=["not_applied", "unknown"])
    def test_held_change_answers_503_in_force_in_this_process_only(
        self, handler, method, json_body, persisted
    ):
        """A held brake is reported as held — never as a fleet-wide success."""
        manager = _manager_double()
        getattr(manager, method).return_value = SystemControlChange(
            state=SystemState(enabled=False, dry_run=True),
            persisted=persisted,
            applies="this_process",
        )

        resp = _call(handler, manager, json_body)

        assert resp.status_code == 503
        assert resp.body["success"] is False
        assert resp.body["message"] == handlers_module._HELD_MESSAGE
        assert (resp.body["persisted"], resp.body["applies"]) == (
            persisted,
            "this_process",
        )

    def test_committed_disable_states_the_step_aside_effect_and_the_reach(self):
        """The effect text and the reach bound — no claim that everything stopped."""
        manager = _manager_double()
        manager.disable.return_value = SystemControlChange(
            state=SystemState(enabled=False),
            persisted=True,
            applies="everywhere",
        )

        resp = _call(system_disable, manager, {"reason": "incident"})

        assert resp.status_code == 200
        assert resp.body["effect"] == handlers_module._DISABLED_EFFECT
        assert (
            f"{SYSTEM_CONTROL_REFRESH_INTERVAL_SECONDS:g} seconds" in resp.body["reach"]
        )
        assert "operations are now stopped" not in str(resp.body)

    def test_disabled_effect_names_what_steps_aside_and_what_stays(self):
        """The operator-facing sentence of the one stated effect (D1)."""
        effect = handlers_module._DISABLED_EFFECT

        for steps_aside in ("no retry", "no DLQ capture", "no rate-limit force-open"):
            assert steps_aside in effect
        for stays in ("Fallbacks", "timeouts", "idempotency", "Blocks stay in force"):
            assert stays in effect

    def test_status_spreads_this_process_read_health(self):
        """The refresh status fields reach the operator verbatim."""
        manager = create_autospec(SystemControlManager, instance=True)
        manager.get_state.return_value = SystemState()
        manager.is_persist_dirty.return_value = False
        manager.get_backend_info.return_value = {"backend_type": "RedisStateBackend"}
        manager.get_refresh_status.return_value = {
            "store_reachable": False,
            "state_refreshed_at": "2026-09-30T12:00:00+00:00",
            "state_age_seconds": 42.0,
            "last_store_error": "ConnectionError: down",
            "refresher_running": True,
        }

        with patch(
            "baldur.api.handlers.system_control.get_system_control",
            return_value=manager,
        ):
            body = system_status(_make_ctx()).body

        assert body["store_reachable"] is False
        assert body["state_age_seconds"] == 42.0
        assert body["last_store_error"] == "ConnectionError: down"
        assert body["refresher_running"] is True


class TestSystemControlBackendInfoBehavior:
    """The status names the store in use — for a file store, its absolute directory."""

    @pytest.fixture(autouse=True)
    def _isolated_store(self):
        reset_state_backend()
        reset_system_control_settings()
        yield
        reset_state_backend()
        reset_system_control_settings()

    def test_status_names_the_resolved_file_store_directory(self, tmp_path):
        """Two processes started from different directories can see two stores."""
        configure_state_backend(FileStateBackend(tmp_path / "state"))

        body = system_status(_make_ctx()).body

        assert body["backend"]["backend_type"] == "FileStateBackend"
        assert body["backend"]["directory"] == str((tmp_path / "state").resolve())

    def test_unbuildable_file_store_still_names_its_directory_and_error(
        self, tmp_path, monkeypatch
    ):
        """An unwritable directory is visible: the variable's value, resolved."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("BALDUR_SYSTEM_CONTROL_BACKEND", "file")
        monkeypatch.setenv("BALDUR_SYSTEM_CONTROL_DIR", "relative/state")
        reset_system_control_settings()

        with patch(
            "baldur.services.system_control.get_state_backend",
            side_effect=PermissionError("read-only file system"),
        ):
            info = get_system_control().get_backend_info()

        assert info == {
            "backend_type": "file",
            "error": "PermissionError: read-only file system",
            "directory": str((tmp_path / "relative" / "state").resolve()),
        }
