"""Canary rollout handlers — unit tests (523 Step 8).

Target: ``baldur.api.handlers.canary`` — framework-agnostic Canary Rollout
HTTP endpoints (list / create / detail / action / panic_rollback / metrics /
history) plus the private helpers ``_service``, ``_format_rollout_summary``,
``_format_rollout_detail``, and ``_execute_action``.

Verification techniques applied (§8):
  - §8.2 Exception/edge cases — missing fields, invalid action, missing
    rollout, settings-import failure fallback, panic-rollback per-rollout
    exception capture.
  - §8.4 Side effects — service.* method calls verified per action.
  - §8.5 Dependency interaction — ProviderRegistry slot mocked, baldur_pro
    Canary models imported at runtime.

The PRO ``CanaryRolloutService`` is replaced by a ``MagicMock``; the OSS
handlers under test are pure response-formatting + dispatch glue and do not
touch real state.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from baldur.api.handlers.canary import (
    VALID_ACTIONS,
    _execute_action,
    _format_rollout_detail,
    _format_rollout_summary,
    _service,
    canary_panic_rollback,
    canary_rollout_action,
    canary_rollout_create,
    canary_rollout_detail,
    canary_rollout_history,
    canary_rollout_list,
    canary_rollout_metrics,
)
from baldur.interfaces.web_framework import HttpMethod, RequestContext

# =============================================================================
# Fixtures
# =============================================================================


def _make_ctx(
    method: str = "GET",
    path: str = "/canary/",
    query: dict | None = None,
    path_params: dict | None = None,
    json_body: dict | None = None,
    user=None,
) -> RequestContext:
    return RequestContext(
        method=HttpMethod(method),
        path=path,
        query_params=query or {},
        path_params=path_params or {},
        json_body=json_body,
        user=user,
    )


def _make_state(value: str = "canary") -> SimpleNamespace:
    """Minimal stand-in for CanaryState enum value."""
    return SimpleNamespace(value=value)


def _make_stage(name: str = "stage_0") -> SimpleNamespace:
    return SimpleNamespace(
        name=name,
        clusters=["c1", "c2"],
        percentage=10.0,
        duration_minutes=5,
        auto_promote=True,
    )


def _make_rollout(
    rollout_id: str = "r1",
    state: str = "canary",
    config_type: str = "feature_flag",
    completed_at=None,
    current_stage: SimpleNamespace | None = None,
    stages: list | None = None,
) -> SimpleNamespace:
    """Lightweight rollout DTO replacing the PRO CanaryRollout."""
    return SimpleNamespace(
        id=rollout_id,
        config_type=config_type,
        state=_make_state(state),
        current_stage=current_stage,
        current_stage_index=0,
        stages=stages if stages is not None else [_make_stage()],
        affected_clusters=["c1", "c2"],
        created_by="alice",
        created_at=datetime(2026, 5, 20, tzinfo=UTC),
        completed_at=completed_at,
        previous_values={"k": 1},
        new_values={"k": 2},
        progress_percentage=10.0,
        is_terminal=False,
        reason="initial",
        rollback_reason=None,
    )


@pytest.fixture
def mock_service():
    """Default service with get_active_rollouts() returning one rollout."""
    svc = MagicMock(name="CanaryRolloutService")
    svc.get_active_rollouts.return_value = [_make_rollout()]
    svc.get_completed_rollouts.return_value = []
    svc.get_rollout.return_value = _make_rollout()
    return svc


@pytest.fixture
def wired_service(monkeypatch, mock_service):
    """Wire mock_service into the ProviderRegistry slot."""
    from baldur.factory.registry import ProviderRegistry

    monkeypatch.setattr(
        ProviderRegistry.canary_rollout_service,
        "safe_get",
        lambda *a, **kw: mock_service,
    )
    return mock_service


@pytest.fixture
def no_service(monkeypatch):
    """ProviderRegistry slot returns None — OSS-without-PRO branch."""
    from baldur.factory.registry import ProviderRegistry

    monkeypatch.setattr(
        ProviderRegistry.canary_rollout_service, "safe_get", lambda *a, **kw: None
    )


# =============================================================================
# _service / formatters
# =============================================================================


class TestServiceResolution:
    def test_returns_registered_service(self, wired_service):
        assert _service() is wired_service

    def test_raises_runtime_error_when_unregistered(self, no_service):
        with pytest.raises(RuntimeError, match="Canary handlers require baldur_pro"):
            _service()


class TestFormatRolloutSummary:
    def test_all_fields_with_current_stage(self):
        rollout = _make_rollout(current_stage=_make_stage("stage_now"))
        summary = _format_rollout_summary(rollout)
        assert summary["id"] == "r1"
        assert summary["state"] == "canary"
        assert summary["current_stage"] == "stage_now"
        assert summary["current_stage_index"] == 0
        assert summary["total_stages"] == 1
        assert summary["affected_clusters"] == ["c1", "c2"]
        assert summary["created_by"] == "alice"
        assert summary["progress_percentage"] == 10.0

    def test_current_stage_none_branch(self):
        rollout = _make_rollout(current_stage=None)
        summary = _format_rollout_summary(rollout)
        assert summary["current_stage"] is None


class TestFormatRolloutDetail:
    def test_all_fields_with_completed_at(self):
        completed_at = datetime(2026, 5, 21, tzinfo=UTC)
        rollout = _make_rollout(completed_at=completed_at)
        detail = _format_rollout_detail(rollout)
        assert detail["completed_at"] == completed_at.isoformat()
        assert detail["previous_values"] == {"k": 1}
        assert detail["new_values"] == {"k": 2}
        assert len(detail["stages"]) == 1
        assert detail["stages"][0]["name"] == "stage_0"
        assert detail["stages"][0]["percentage"] == 10.0

    def test_completed_at_none_branch(self):
        rollout = _make_rollout(completed_at=None)
        detail = _format_rollout_detail(rollout)
        assert detail["completed_at"] is None


# =============================================================================
# canary_rollout_list
# =============================================================================


class TestCanaryRolloutList:
    def test_default_returns_active_only(self, wired_service):
        ctx = _make_ctx()
        resp = canary_rollout_list(ctx)
        assert resp.status_code == 200
        assert resp.body["count"] == 1
        assert resp.body["status"] == "success"
        wired_service.get_completed_rollouts.assert_not_called()

    def test_include_completed_uses_settings_limit(self, wired_service):
        wired_service.get_completed_rollouts.return_value = [
            _make_rollout("r_done", state="completed")
        ]
        # Stub the settings import so we don't depend on real settings module
        fake_settings = SimpleNamespace(default_completed_rollouts_limit=42)
        with patch(
            "baldur.settings.canary.get_canary_settings",
            return_value=fake_settings,
            create=True,
        ):
            resp = canary_rollout_list(_make_ctx(query={"include_completed": "true"}))
        wired_service.get_completed_rollouts.assert_called_once_with(limit=42)
        assert resp.body["count"] == 2

    def test_include_completed_settings_import_failure_falls_back(self, wired_service):
        # Force the inner import to raise — limit must fall back to 20.
        with patch(
            "baldur.settings.canary.get_canary_settings",
            side_effect=ImportError,
            create=True,
        ):
            resp = canary_rollout_list(_make_ctx(query={"include_completed": "true"}))
        wired_service.get_completed_rollouts.assert_called_once_with(limit=20)
        assert resp.status_code == 200

    def test_config_type_filter_removes_non_matching(self, wired_service):
        wired_service.get_active_rollouts.return_value = [
            _make_rollout("r1", config_type="ff"),
            _make_rollout("r2", config_type="other"),
        ]
        resp = canary_rollout_list(_make_ctx(query={"config_type": "ff"}))
        assert resp.body["count"] == 1
        assert resp.body["rollouts"][0]["id"] == "r1"

    def test_service_not_registered_raises(self, no_service):
        with pytest.raises(RuntimeError):
            canary_rollout_list(_make_ctx())


# =============================================================================
# canary_rollout_create
# =============================================================================


class TestCanaryRolloutCreate:
    def test_missing_config_type_returns_400(self, wired_service):
        resp = canary_rollout_create(_make_ctx(method="POST", json_body={}))
        assert resp.status_code == 400
        assert "config_type" in resp.body["error"]

    def test_missing_new_values_returns_400(self, wired_service):
        resp = canary_rollout_create(
            _make_ctx(method="POST", json_body={"config_type": "ff"})
        )
        assert resp.status_code == 400
        assert "new_values" in resp.body["error"]

    def test_empty_stages_returns_400(self, wired_service):
        resp = canary_rollout_create(
            _make_ctx(
                method="POST",
                json_body={"config_type": "ff", "new_values": {"k": 1}},
            )
        )
        assert resp.status_code == 400
        assert "stage" in resp.body["error"].lower()

    def test_happy_path_creates_rollout(self, wired_service):
        # service.create_rollout returns a rollout that we then format
        created = _make_rollout("r_new")
        wired_service.create_rollout.return_value = created

        body = {
            "config_type": "feature_flag",
            "new_values": {"k": 2},
            "stages": [
                {
                    "name": "s1",
                    "clusters": ["c1"],
                    "percentage": "25",
                    "duration_minutes": "10",
                    "auto_promote": False,
                }
            ],
            "reason": "test rollout",
        }
        resp = canary_rollout_create(_make_ctx(method="POST", json_body=body))
        assert resp.status_code == 201
        assert resp.body["status"] == "success"
        assert resp.body["rollout"]["id"] == "r_new"

        kwargs = wired_service.create_rollout.call_args.kwargs
        assert kwargs["config_type"] == "feature_flag"
        # Stage parsed with numeric casts
        stage = kwargs["stages"][0]
        assert stage.percentage == 25.0
        assert stage.duration_minutes == 10
        assert stage.auto_promote is False
        assert kwargs["reason"] == "test rollout"
        assert kwargs["force_during_chaos"] is False
        assert kwargs["created_by"] == "anonymous"  # ctx.user is None

    def test_uses_default_stage_name_index(self, wired_service):
        wired_service.create_rollout.return_value = _make_rollout("r_new")
        body = {
            "config_type": "ff",
            "new_values": {"k": 1},
            "stages": [{}],  # No name / clusters / etc → all defaults
        }
        canary_rollout_create(_make_ctx(method="POST", json_body=body))
        stage = wired_service.create_rollout.call_args.kwargs["stages"][0]
        assert stage.name == "stage_0"
        assert stage.clusters == []
        assert stage.percentage == 0.0
        assert stage.duration_minutes == 5
        assert stage.auto_promote is True

    def test_actor_resolved_from_user(self, wired_service):
        wired_service.create_rollout.return_value = _make_rollout("r_new")
        user = SimpleNamespace(username="bob")
        body = {
            "config_type": "ff",
            "new_values": {"k": 1},
            "stages": [{"name": "s"}],
        }
        canary_rollout_create(_make_ctx(method="POST", json_body=body, user=user))
        assert wired_service.create_rollout.call_args.kwargs["created_by"] == "bob"

    def test_json_body_none_treated_as_empty(self, wired_service):
        # ctx.json_body=None → body falls back to {} → missing config_type 400.
        resp = canary_rollout_create(_make_ctx(method="POST", json_body=None))
        assert resp.status_code == 400

    def test_service_valueerror_returns_400_with_vocabulary(self, wired_service):
        """684 C2: the PRO service rejects an unknown config_type/field with a
        ValueError naming the offending value + valid vocabulary; the handler
        rides its existing ValueError→400 map and surfaces the message verbatim."""
        wired_service.create_rollout.side_effect = ValueError(
            "Unknown canary config_type 'bogus'. "
            "Valid config domains: ['circuit_breaker', 'retry']"
        )
        body = {
            "config_type": "bogus",
            "new_values": {"max_attempts": 5},
            "stages": [{"name": "s"}],
        }
        resp = canary_rollout_create(_make_ctx(method="POST", json_body=body))

        assert resp.status_code == 400
        assert "bogus" in resp.body["error"]
        assert "Valid config domains" in resp.body["error"]


# =============================================================================
# canary_rollout_detail
# =============================================================================


class TestCanaryRolloutDetail:
    def test_not_found(self, wired_service):
        wired_service.get_rollout.return_value = None
        resp = canary_rollout_detail(_make_ctx(path_params={"rollout_id": "x"}))
        assert resp.status_code == 404

    def test_happy_path(self, wired_service):
        resp = canary_rollout_detail(_make_ctx(path_params={"rollout_id": "r1"}))
        assert resp.status_code == 200
        assert resp.body["rollout"]["id"] == "r1"


# =============================================================================
# _execute_action (covers all 6 valid actions + unknown)
# =============================================================================


class TestExecuteAction:
    def test_start_success_returns_no_error(self, wired_service):
        wired_service.start_rollout.return_value = True
        rollout = _make_rollout()
        ok, err = _execute_action(wired_service, "r1", rollout, "start", {})
        assert ok is True
        assert err is None

    def test_start_failure_returns_state_in_message(self, wired_service):
        wired_service.start_rollout.return_value = False
        rollout = _make_rollout(state="paused")
        ok, err = _execute_action(wired_service, "r1", rollout, "start", {})
        assert ok is False
        assert "paused" in err

    def test_promote_propagates_force_flag(self, wired_service):
        wired_service.promote.return_value = True
        ok, err = _execute_action(
            wired_service, "r1", _make_rollout(), "promote", {"force": True}
        )
        wired_service.promote.assert_called_once_with("r1", force=True)
        assert ok
        assert err is None

    def test_promote_failure_message(self, wired_service):
        wired_service.promote.return_value = False
        ok, err = _execute_action(wired_service, "r1", _make_rollout(), "promote", {})
        assert ok is False
        assert "metrics" in err

    def test_rollback_uses_default_reason(self, wired_service):
        wired_service.rollback.return_value = True
        _execute_action(wired_service, "r1", _make_rollout(), "rollback", {})
        wired_service.rollback.assert_called_once_with("r1", reason="Manual rollback")

    def test_rollback_failure_message(self, wired_service):
        wired_service.rollback.return_value = False
        ok, err = _execute_action(
            wired_service, "r1", _make_rollout(), "rollback", {"reason": "x"}
        )
        assert ok is False
        assert "rolled back" in err
        assert "Cancel" in err

    def test_pause_success_and_failure(self, wired_service):
        wired_service.pause.return_value = True
        ok, err = _execute_action(wired_service, "r1", _make_rollout(), "pause", {})
        assert ok
        assert err is None
        wired_service.pause.return_value = False
        ok, err = _execute_action(wired_service, "r1", _make_rollout(), "pause", {})
        assert ok is False
        assert "CANARY" in err

    def test_resume_success_and_failure(self, wired_service):
        wired_service.resume.return_value = True
        ok, err = _execute_action(wired_service, "r1", _make_rollout(), "resume", {})
        assert ok
        assert err is None
        wired_service.resume.return_value = False
        ok, err = _execute_action(wired_service, "r1", _make_rollout(), "resume", {})
        assert ok is False
        assert "PAUSED" in err

    def test_cancel_success_and_failure(self, wired_service):
        wired_service.cancel.return_value = True
        ok, err = _execute_action(wired_service, "r1", _make_rollout(), "cancel", {})
        assert ok
        assert err is None
        wired_service.cancel.return_value = False
        ok, err = _execute_action(wired_service, "r1", _make_rollout(), "cancel", {})
        assert ok is False
        assert "cancelled" in err
        assert "Rollback" in err

    def test_unknown_action_returns_false_with_action_in_message(self):
        # No need for fixture; service unused on unknown path
        ok, err = _execute_action(MagicMock(), "r1", _make_rollout(), "weird", {})
        assert ok is False
        assert "weird" in err


# =============================================================================
# canary_rollout_action
# =============================================================================


class TestCanaryRolloutAction:
    def test_invalid_action_returns_400(self, wired_service):
        resp = canary_rollout_action(
            _make_ctx(
                method="POST",
                path_params={"rollout_id": "r1", "action": "explode"},
            )
        )
        assert resp.status_code == 400
        assert "Unknown action" in resp.body["error"]

    def test_rollout_not_found_returns_404(self, wired_service):
        wired_service.get_rollout.return_value = None
        resp = canary_rollout_action(
            _make_ctx(
                method="POST",
                path_params={"rollout_id": "missing", "action": "start"},
            )
        )
        assert resp.status_code == 404

    def test_action_success_returns_200(self, wired_service):
        wired_service.start_rollout.return_value = True
        # Second get_rollout call (for updated_rollout) returns a fresh state
        wired_service.get_rollout.side_effect = [
            _make_rollout(state="created"),
            _make_rollout(state="canary"),
        ]
        resp = canary_rollout_action(
            _make_ctx(
                method="POST",
                path_params={"rollout_id": "r1", "action": "start"},
                json_body={},
            )
        )
        assert resp.status_code == 200
        assert resp.body["action"] == "start"
        assert resp.body["rollout"]["state"] == "canary"

    def test_action_success_with_updated_rollout_missing(self, wired_service):
        wired_service.start_rollout.return_value = True
        # First get_rollout returns rollout; second returns None
        # (the rollout was deleted/swept between the two reads).
        wired_service.get_rollout.side_effect = [_make_rollout(), None]
        resp = canary_rollout_action(
            _make_ctx(
                method="POST",
                path_params={"rollout_id": "r1", "action": "start"},
                json_body={},
            )
        )
        assert resp.status_code == 200
        assert resp.body["rollout"] is None

    def test_action_failure_returns_400(self, wired_service):
        wired_service.start_rollout.return_value = False
        resp = canary_rollout_action(
            _make_ctx(
                method="POST",
                path_params={"rollout_id": "r1", "action": "start"},
                json_body={},
            )
        )
        assert resp.status_code == 400
        # Default message includes state info from the rollout
        assert "Cannot start" in resp.body["error"]

    def test_action_failure_default_message_when_none(self, wired_service):
        # Force _execute_action to return (False, None) via unknown action path
        # — but the public dispatcher already pre-filters unknown actions.
        # Instead, exercise the "default to 'Action failed'" path by faking
        # _execute_action directly.
        with patch(
            "baldur.api.handlers.canary._execute_action",
            return_value=(False, None),
        ):
            resp = canary_rollout_action(
                _make_ctx(
                    method="POST",
                    path_params={"rollout_id": "r1", "action": "start"},
                    json_body={},
                )
            )
        assert resp.status_code == 400
        assert resp.body["error"] == "Action failed"

    def test_valid_actions_constant(self):
        assert set(VALID_ACTIONS) == {
            "start",
            "promote",
            "rollback",
            "pause",
            "resume",
            "cancel",
        }


# =============================================================================
# canary_panic_rollback
# =============================================================================


class TestCanaryPanicRollback:
    def test_no_active_rollouts(self, wired_service):
        wired_service.get_active_rollouts.return_value = []
        resp = canary_panic_rollback(_make_ctx(method="POST"))
        assert resp.status_code == 200
        assert resp.body["rolled_back"] == []
        assert "No active rollouts" in resp.body["message"]

    def test_rollback_all_success(self, wired_service):
        wired_service.get_active_rollouts.return_value = [
            _make_rollout("r1"),
            _make_rollout("r2"),
        ]
        wired_service.rollback.return_value = True
        resp = canary_panic_rollback(
            _make_ctx(method="POST", json_body={"reason": "fire"})
        )
        assert resp.status_code == 200
        assert len(resp.body["rolled_back"]) == 2
        assert "2/2 successful" in resp.body["message"]
        # Reason gets [PANIC] prefix
        wired_service.rollback.assert_called_with("r2", reason="[PANIC] fire")

    def test_per_rollout_exception_captured(self, wired_service):
        wired_service.get_active_rollouts.return_value = [
            _make_rollout("r1"),
            _make_rollout("r2"),
        ]
        wired_service.rollback.side_effect = [True, RuntimeError("kaboom")]
        resp = canary_panic_rollback(_make_ctx(method="POST"))
        assert resp.status_code == 200
        rolled = resp.body["rolled_back"]
        assert rolled[0]["success"] is True
        assert rolled[1]["success"] is False
        assert "kaboom" in rolled[1]["error"]
        # Only the successful one counted
        assert "1/2 successful" in resp.body["message"]

    def test_default_reason_uses_actor(self, wired_service):
        wired_service.get_active_rollouts.return_value = [_make_rollout("r1")]
        wired_service.rollback.return_value = True
        canary_panic_rollback(
            _make_ctx(method="POST", user=SimpleNamespace(username="alice"))
        )
        wired_service.rollback.assert_called_once_with(
            "r1", reason="[PANIC] Panic rollback by alice"
        )

    def test_emergency_code_propagates_to_response(self, wired_service):
        # emergency_code is included on the rolled-back-summary response
        # (the no-active branch returns a separate short-circuit body that
        # intentionally drops it because nothing happened).
        wired_service.get_active_rollouts.return_value = [_make_rollout("r1")]
        wired_service.rollback.return_value = True
        resp = canary_panic_rollback(
            _make_ctx(method="POST", json_body={"emergency_code": "E-42"})
        )
        assert resp.body["emergency_code"] == "E-42"

    def test_created_rollout_cancelled_not_rolled_back(self, wired_service):
        """A never-started (CREATED) rollout is cancelled, not rolled back —
        rollback() would force an off-state-machine CREATED -> ROLLED_BACK and
        leave a spurious terminal for a rollout that touched no cluster."""
        from baldur.models.canary import CanaryState

        created = _make_rollout("r_created")
        created.state = CanaryState.CREATED  # real enum so the == check fires
        started = _make_rollout("r_started")  # SimpleNamespace state "canary"
        wired_service.get_active_rollouts.return_value = [created, started]
        wired_service.cancel.return_value = True
        wired_service.rollback.return_value = True

        resp = canary_panic_rollback(
            _make_ctx(method="POST", json_body={"reason": "fire"})
        )

        assert resp.status_code == 200
        wired_service.cancel.assert_called_once_with("r_created", reason="[PANIC] fire")
        wired_service.rollback.assert_called_once_with(
            "r_started", reason="[PANIC] fire"
        )
        by_id = {r["id"]: r for r in resp.body["rolled_back"]}
        assert by_id["r_created"]["action"] == "cancelled"
        assert by_id["r_started"]["action"] == "rolled_back"
        assert "2/2 successful" in resp.body["message"]


# =============================================================================
# canary_rollout_metrics
# =============================================================================


class TestCanaryRolloutMetrics:
    def _metric(self):
        return SimpleNamespace(
            cluster="c1",
            stage_name="stage_0",
            error_rate_before=0.01,
            error_rate_after=0.02,
            latency_p50_before=10.0,
            latency_p50_after=12.0,
            latency_p99_before=50.0,
            latency_p99_after=55.0,
            requests_total=1000,
            errors_total=10,
            is_healthy=True,
            unhealthy_reason=None,
        )

    def test_rollout_not_found(self, wired_service):
        wired_service.get_rollout.return_value = None
        resp = canary_rollout_metrics(_make_ctx(path_params={"rollout_id": "missing"}))
        assert resp.status_code == 404

    def test_with_metrics(self, wired_service):
        wired_service.get_rollout.return_value = _make_rollout(
            current_stage=_make_stage("s0")
        )
        wired_service.collect_metrics.return_value = [self._metric()]
        resp = canary_rollout_metrics(_make_ctx(path_params={"rollout_id": "r1"}))
        assert resp.status_code == 200
        assert resp.body["state"] == "canary"
        assert resp.body["current_stage"] == "s0"
        assert len(resp.body["metrics"]) == 1
        assert resp.body["metrics"][0]["cluster"] == "c1"

    def test_with_no_metrics_returns_empty_list(self, wired_service):
        wired_service.get_rollout.return_value = _make_rollout(current_stage=None)
        wired_service.collect_metrics.return_value = []
        resp = canary_rollout_metrics(_make_ctx(path_params={"rollout_id": "r1"}))
        assert resp.body["metrics"] == []
        assert resp.body["current_stage"] is None


# =============================================================================
# canary_rollout_history
# =============================================================================


class TestCanaryRolloutHistory:
    def test_default_limit_20(self, wired_service):
        wired_service.get_completed_rollouts.return_value = []
        resp = canary_rollout_history(_make_ctx())
        wired_service.get_completed_rollouts.assert_called_once_with(limit=20)
        assert resp.status_code == 200
        assert resp.body["count"] == 0

    def test_limit_param_parsed(self, wired_service):
        canary_rollout_history(_make_ctx(query={"limit": "50"}))
        wired_service.get_completed_rollouts.assert_called_once_with(limit=50)

    def test_limit_invalid_string_falls_back_to_20(self, wired_service):
        canary_rollout_history(_make_ctx(query={"limit": "not-a-number"}))
        wired_service.get_completed_rollouts.assert_called_once_with(limit=20)

    def test_limit_clamped_to_max_100(self, wired_service):
        canary_rollout_history(_make_ctx(query={"limit": "9999"}))
        wired_service.get_completed_rollouts.assert_called_once_with(limit=100)

    def test_limit_clamped_to_min_1(self, wired_service):
        canary_rollout_history(_make_ctx(query={"limit": "-5"}))
        wired_service.get_completed_rollouts.assert_called_once_with(limit=1)

    def test_config_type_filter(self, wired_service):
        wired_service.get_completed_rollouts.return_value = [
            _make_rollout("r1", config_type="ff"),
            _make_rollout("r2", config_type="other"),
        ]
        resp = canary_rollout_history(_make_ctx(query={"config_type": "ff"}))
        assert resp.body["count"] == 1
        assert resp.body["history"][0]["id"] == "r1"

    def test_state_filter_valid(self, wired_service):
        wired_service.get_completed_rollouts.return_value = [
            _make_rollout("r1", state="completed"),
            _make_rollout("r2", state="rolled_back"),
        ]
        # CanaryState("completed") matches r1 only
        resp = canary_rollout_history(_make_ctx(query={"state": "completed"}))
        # Cross-check via baldur.models.canary import path used by handler.
        from baldur.models.canary import CanaryState

        # Compare state.value-vs-CanaryState instance equality is how the
        # handler filters; replace SimpleNamespace state with real enum so
        # the comparison `r.state == target_state` works against equality.
        wired_service.get_completed_rollouts.return_value = [
            SimpleNamespace(
                id="r1",
                config_type="ff",
                state=CanaryState.COMPLETED,
                current_stage=None,
                current_stage_index=0,
                stages=[],
                affected_clusters=[],
                created_by="a",
                created_at=datetime(2026, 5, 20, tzinfo=UTC),
                progress_percentage=100.0,
            ),
            SimpleNamespace(
                id="r2",
                config_type="ff",
                state=CanaryState.ROLLED_BACK,
                current_stage=None,
                current_stage_index=0,
                stages=[],
                affected_clusters=[],
                created_by="a",
                created_at=datetime(2026, 5, 20, tzinfo=UTC),
                progress_percentage=100.0,
            ),
        ]
        resp = canary_rollout_history(_make_ctx(query={"state": "completed"}))
        assert resp.body["count"] == 1
        assert resp.body["history"][0]["id"] == "r1"

    def test_state_filter_invalid_value_ignored(self, wired_service):
        wired_service.get_completed_rollouts.return_value = [
            _make_rollout("r1", state="completed"),
        ]
        # "not_a_state" raises ValueError on CanaryState(...) — handler
        # silently swallows and returns the unfiltered list.
        resp = canary_rollout_history(_make_ctx(query={"state": "not_a_state"}))
        assert resp.body["count"] == 1
