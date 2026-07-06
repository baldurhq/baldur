"""GovernanceApiService — unit tests (523 Step 4).

Covers ``baldur_pro.services.governance.api_service``: ``GovernanceApiService``
(public methods + private helpers) plus the singleton accessors.

Scope:
- ``set_startup_info``: stores startup_time + optional next_scheduled_sync.
- ``get_status``: orchestrates sub-helpers and assembles status dict.
- ``reconcile``: success + ImportError + init exception branches.
- ``set_mode``: validation, audit logging hook, STRICT/NORMAL tracker sync,
  warning message, expires_at injection.
- ``_sync_emergency_tracker``: STRICT activation, NORMAL restoration,
  ImportError, generic exception.
- ``_get_reliability_states``: success / ImportError / Exception branches.
- ``_build_domains_status``: object-with-attrs branch + dict branch +
  enum unwrapping.
- ``_get_global_operating_mode``: manager path + fallback derivation +
  empty-states default.
- ``_classify_overall_health``: empty / critical / warning / degraded / healthy.
- ``_get_sync_status``: aggregation of last_sync_time / actor / freshness.
- ``_get_snapshot_health``: ImportError fallback + generic exception fallback.
- ``_get_drift_summary``: ImportError + drift accumulation + exception fallback.
- ``_get_next_sync_expected_at``: None vs set.
- ``_log_mode_change``: success path + exception path (silent).
- ``_get_mode_warning``: STRICT/EMERGENCY/CAUTIOUS/NORMAL/unknown.
- ``get_governance_api_service`` / ``reset_governance_api_service``: singleton
  lifecycle.
"""

from __future__ import annotations

import pytest

pytest.importorskip("baldur_pro")

pytestmark = pytest.mark.requires_pro


from types import SimpleNamespace
from unittest.mock import patch

import pytest

from baldur.metrics.reliability_manager import OperatingMode
from baldur_pro.services.governance.api_service import (
    GovernanceApiService,
    get_governance_api_service,
    reset_governance_api_service,
)

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture(autouse=True)
def _reset_singleton():
    reset_governance_api_service()
    yield
    reset_governance_api_service()


# =============================================================================
# set_startup_info — startup hydration contract
# =============================================================================


class TestSetStartupInfoContract:
    def test_stores_startup_time(self):
        svc = GovernanceApiService()
        svc.set_startup_info(startup_time=1000.0)
        assert svc._startup_time == 1000.0
        assert svc._next_scheduled_sync is None

    def test_stores_next_scheduled_sync(self):
        svc = GovernanceApiService()
        svc.set_startup_info(startup_time=1000.0, next_scheduled_sync=1500.0)
        assert svc._next_scheduled_sync == 1500.0


# =============================================================================
# _get_reliability_states — try/except branches
# =============================================================================


class TestGetReliabilityStatesBranches:
    def test_success_returns_manager_states(self):
        manager = SimpleNamespace(
            get_all_states=lambda: {
                "d1": SimpleNamespace(operating_mode=OperatingMode.NORMAL)
            }
        )
        with patch(
            "baldur.metrics.reliability_manager.get_reliability_manager",
            return_value=manager,
        ):
            states = GovernanceApiService()._get_reliability_states()
        assert "d1" in states

    def test_import_error_returns_empty_dict(self):
        with patch(
            "baldur.metrics.reliability_manager.get_reliability_manager",
            side_effect=ImportError("not installed"),
        ):
            states = GovernanceApiService()._get_reliability_states()
        assert states == {}

    def test_generic_exception_returns_empty_dict(self):
        with patch(
            "baldur.metrics.reliability_manager.get_reliability_manager",
            side_effect=RuntimeError("boom"),
        ):
            states = GovernanceApiService()._get_reliability_states()
        assert states == {}


# =============================================================================
# _build_domains_status — object vs dict branches
# =============================================================================


class TestBuildDomainsStatusBehavior:
    def test_object_state_unwraps_enum_values(self):
        state = SimpleNamespace(
            reliability_level=SimpleNamespace(value="high"),
            operating_mode=SimpleNamespace(value="normal"),
            last_sync_time=1234.0,
            last_sync_source="push",
            consecutive_successful_syncs=5,
            is_data_fresh=True,
            stabilization_progress=0.8,
            current_value=42,
        )
        result = GovernanceApiService()._build_domains_status({"d1": state})
        assert result["d1"]["reliability_level"] == "high"
        assert result["d1"]["operating_mode"] == "normal"
        assert result["d1"]["consecutive_syncs"] == 5
        assert result["d1"]["is_data_fresh"] is True
        assert result["d1"]["dlq_pending"]["value"] == 42

    def test_dict_state_pass_through(self):
        result = GovernanceApiService()._build_domains_status({"d2": {"level": "low"}})
        # Dict state path: a dict has no __dict__ from dataclass-like state
        # However SimpleNamespace and regular dicts both expose __dict__ —
        # but the function checks ``hasattr(state, "__dict__")`` so a plain
        # dict (which has no __dict__) takes the else branch.
        assert result["d2"] == {"level": "low"}


# =============================================================================
# _get_global_operating_mode — manager + fallback branches
# =============================================================================


class TestGetGlobalOperatingModeBranches:
    def test_manager_returns_mode(self):
        manager = SimpleNamespace(get_global_mode=lambda: OperatingMode.STRICT)
        with patch(
            "baldur.metrics.reliability_manager.get_reliability_manager",
            return_value=manager,
        ):
            mode = GovernanceApiService()._get_global_operating_mode({})
        assert mode == "strict"

    def test_manager_failure_falls_back_to_strictest_state_mode(self):
        states = {
            "a": SimpleNamespace(operating_mode=OperatingMode.NORMAL),
            "b": SimpleNamespace(operating_mode=OperatingMode.EMERGENCY),
        }
        with patch(
            "baldur.metrics.reliability_manager.get_reliability_manager",
            side_effect=RuntimeError("down"),
        ):
            mode = GovernanceApiService()._get_global_operating_mode(states)
        assert mode == "emergency"

    def test_manager_failure_strictest_wins_across_full_ordering(self):
        states = {
            "a": SimpleNamespace(operating_mode=OperatingMode.NORMAL),
            "b": SimpleNamespace(operating_mode=OperatingMode.CAUTIOUS),
            "c": SimpleNamespace(operating_mode=OperatingMode.STRICT),
        }
        with patch(
            "baldur.metrics.reliability_manager.get_reliability_manager",
            side_effect=RuntimeError("down"),
        ):
            mode = GovernanceApiService()._get_global_operating_mode(states)
        assert mode == "strict"

    def test_manager_failure_no_modes_returns_normal_default(self):
        with patch(
            "baldur.metrics.reliability_manager.get_reliability_manager",
            side_effect=RuntimeError("down"),
        ):
            mode = GovernanceApiService()._get_global_operating_mode({})
        assert mode == "NORMAL"


# =============================================================================
# _classify_overall_health — bucketing branches
# =============================================================================


def _state_with_level(level: str):
    return SimpleNamespace(reliability_level=SimpleNamespace(value=level))


class TestClassifyOverallHealthBranches:
    def test_empty_states_returns_unknown(self):
        assert GovernanceApiService()._classify_overall_health({}) == "unknown"

    def test_majority_unknown_returns_critical(self):
        states = {
            "a": _state_with_level("unknown"),
            "b": _state_with_level("unknown"),
            "c": _state_with_level("high"),
        }
        assert GovernanceApiService()._classify_overall_health(states) == "critical"

    def test_some_unknown_returns_warning(self):
        states = {
            "a": _state_with_level("unknown"),
            "b": _state_with_level("high"),
            "c": _state_with_level("high"),
        }
        assert GovernanceApiService()._classify_overall_health(states) == "warning"

    def test_majority_low_returns_warning(self):
        states = {
            "a": _state_with_level("low"),
            "b": _state_with_level("low"),
            "c": _state_with_level("high"),
        }
        assert GovernanceApiService()._classify_overall_health(states) == "warning"

    def test_some_low_returns_degraded(self):
        states = {
            "a": _state_with_level("low"),
            "b": _state_with_level("high"),
            "c": _state_with_level("high"),
            "d": _state_with_level("high"),
        }
        assert GovernanceApiService()._classify_overall_health(states) == "degraded"

    def test_all_high_returns_healthy(self):
        states = {
            "a": _state_with_level("high"),
            "b": _state_with_level("high"),
        }
        assert GovernanceApiService()._classify_overall_health(states) == "healthy"


# =============================================================================
# _get_sync_status — aggregation logic
# =============================================================================


class TestGetSyncStatusBehavior:
    def test_picks_latest_sync_time_and_actor(self):
        states = {
            "a": SimpleNamespace(
                last_sync_time=100.0,
                last_sync_source="push",
                is_data_fresh=True,
                consecutive_successful_syncs=2,
            ),
            "b": SimpleNamespace(
                last_sync_time=200.0,
                last_sync_source="db",
                is_data_fresh=False,
                consecutive_successful_syncs=5,
            ),
        }
        result = GovernanceApiService()._get_sync_status(states)
        assert result["last_sync_actor"] == "db"  # latest timestamp wins
        assert result["last_sync_at"] is not None
        assert result["is_stale"] is False  # any-fresh → not stale
        assert result["consecutive_syncs"] == 5  # max

    def test_no_sync_returns_none_at(self):
        result = GovernanceApiService()._get_sync_status({})
        assert result["last_sync_at"] is None
        assert result["is_stale"] is True
        assert result["consecutive_syncs"] == 0


# =============================================================================
# _get_snapshot_health — ImportError fallback path
# =============================================================================


class TestGetSnapshotHealthBranches:
    def test_import_error_returns_invalid_default(self):
        with patch(
            "baldur.metrics.snapshot_storage.get_snapshot_storage",
            side_effect=ImportError("absent"),
        ):
            result = GovernanceApiService()._get_snapshot_health()
        assert result == {"age_seconds": None, "is_valid": False, "path": None}

    def test_success_with_fresh_snapshot_returns_valid(self):
        storage = SimpleNamespace(
            get_oldest_snapshot_age=lambda: 100.0,
            base_path="/tmp/snap",
        )
        with patch(
            "baldur.metrics.snapshot_storage.get_snapshot_storage",
            return_value=storage,
            create=True,
        ):
            result = GovernanceApiService()._get_snapshot_health()
        assert result["age_seconds"] == 100.0
        assert result["is_valid"] is True
        assert result["path"] == "/tmp/snap"

    def test_success_with_stale_snapshot_returns_invalid(self):
        storage = SimpleNamespace(
            get_oldest_snapshot_age=lambda: 7200.0, base_path="/tmp/snap"
        )
        with patch(
            "baldur.metrics.snapshot_storage.get_snapshot_storage",
            return_value=storage,
            create=True,
        ):
            result = GovernanceApiService()._get_snapshot_health()
        assert result["is_valid"] is False

    def test_generic_exception_returns_error_payload(self):
        with patch(
            "baldur.metrics.snapshot_storage.get_snapshot_storage",
            side_effect=RuntimeError("disk full"),
            create=True,
        ):
            result = GovernanceApiService()._get_snapshot_health()
        assert result["is_valid"] is False
        assert "disk full" in result["error"]


# =============================================================================
# _get_drift_summary — aggregation + branches
# =============================================================================


class TestGetDriftSummaryBranches:
    def test_drift_accumulation(self):
        service = SimpleNamespace(
            get_drift_report=lambda: {
                "metrics": {
                    "rps": {
                        "d1": {"drift": 1.0, "is_critical": True},
                        "d2": {"drift": 0.0, "is_critical": False},
                    },
                    "latency": {
                        "d1": {"drift": -0.5, "is_critical": False},
                    },
                }
            }
        )
        with patch(
            "baldur.services.metric_sync_service.get_metric_sync_service",
            return_value=service,
        ):
            result = GovernanceApiService()._get_drift_summary()
        assert result["total_drifts"] == 2  # d2 with drift=0 skipped
        assert result["critical_drifts"] == 1
        assert set(result["domains_with_drift"]) == {"d1"}

    def test_import_error_returns_zero_drift(self):
        with patch(
            "baldur.services.metric_sync_service.get_metric_sync_service",
            side_effect=ImportError("not installed"),
        ):
            result = GovernanceApiService()._get_drift_summary()
        assert result == {
            "total_drifts": 0,
            "critical_drifts": 0,
            "domains_with_drift": [],
        }

    def test_generic_exception_returns_zero_drift(self):
        with patch(
            "baldur.services.metric_sync_service.get_metric_sync_service",
            side_effect=RuntimeError("boom"),
        ):
            result = GovernanceApiService()._get_drift_summary()
        assert result == {
            "total_drifts": 0,
            "critical_drifts": 0,
            "domains_with_drift": [],
        }


# =============================================================================
# _get_next_sync_expected_at — set vs unset
# =============================================================================


class TestGetNextSyncExpectedAtBehavior:
    def test_unset_returns_none(self):
        svc = GovernanceApiService()
        assert svc._get_next_sync_expected_at() is None

    def test_set_returns_iso_string(self):
        svc = GovernanceApiService()
        svc.set_startup_info(startup_time=1000.0, next_scheduled_sync=2000.0)
        result = svc._get_next_sync_expected_at()
        assert isinstance(result, str)
        assert "T" in result  # ISO format


# =============================================================================
# _get_mode_warning — message map
# =============================================================================


class TestGetModeWarningBehavior:
    def test_strict_returns_strict_warning(self):
        msg = GovernanceApiService()._get_mode_warning(OperatingMode.STRICT)
        assert "STRICT" in msg

    def test_emergency_returns_emergency_warning(self):
        msg = GovernanceApiService()._get_mode_warning(OperatingMode.EMERGENCY)
        assert "EMERGENCY" in msg

    def test_cautious_returns_cautious_warning(self):
        msg = GovernanceApiService()._get_mode_warning(OperatingMode.CAUTIOUS)
        assert "CAUTIOUS" in msg

    def test_normal_returns_none(self):
        assert GovernanceApiService()._get_mode_warning(OperatingMode.NORMAL) is None

    def test_string_input_is_supported(self):
        assert "STRICT" in GovernanceApiService()._get_mode_warning("strict")


# =============================================================================
# _log_mode_change — audit logging branches
# =============================================================================


class TestLogModeChangeBranches:
    def test_success_calls_audit_logger(self):
        logged = []
        audit = SimpleNamespace(log=lambda event: logged.append(event))
        with patch("baldur.audit.logger.AuditLogger.get_instance", return_value=audit):
            GovernanceApiService()._log_mode_change(
                actor="alice",
                old_mode=OperatingMode.NORMAL,
                new_mode=OperatingMode.STRICT,
                reason="drill",
            )
        assert len(logged) == 1

    def test_exception_is_swallowed(self):
        with patch(
            "baldur.audit.logger.AuditLogger.get_instance",
            side_effect=RuntimeError("audit broken"),
        ):
            # Must not raise
            GovernanceApiService()._log_mode_change(
                actor="alice",
                old_mode=OperatingMode.NORMAL,
                new_mode=OperatingMode.STRICT,
                reason=None,
            )


# =============================================================================
# _sync_emergency_tracker — STRICT / NORMAL / errors
# =============================================================================


class TestSyncEmergencyTrackerBranches:
    def test_strict_activates_tracker_and_injects_expires_at(self):
        tracker = SimpleNamespace(
            record_emergency_activation=lambda **kw: {"expiry_hours": 4},
        )
        with patch(
            "baldur_pro.services.governance.get_emergency_tracker",
            return_value=tracker,
        ):
            result = GovernanceApiService()._sync_emergency_tracker(
                mode="STRICT",
                actor="alice",
                reason="drill",
                old_mode=OperatingMode.NORMAL,
            )
        assert result["expiry_hours"] == 4
        assert "expires_at" in result

    def test_normal_from_strict_calls_restoration(self):
        calls = []
        tracker = SimpleNamespace(
            record_normal_restoration=lambda **kw: (
                calls.append(kw) or {"deactivated": True}
            ),
        )
        with patch(
            "baldur_pro.services.governance.get_emergency_tracker",
            return_value=tracker,
        ):
            result = GovernanceApiService()._sync_emergency_tracker(
                mode="NORMAL",
                actor="alice",
                reason=None,
                old_mode=OperatingMode.STRICT,
            )
        assert result == {"deactivated": True}
        assert calls[0]["restored_by"] == "alice"

    def test_normal_from_normal_no_op(self):
        tracker = SimpleNamespace(
            record_normal_restoration=lambda **kw: pytest.fail("should not run"),
        )
        with patch(
            "baldur_pro.services.governance.get_emergency_tracker",
            return_value=tracker,
        ):
            result = GovernanceApiService()._sync_emergency_tracker(
                mode="NORMAL",
                actor="alice",
                reason=None,
                old_mode=OperatingMode.NORMAL,
            )
        assert result is None

    def test_import_error_returns_none(self):
        with patch(
            "baldur_pro.services.governance.get_emergency_tracker",
            side_effect=ImportError("not present"),
        ):
            result = GovernanceApiService()._sync_emergency_tracker(
                mode="STRICT",
                actor="alice",
                reason="drill",
                old_mode=OperatingMode.NORMAL,
            )
        assert result is None

    def test_generic_exception_returns_none(self):
        with patch(
            "baldur_pro.services.governance.get_emergency_tracker",
            side_effect=RuntimeError("broken"),
        ):
            result = GovernanceApiService()._sync_emergency_tracker(
                mode="STRICT",
                actor="alice",
                reason="drill",
                old_mode=OperatingMode.NORMAL,
            )
        assert result is None


# =============================================================================
# set_mode — public API
# =============================================================================


class TestSetModeBehavior:
    def _patch_reliability_manager(self, manager):
        return patch(
            "baldur.metrics.reliability_manager.get_reliability_manager",
            return_value=manager,
        )

    def _patch_tracker_unavailable(self):
        # _sync_emergency_tracker hits ImportError → None
        return patch(
            "baldur_pro.services.governance.get_emergency_tracker",
            side_effect=ImportError("absent"),
        )

    def _patch_audit_silent(self):
        return patch(
            "baldur.audit.logger.AuditLogger.get_instance",
            side_effect=RuntimeError("absent"),
        )

    def test_invalid_mode_raises_value_error(self):
        with pytest.raises(ValueError, match="Invalid mode"):
            GovernanceApiService().set_mode("BOGUS")

    @pytest.mark.parametrize(
        ("mode_in", "expected"),
        [
            ("normal", "normal"),
            ("CAUTIOUS", "cautious"),
            ("strict", "strict"),
            ("EMERGENCY", "emergency"),
        ],
    )
    def test_valid_modes_round_trip(self, mode_in, expected):
        manager = SimpleNamespace(
            get_global_mode=lambda: OperatingMode.NORMAL,
            force_global_mode=lambda mode, reason: None,
        )
        with (
            self._patch_reliability_manager(manager),
            self._patch_tracker_unavailable(),
            self._patch_audit_silent(),
        ):
            result = GovernanceApiService().set_mode(mode_in, actor="alice")
        assert result["current_mode"] == expected
        assert result["status"] == "mode_changed"
        assert result["actor"] == "alice"

    def test_strict_mode_injects_expires_at_from_tracker(self):
        manager = SimpleNamespace(
            get_global_mode=lambda: OperatingMode.NORMAL,
            force_global_mode=lambda mode, reason: None,
        )
        tracker = SimpleNamespace(
            record_emergency_activation=lambda **kw: {"expiry_hours": 12}
        )
        with (
            self._patch_reliability_manager(manager),
            patch(
                "baldur_pro.services.governance.get_emergency_tracker",
                return_value=tracker,
            ),
            self._patch_audit_silent(),
        ):
            result = GovernanceApiService().set_mode("STRICT", actor="alice")
        assert "expires_at" in result
        assert result["expiry_hours"] == 12

    def test_strict_mode_returns_warning(self):
        manager = SimpleNamespace(
            get_global_mode=lambda: OperatingMode.NORMAL,
            force_global_mode=lambda mode, reason: None,
        )
        with (
            self._patch_reliability_manager(manager),
            self._patch_tracker_unavailable(),
            self._patch_audit_silent(),
        ):
            result = GovernanceApiService().set_mode("STRICT", actor="alice")
        assert "STRICT" in result["warning"]

    def test_normal_mode_returns_no_warning(self):
        manager = SimpleNamespace(
            get_global_mode=lambda: OperatingMode.NORMAL,
            force_global_mode=lambda mode, reason: None,
        )
        with (
            self._patch_reliability_manager(manager),
            self._patch_tracker_unavailable(),
            self._patch_audit_silent(),
        ):
            result = GovernanceApiService().set_mode("NORMAL", actor="alice")
        assert result["warning"] is None


# =============================================================================
# reconcile — public API
# =============================================================================


class TestReconcileBranches:
    def test_success_delegates_and_remaps_keys(self):
        service = SimpleNamespace(
            sync_metrics=lambda **kw: {
                "status": "ok",
                "synced_at": "2026-05-19T00:00:00Z",
                "actor": "alice",
                "dry_run": True,
                "results": {"r": 1},
                "summary": {"total": 2},
            }
        )
        with patch(
            "baldur.services.metric_sync_service.get_metric_sync_service",
            return_value=service,
        ):
            result = GovernanceApiService().reconcile(
                domains=["dlq"], dry_run=True, actor="alice"
            )
        assert result["reconciliation_result"] == "ok"
        assert result["reconciled_at"] == "2026-05-19T00:00:00Z"
        assert result["dry_run"] is True
        assert result["results"] == {"r": 1}
        assert result["summary"] == {"total": 2}

    def test_import_error_returns_unavailable(self):
        with patch(
            "baldur.services.metric_sync_service.get_metric_sync_service",
            side_effect=ImportError("absent"),
        ):
            result = GovernanceApiService().reconcile()
        assert result["reconciliation_result"] == "unavailable"

    def test_init_exception_returns_error(self):
        with patch(
            "baldur.services.metric_sync_service.get_metric_sync_service",
            side_effect=RuntimeError("broken"),
        ):
            result = GovernanceApiService().reconcile()
        assert result["reconciliation_result"] == "error"
        assert "broken" in result["error"]


# =============================================================================
# get_status — orchestrator
# =============================================================================


class TestGetStatusBehavior:
    def test_assembles_full_status_payload(self):
        manager = SimpleNamespace(
            get_all_states=lambda: {},
            get_global_mode=lambda: OperatingMode.NORMAL,
        )
        with (
            patch(
                "baldur.metrics.reliability_manager.get_reliability_manager",
                return_value=manager,
            ),
            patch(
                "baldur.services.metric_sync_service.get_metric_sync_service",
                side_effect=ImportError("absent"),
            ),
        ):
            result = GovernanceApiService().get_status()
        # Required keys present
        for key in (
            "generated_at",
            "operating_mode",
            "overall_health",
            "sync_status",
            "snapshot_health",
            "drift_summary",
            "domains",
            "next_sync_expected_at",
        ):
            assert key in result
        assert result["operating_mode"] == "normal"
        assert result["overall_health"] == "unknown"  # empty states


# =============================================================================
# Singleton accessors
# =============================================================================


class TestSingletonContract:
    def test_get_returns_same_instance(self):
        a = get_governance_api_service()
        b = get_governance_api_service()
        assert a is b

    def test_reset_clears_singleton(self):
        a = get_governance_api_service()
        reset_governance_api_service()
        b = get_governance_api_service()
        assert a is not b
