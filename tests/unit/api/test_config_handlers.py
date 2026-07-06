"""Unit tests for framework-agnostic Runtime Config handlers (429 PR3-phase2a).

Target: ``baldur.api.handlers.config`` — generic get/update, reset, pending
changes, SLO special-case, logging runtime hot reload.

Verification techniques applied (§8):
  - §8.4 Side effects — runtime logger apply on logging PUT
  - §8.5 Dependency interaction — manager.update_with_strategy args
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from baldur.api.handlers.config import (
    _APPLY_OPTION_FIELDS,
    _split_apply_options,
    _status_to_http,
    all_config_get,
    cancel_pending_change,
    config_get,
    config_reset,
    config_update,
    editable_config_get,
    logging_config_update,
    pending_changes_get,
    slo_config_delete,
    slo_config_update,
)
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


# =============================================================================
# _split_apply_options — Contract
# =============================================================================


class TestSplitApplyOptionsContract:
    """Apply-option field names are the API design contract."""

    def test_apply_option_fields_expected_set(self):
        """Contract: apply options are apply_strategy + delay_seconds +
        grace_timeout_seconds + reason + expected_version (666 optimistic
        concurrency)."""
        assert _APPLY_OPTION_FIELDS == {
            "apply_strategy",
            "delay_seconds",
            "grace_timeout_seconds",
            "reason",
            "expected_version",
        }

    def test_apply_options_extracted_from_body(self):
        body = {
            "apply_strategy": "delayed",
            "delay_seconds": 30,
            "grace_timeout_seconds": 120,
            "reason": "staged rollout",
            "max_retries": 5,
        }
        apply, changes = _split_apply_options(body)
        assert apply == {
            "strategy": "delayed",
            "delay_seconds": 30,
            "grace_timeout_seconds": 120,
            "reason": "staged rollout",
            "expected_version": None,
        }
        assert changes == {"max_retries": 5}

    def test_none_values_filtered_from_changes(self):
        """None values are not forwarded as config changes."""
        body = {"max_retries": 5, "timeout_ms": None}
        _, changes = _split_apply_options(body)
        assert changes == {"max_retries": 5}

    def test_missing_apply_keys_default_to_none_or_empty(self):
        apply, _ = _split_apply_options({"field": 1})
        assert apply["strategy"] is None
        assert apply["delay_seconds"] is None
        assert apply["grace_timeout_seconds"] is None
        assert apply["reason"] == ""


# =============================================================================
# _status_to_http — Contract (status→HTTP mapping is the design contract)
# =============================================================================


class TestStatusToHttpContract:
    def test_applied_maps_to_200(self):
        assert _status_to_http("applied") == 200

    def test_scheduled_maps_to_202(self):
        assert _status_to_http("scheduled") == 202

    def test_waiting_maps_to_202(self):
        assert _status_to_http("waiting") == 202

    def test_unknown_status_maps_to_400(self):
        assert _status_to_http("error") == 400
        assert _status_to_http("") == 400
        assert _status_to_http("bogus") == 400


# =============================================================================
# all_config_get / config_reset / pending_changes_get / cancel_pending_change
# =============================================================================


class TestAllConfigGetBehavior:
    @pytest.fixture(autouse=True)
    def _require_pro(self):
        pytest.importorskip("baldur_pro")

    def test_wraps_values_with_default_strategy(self):
        manager = MagicMock()
        manager.get_all_config.return_value = {"cb": {"max_retries": 3}}
        manager.get_default_strategy.return_value = {"strategy": "immediate"}
        manager.get_pending_changes.return_value = []
        with patch(
            "baldur_pro.services.runtime_config.get_runtime_config_manager",
            return_value=manager,
        ):
            resp = all_config_get(_make_ctx())
        assert resp.status_code == 200
        assert resp.body["config"]["cb"]["values"] == {"max_retries": 3}
        assert resp.body["config"]["cb"]["default_strategy"] == {
            "strategy": "immediate"
        }


class TestConfigResetBehavior:
    @pytest.fixture(autouse=True)
    def _require_pro(self):
        pytest.importorskip("baldur_pro")

    def test_invokes_reset_to_defaults(self):
        manager = MagicMock()
        manager.reset_to_defaults.return_value = {"cb": {}}
        with patch(
            "baldur_pro.services.runtime_config.get_runtime_config_manager",
            return_value=manager,
        ):
            resp = config_reset(
                _make_ctx(method="POST", user=SimpleNamespace(username="admin"))
            )
        manager.reset_to_defaults.assert_called_once()
        assert resp.status_code == 200


class TestPendingChangesGetBehavior:
    @pytest.fixture(autouse=True)
    def _require_pro(self):
        pytest.importorskip("baldur_pro")

    def test_forwards_config_type_query_param(self):
        manager = MagicMock()
        manager.get_pending_changes.return_value = [{"id": "p1"}]
        with patch(
            "baldur_pro.services.runtime_config.get_runtime_config_manager",
            return_value=manager,
        ):
            resp = pending_changes_get(_make_ctx(query={"config_type": "dlq"}))
        manager.get_pending_changes.assert_called_once_with("dlq")
        assert resp.body["count"] == 1


class TestCancelPendingChangeBehavior:
    @pytest.fixture(autouse=True)
    def _require_pro(self):
        pytest.importorskip("baldur_pro")

    def test_missing_pending_id_returns_400(self):
        with patch(
            "baldur_pro.services.runtime_config.get_runtime_config_manager"
        ) as mock_get:
            resp = cancel_pending_change(
                _make_ctx(method="POST", path_params={"pending_id": ""})
            )
        assert resp.status_code == 400
        mock_get.assert_not_called()

    def test_cancelled_status_returns_200(self):
        manager = MagicMock()
        manager.cancel_pending_change.return_value = {
            "status": "cancelled",
            "pending_id": "p1",
        }
        with patch(
            "baldur_pro.services.runtime_config.get_runtime_config_manager",
            return_value=manager,
        ):
            resp = cancel_pending_change(
                _make_ctx(method="POST", path_params={"pending_id": "p1"})
            )
        assert resp.status_code == 200
        assert resp.body["status"] == "cancelled"

    def test_not_cancelled_returns_404(self):
        manager = MagicMock()
        manager.cancel_pending_change.return_value = {
            "status": "error",
            "error": "not found",
        }
        with patch(
            "baldur_pro.services.runtime_config.get_runtime_config_manager",
            return_value=manager,
        ):
            resp = cancel_pending_change(
                _make_ctx(method="POST", path_params={"pending_id": "p1"})
            )
        assert resp.status_code == 404


# =============================================================================
# config_get / config_update (generic)
# =============================================================================


class TestConfigGetBehavior:
    @pytest.fixture(autouse=True)
    def _require_pro(self):
        pytest.importorskip("baldur_pro")

    def test_delegates_to_manager_get_config(self):
        manager = MagicMock()
        manager.get_config.return_value = {"max_retries": 3}
        manager.get_default_strategy.return_value = {"strategy": "immediate"}
        manager.get_pending_changes.return_value = []
        with patch(
            "baldur_pro.services.runtime_config.get_runtime_config_manager",
            return_value=manager,
        ):
            resp = config_get(_make_ctx(), config_name="dlq")
        manager.get_config.assert_called_once_with("dlq")
        assert resp.body["config_type"] == "dlq"
        assert resp.body["config"] == {"max_retries": 3}

    def test_includes_section_version_for_round_trip(self):
        """The read returns the per-section version the client echoes on PUT
        (the read→echo→check round-trip token, 666 D4)."""
        manager = MagicMock()
        manager.get_config.return_value = {"max_retries": 3}
        manager.get_default_strategy.return_value = {"strategy": "immediate"}
        manager.get_pending_changes.return_value = []
        manager.get_section_version.return_value = 7
        with patch(
            "baldur_pro.services.runtime_config.get_runtime_config_manager",
            return_value=manager,
        ):
            resp = config_get(_make_ctx(), config_name="dlq")
        assert resp.body["version"] == 7
        manager.get_section_version.assert_called_once_with("dlq")


class TestConfigUpdateBehavior:
    @pytest.fixture(autouse=True)
    def _require_pro(self):
        pytest.importorskip("baldur_pro")

    def test_empty_changes_returns_400(self):
        """Body with only apply-option fields -> 400 without invoking manager."""
        with patch(
            "baldur_pro.services.runtime_config.get_runtime_config_manager"
        ) as mock_get:
            resp = config_update(
                _make_ctx(
                    method="PUT",
                    json_body={"apply_strategy": "immediate", "reason": "x"},
                ),
                config_name="dlq",
            )
        assert resp.status_code == 400
        mock_get.assert_not_called()

    def test_applied_status_maps_to_200(self):
        manager = MagicMock()
        manager.update_with_strategy.return_value = {
            "status": "applied",
            "config": {},
        }
        with patch(
            "baldur_pro.services.runtime_config.get_runtime_config_manager",
            return_value=manager,
        ):
            resp = config_update(
                _make_ctx(method="PUT", json_body={"max_retries": 5}),
                config_name="dlq",
            )
        assert resp.status_code == 200

    def test_scheduled_status_maps_to_202(self):
        manager = MagicMock()
        manager.update_with_strategy.return_value = {"status": "scheduled"}
        with patch(
            "baldur_pro.services.runtime_config.get_runtime_config_manager",
            return_value=manager,
        ):
            resp = config_update(
                _make_ctx(
                    method="PUT",
                    json_body={
                        "max_retries": 5,
                        "apply_strategy": "delayed",
                        "delay_seconds": 30,
                    },
                ),
                config_name="dlq",
            )
        assert resp.status_code == 202

    def test_apply_options_forwarded_to_manager(self):
        manager = MagicMock()
        manager.update_with_strategy.return_value = {"status": "applied"}
        with patch(
            "baldur_pro.services.runtime_config.get_runtime_config_manager",
            return_value=manager,
        ):
            config_update(
                _make_ctx(
                    method="PUT",
                    json_body={
                        "max_retries": 5,
                        "apply_strategy": "graceful",
                        "grace_timeout_seconds": 120,
                    },
                    user=SimpleNamespace(username="admin"),
                ),
                config_name="dlq",
            )
        _, kwargs = manager.update_with_strategy.call_args
        assert kwargs["strategy"] == "graceful"
        assert kwargs["grace_timeout_seconds"] == 120
        assert kwargs["changed_by"] == "admin"


# =============================================================================
# config_update — 409 version-conflict mapping (666 D4, OSS handler)
# =============================================================================


class TestConfigUpdate409Behavior:
    """A ConfigVersionConflictError from the manager maps to HTTP 409 with a
    usable retry token (expected/actual) plus the fresh current_config fetched
    in the except block — so the console renders a merge without a re-GET (666
    D4)."""

    @pytest.fixture(autouse=True)
    def _require_pro(self):
        pytest.importorskip("baldur_pro")

    def test_version_conflict_maps_to_409_with_token_and_current_config(self):
        from baldur.core.exceptions import ConfigVersionConflictError

        manager = MagicMock()
        manager.update_with_strategy.side_effect = ConfigVersionConflictError(
            "dlq", expected_version=1, actual_version=4
        )
        manager.get_config.return_value = {"max_replay_attempts": 9}

        with patch(
            "baldur_pro.services.runtime_config.get_runtime_config_manager",
            return_value=manager,
        ):
            resp = config_update(
                _make_ctx(
                    method="PUT",
                    json_body={"max_replay_attempts": 5, "expected_version": 1},
                ),
                config_name="dlq",
            )

        assert resp.status_code == 409
        assert resp.body["status"] == "conflict"
        assert resp.body["expected_version"] == 1
        assert resp.body["actual_version"] == 4
        # The fresh current_config is fetched in the except block (not carried on
        # the scalar exception) so the editor can diff immediately.
        assert resp.body["current_config"] == {"max_replay_attempts": 9}
        manager.get_config.assert_called_once_with("dlq")

    def test_current_config_none_when_refetch_fails(self):
        """The current_config refetch is best-effort: a failure degrades to None
        (still a 409 with the retry token), never a propagated 500."""
        from baldur.core.exceptions import ConfigVersionConflictError

        manager = MagicMock()
        manager.update_with_strategy.side_effect = ConfigVersionConflictError(
            "retry", expected_version=0, actual_version=2
        )
        manager.get_config.side_effect = RuntimeError("backend down")

        with patch(
            "baldur_pro.services.runtime_config.get_runtime_config_manager",
            return_value=manager,
        ):
            resp = config_update(
                _make_ctx(
                    method="PUT",
                    json_body={"max_attempts": 5, "expected_version": 0},
                ),
                config_name="retry",
            )

        assert resp.status_code == 409
        assert resp.body["current_config"] is None


# =============================================================================
# editable_config_get — console editor projection (662 D1/D6)
# =============================================================================


class TestEditableConfigGetContract:
    """GET /config/editable backs the console Runtime Config editor panel:
    the tier-filtered projection wrapped with per-domain default strategy, the
    audit-status badge, and current pending changes."""

    @pytest.fixture(autouse=True)
    def _require_pro(self):
        pytest.importorskip("baldur_pro")

    def test_wraps_projection_with_strategy_and_audit_badge(self):
        manager = MagicMock()
        manager.get_editable_config.return_value = {
            "retry": {"max_attempts": {"value": 3, "widget": "number"}}
        }
        manager.get_default_strategy.return_value = {"strategy": "immediate"}
        manager.get_pending_changes.return_value = []
        with (
            patch(
                "baldur_pro.services.runtime_config.get_runtime_config_manager",
                return_value=manager,
            ),
            patch(
                "baldur.settings.audit.get_audit_settings",
                return_value=SimpleNamespace(enabled=True),
            ),
        ):
            resp = editable_config_get(_make_ctx())

        assert resp.status_code == 200
        assert resp.body["status"] == "success"
        assert resp.body["audit_enabled"] is True
        assert resp.body["pending_changes"] == []
        # Each domain wraps its fields projection + the default strategy.
        retry = resp.body["config"]["retry"]
        assert retry["fields"] == {"max_attempts": {"value": 3, "widget": "number"}}
        assert retry["default_strategy"] == {"strategy": "immediate"}

    def test_each_domain_carries_section_version(self):
        """Each editable domain bundles its per-section OCC version (a per-domain
        body field, since one ETag cannot represent N section versions, 666 D4)."""
        manager = MagicMock()
        manager.get_editable_config.return_value = {
            "retry": {"max_attempts": {"value": 3, "widget": "number"}}
        }
        manager.get_default_strategy.return_value = {"strategy": "immediate"}
        manager.get_pending_changes.return_value = []
        manager.get_section_version.return_value = 4
        with (
            patch(
                "baldur_pro.services.runtime_config.get_runtime_config_manager",
                return_value=manager,
            ),
            patch(
                "baldur.settings.audit.get_audit_settings",
                return_value=SimpleNamespace(enabled=True),
            ),
        ):
            resp = editable_config_get(_make_ctx())

        assert resp.body["config"]["retry"]["version"] == 4
        manager.get_section_version.assert_called_once_with("retry")

    def test_audit_badge_false_when_audit_disabled(self):
        """audit_enabled mirrors BALDUR_AUDIT_ENABLED — OFF drives the panel's
        "reason not durably retained" warning (662 D5)."""
        manager = MagicMock()
        manager.get_editable_config.return_value = {}
        manager.get_pending_changes.return_value = []
        with (
            patch(
                "baldur_pro.services.runtime_config.get_runtime_config_manager",
                return_value=manager,
            ),
            patch(
                "baldur.settings.audit.get_audit_settings",
                return_value=SimpleNamespace(enabled=False),
            ),
        ):
            resp = editable_config_get(_make_ctx())

        assert resp.body["audit_enabled"] is False

    def test_audit_badge_degrades_to_false_on_settings_error(self):
        """The badge is best-effort: any settings failure degrades to False (the
        safe, warning-shown state), never propagating a 500."""
        manager = MagicMock()
        manager.get_editable_config.return_value = {}
        manager.get_pending_changes.return_value = []
        with (
            patch(
                "baldur_pro.services.runtime_config.get_runtime_config_manager",
                return_value=manager,
            ),
            patch(
                "baldur.settings.audit.get_audit_settings",
                side_effect=RuntimeError("settings unavailable"),
            ),
        ):
            resp = editable_config_get(_make_ctx())

        assert resp.status_code == 200
        assert resp.body["audit_enabled"] is False


# =============================================================================
# logging_config_update
# =============================================================================


class TestLoggingConfigUpdateBehavior:
    """PUT /config/logging/ applies runtime logger changes after a successful
    update (283 DYNAMIC_LOG_LEVEL_API)."""

    @pytest.fixture(autouse=True)
    def _require_pro(self):
        pytest.importorskip("baldur_pro")

    def test_runtime_apply_invoked_on_success(self):
        """200 response -> _apply_component_log_levels is called."""
        manager = MagicMock()
        manager.update_with_strategy.return_value = {
            "status": "applied",
            "config": {},
        }

        with (
            patch(
                "baldur_pro.services.runtime_config.get_runtime_config_manager",
                return_value=manager,
            ),
            patch(
                "baldur.observability.structlog_config._apply_component_log_levels"
            ) as mock_apply,
            patch(
                "baldur.observability.structlog_config._COMPONENT_LOGGER_MAP",
                {"selfhealing": "baldur"},
            ),
            patch("baldur.settings.logging_settings.reset_logging_settings"),
            patch(
                "baldur.settings.logging_settings.get_logging_settings",
                return_value=MagicMock(selfhealing="DEBUG"),
            ),
        ):
            resp = logging_config_update(
                _make_ctx(method="PUT", json_body={"selfhealing": "DEBUG"})
            )

        assert resp.status_code == 200
        mock_apply.assert_called_once()

    def test_runtime_apply_graceful_on_failure(self):
        """Exception in runtime apply -> swallowed (logged), still returns 200."""
        manager = MagicMock()
        manager.update_with_strategy.return_value = {
            "status": "applied",
            "config": {},
        }

        with (
            patch(
                "baldur_pro.services.runtime_config.get_runtime_config_manager",
                return_value=manager,
            ),
            patch(
                "baldur.observability.structlog_config._apply_component_log_levels",
                side_effect=Exception("boom"),
            ),
            patch(
                "baldur.observability.structlog_config._COMPONENT_LOGGER_MAP",
                {"selfhealing": "baldur"},
            ),
            patch("baldur.settings.logging_settings.reset_logging_settings"),
            patch(
                "baldur.settings.logging_settings.get_logging_settings",
                return_value=MagicMock(selfhealing="DEBUG"),
            ),
        ):
            resp = logging_config_update(
                _make_ctx(method="PUT", json_body={"selfhealing": "DEBUG"})
            )

        # Exception inside runtime apply must not propagate
        assert resp.status_code == 200

    def test_runtime_apply_skipped_when_update_fails(self):
        """400 response -> runtime apply is NOT attempted."""
        manager = MagicMock()
        manager.update_with_strategy.return_value = {"status": "error"}

        with (
            patch(
                "baldur_pro.services.runtime_config.get_runtime_config_manager",
                return_value=manager,
            ),
            patch(
                "baldur.observability.structlog_config._apply_component_log_levels"
            ) as mock_apply,
        ):
            resp = logging_config_update(
                _make_ctx(method="PUT", json_body={"selfhealing": "DEBUG"})
            )

        assert resp.status_code == 400
        mock_apply.assert_not_called()


# =============================================================================
# SLO special-case handlers
# =============================================================================


class TestSloConfigUpdateBehavior:
    @pytest.fixture(autouse=True)
    def _require_pro(self):
        pytest.importorskip("baldur_pro")

    def test_forwards_slo_fields(self):
        manager = MagicMock()
        manager.update_slo_config.return_value = {"slos": {}}
        with patch(
            "baldur_pro.services.runtime_config.get_runtime_config_manager",
            return_value=manager,
        ):
            resp = slo_config_update(
                _make_ctx(
                    method="PUT",
                    json_body={
                        "default_target": 0.999,
                        "slo": {"name": "api_latency", "target": 0.99},
                    },
                    user=SimpleNamespace(username="admin"),
                )
            )
        _, kwargs = manager.update_slo_config.call_args
        assert kwargs["default_target"] == 0.999
        assert kwargs["slo"] == {"name": "api_latency", "target": 0.99}
        assert resp.status_code == 200


class TestSloConfigDeleteBehavior:
    @pytest.fixture(autouse=True)
    def _require_pro(self):
        pytest.importorskip("baldur_pro")

    def test_missing_name_query_returns_400(self):
        with patch(
            "baldur_pro.services.runtime_config.get_runtime_config_manager"
        ) as mock_get:
            resp = slo_config_delete(_make_ctx(method="DELETE"))
        assert resp.status_code == 400
        mock_get.assert_not_called()

    def test_deleted_status_returns_200(self):
        manager = MagicMock()
        manager.delete_slo.return_value = {
            "status": "deleted",
            "slo_name": "api_latency",
        }
        with patch(
            "baldur_pro.services.runtime_config.get_runtime_config_manager",
            return_value=manager,
        ):
            resp = slo_config_delete(
                _make_ctx(method="DELETE", query={"name": "api_latency"})
            )
        assert resp.status_code == 200

    def test_missing_slo_returns_404(self):
        manager = MagicMock()
        manager.delete_slo.return_value = {"status": "error"}
        with patch(
            "baldur_pro.services.runtime_config.get_runtime_config_manager",
            return_value=manager,
        ):
            resp = slo_config_delete(
                _make_ctx(method="DELETE", query={"name": "missing"})
            )
        assert resp.status_code == 404
