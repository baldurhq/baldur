"""
Tests for ReplayService.replay_on_circuit_close() misconfig observability (#496).

Verifies the 4-channel signal surface emitted when
`service_failure_type_map` has no entry for the recovered service:

1. WARNING log `replay_service.no_failure_types_mapped` with
   `service_name`, `block_reason`, `config_path` (D2 + D7)
2. EventBus emit `DLQ_REPLAY_BLOCKED` with payload carrying
   `trigger=circuit_close`, `service_name`, `block_reason`, `config_path`
   (D4 + D7)
3. `ReplayEventHandler.on_replay_blocked(service_name, REASON_...)` metric
   call (D3)
4. `log_dlq_replay_blocked_audit(domain="dlq", reason=..., service_name=...,
   trigger="circuit_close", details={"config_path": ...})` (D6 + D7)

Parametrized over the 3 upstream causes that converge on this branch:
- empty top-level map (`{}`)
- foreign service mapped, target service absent
- target service mapped but value is an empty list

Negative control: a populated map falls through to the governance check.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from structlog.testing import capture_logs

from baldur.services.event_bus.bus.event_types import EventType
from baldur.services.replay_service import ReplayService
from baldur.services.replay_service.service import (
    CONFIG_PATH_FAILURE_TYPE_MAP,
    REASON_NO_FAILURE_TYPE_MAPPING,
)

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def mock_event_bus():
    return MagicMock()


@pytest.fixture
def replay_service(mock_event_bus):
    """ReplayService with mock repository and injected mock event bus."""
    svc = ReplayService(repository=MagicMock())
    svc._event_bus = mock_event_bus
    return svc


# The 3 misconfig entry shapes that all converge on the same branch.
MISCONFIG_PARAMS = pytest.mark.parametrize(
    "service_failure_type_map",
    [
        pytest.param({}, id="empty_top_level_map"),
        pytest.param({"other_svc": ["TIMEOUT"]}, id="foreign_service_only"),
        pytest.param({"payment_api": []}, id="target_service_empty_list"),
    ],
)


# =============================================================================
# Contract — module-level constants frozen by D5 + D7
# =============================================================================


class TestNoMappingObservabilityConstantsContract:
    """Module-level constants — string equality (Contract)."""

    def test_reason_constant_value(self):
        """REASON_NO_FAILURE_TYPE_MAPPING is the D5 string literal."""
        assert REASON_NO_FAILURE_TYPE_MAPPING == "service_failure_type_map_unconfigured"

    def test_config_path_constant_value(self):
        """CONFIG_PATH_FAILURE_TYPE_MAP is the D7 RuntimeConfig key."""
        assert (
            CONFIG_PATH_FAILURE_TYPE_MAP == "replay_automation.service_failure_type_map"
        )


# =============================================================================
# Behavior — 4-channel signal surface (parametrized over misconfig shapes)
# =============================================================================


class TestReplayNoMappingObservabilityBehavior:
    """Misconfig branch emits the full 4-channel signal surface (D2-D7)."""

    @MISCONFIG_PARAMS
    def test_misconfig_emits_dlq_replay_blocked_event_with_full_payload(
        self,
        service_failure_type_map,
        replay_service,
        mock_event_bus,
    ):
        """DLQ_REPLAY_BLOCKED carries trigger, service_name, block_reason, config_path."""
        with patch(
            "baldur.services.replay_service.service.log_dlq_replay_blocked_audit"
        ):
            replay_service.replay_on_circuit_close(
                service_name="payment_api",
                service_failure_type_map=service_failure_type_map,
            )

        blocked_calls = [
            c
            for c in mock_event_bus.emit.call_args_list
            if c[0][0] == EventType.DLQ_REPLAY_BLOCKED
        ]
        assert len(blocked_calls) == 1
        data = blocked_calls[0][1]["data"]
        assert data == {
            "trigger": "circuit_close",
            "service_name": "payment_api",
            "block_reason": REASON_NO_FAILURE_TYPE_MAPPING,
            "config_path": CONFIG_PATH_FAILURE_TYPE_MAP,
        }

    @MISCONFIG_PARAMS
    def test_misconfig_calls_on_replay_blocked_with_service_name_and_reason(
        self,
        service_failure_type_map,
        replay_service,
    ):
        """Metric handler called with (service_name, REASON_...) — D3 arg order."""
        # ReplayEventHandler is imported lazily inside the misconfig branch,
        # so patch its module-level home rather than the service-side import.
        with (
            patch(
                "baldur.metrics.event_handlers.ReplayEventHandler.on_replay_blocked"
            ) as mock_metric,
            patch(
                "baldur.services.replay_service.service.log_dlq_replay_blocked_audit"
            ),
        ):
            replay_service.replay_on_circuit_close(
                service_name="payment_api",
                service_failure_type_map=service_failure_type_map,
            )

        mock_metric.assert_called_once_with(
            "payment_api", REASON_NO_FAILURE_TYPE_MAPPING
        )

    @MISCONFIG_PARAMS
    def test_misconfig_calls_log_dlq_replay_blocked_audit_with_full_kwargs(
        self,
        service_failure_type_map,
        replay_service,
    ):
        """Audit helper called with domain/reason/service_name/trigger/details (D6+D7)."""
        with patch(
            "baldur.services.replay_service.service.log_dlq_replay_blocked_audit"
        ) as mock_audit:
            replay_service.replay_on_circuit_close(
                service_name="payment_api",
                service_failure_type_map=service_failure_type_map,
            )

        mock_audit.assert_called_once_with(
            domain="dlq",
            reason=REASON_NO_FAILURE_TYPE_MAPPING,
            service_name="payment_api",
            trigger="circuit_close",
            details={"config_path": CONFIG_PATH_FAILURE_TYPE_MAP},
        )

    # 525 D4: xdist mock_leak — structlog capture_logs context races with
    # sibling tests under -n 6 (project_xdist_isolation pattern).
    @pytest.mark.flaky_quarantine(
        issue="525", first_seen="2026-05-20", category="mock_leak"
    )
    @MISCONFIG_PARAMS
    def test_misconfig_emits_warning_level_log_with_structured_fields(
        self,
        service_failure_type_map,
        replay_service,
    ):
        """WARNING log `replay_service.no_failure_types_mapped` carries structured fields."""
        with (
            patch(
                "baldur.services.replay_service.service.log_dlq_replay_blocked_audit"
            ),
            capture_logs() as cap_logs,
        ):
            replay_service.replay_on_circuit_close(
                service_name="payment_api",
                service_failure_type_map=service_failure_type_map,
            )

        matching = [
            entry
            for entry in cap_logs
            if entry.get("event") == "replay_service.no_failure_types_mapped"
        ]
        assert len(matching) == 1
        log = matching[0]
        assert log["log_level"] == "warning"
        assert log["service_name"] == "payment_api"
        assert log["block_reason"] == REASON_NO_FAILURE_TYPE_MAPPING
        assert log["config_path"] == CONFIG_PATH_FAILURE_TYPE_MAP

    @MISCONFIG_PARAMS
    def test_misconfig_returns_empty_batch_replay_result(
        self,
        service_failure_type_map,
        replay_service,
    ):
        """Misconfig branch returns BatchReplayResult() with no items processed."""
        with patch(
            "baldur.services.replay_service.service.log_dlq_replay_blocked_audit"
        ):
            result = replay_service.replay_on_circuit_close(
                service_name="payment_api",
                service_failure_type_map=service_failure_type_map,
            )

        assert result.total == 0
        assert result.success_count == 0
        assert result.failed_count == 0
        assert result.governance_blocked is False

    @MISCONFIG_PARAMS
    def test_misconfig_bypasses_governance_check(
        self,
        service_failure_type_map,
        replay_service,
    ):
        """D1 isolation: misconfig early-return precedes check_all_governance call."""
        pytest.importorskip("baldur_pro")
        with (
            patch(
                "baldur_pro.services.governance.checks.check_all_governance",
            ) as mock_governance,
            patch(
                "baldur.services.replay_service.service.log_dlq_replay_blocked_audit"
            ),
        ):
            replay_service.replay_on_circuit_close(
                service_name="payment_api",
                service_failure_type_map=service_failure_type_map,
            )

        mock_governance.assert_not_called()


# =============================================================================
# Negative control — populated map flows through to governance
# =============================================================================


class TestReplayNoMappingObservabilityNegativeControlBehavior:
    """Populated map: misconfig branch is NOT taken; control falls through."""

    @pytest.fixture(autouse=True)
    def _require_pro(self):
        pytest.importorskip("baldur_pro")

    def test_populated_map_does_not_emit_misconfig_log(self, replay_service):
        """No `replay_service.no_failure_types_mapped` log when map is populated."""
        with (
            patch(
                "baldur_pro.services.governance.checks.check_all_governance",
            ) as mock_governance,
            patch(
                "baldur.services.replay_service.service.log_dlq_replay_blocked_audit"
            ),
            capture_logs() as cap_logs,
        ):
            mock_governance.return_value = MagicMock(
                allowed=False, block_reason=None, block_message="stub"
            )
            replay_service.replay_on_circuit_close(
                service_name="payment_api",
                service_failure_type_map={"payment_api": ["PG_TIMEOUT"]},
            )

        misconfig_logs = [
            e
            for e in cap_logs
            if e.get("event") == "replay_service.no_failure_types_mapped"
        ]
        assert misconfig_logs == []

    def test_populated_map_does_not_call_misconfig_audit_helper(self, replay_service):
        """log_dlq_replay_blocked_audit is NOT called on the populated-map path."""
        with (
            patch(
                "baldur_pro.services.governance.checks.check_all_governance",
            ) as mock_governance,
            patch(
                "baldur.services.replay_service.service.log_dlq_replay_blocked_audit"
            ) as mock_audit,
        ):
            mock_governance.return_value = MagicMock(
                allowed=False, block_reason=None, block_message="stub"
            )
            replay_service.replay_on_circuit_close(
                service_name="payment_api",
                service_failure_type_map={"payment_api": ["PG_TIMEOUT"]},
            )

        mock_audit.assert_not_called()

    def test_populated_map_invokes_governance_check(self, replay_service):
        """Populated map proceeds past the misconfig early-return into governance."""
        with (
            patch(
                "baldur_pro.services.governance.checks.check_all_governance",
            ) as mock_governance,
            patch(
                "baldur.services.replay_service.service.log_dlq_replay_blocked_audit"
            ),
        ):
            mock_governance.return_value = MagicMock(
                allowed=False, block_reason=None, block_message="stub"
            )
            replay_service.replay_on_circuit_close(
                service_name="payment_api",
                service_failure_type_map={"payment_api": ["PG_TIMEOUT"]},
            )

        mock_governance.assert_called_once()


# =============================================================================
# Behavior — duplicate failure types (D5 dedup interaction)
# =============================================================================


class TestReplayNoMappingDedupBehavior:
    """Order-preserving dedup at the operator boundary still skips misconfig branch."""

    @pytest.fixture(autouse=True)
    def _require_pro(self):
        pytest.importorskip("baldur_pro")

    def test_duplicate_failure_types_do_not_collapse_to_empty(self, replay_service):
        """`["TIMEOUT", "TIMEOUT"]` dedups to non-empty → misconfig branch NOT taken."""
        with (
            patch(
                "baldur_pro.services.governance.checks.check_all_governance",
            ) as mock_governance,
            patch(
                "baldur.services.replay_service.service.log_dlq_replay_blocked_audit"
            ) as mock_audit,
        ):
            mock_governance.return_value = MagicMock(
                allowed=False, block_reason=None, block_message="stub"
            )
            replay_service.replay_on_circuit_close(
                service_name="payment_api",
                service_failure_type_map={"payment_api": ["TIMEOUT", "TIMEOUT"]},
            )

        # Misconfig audit NOT called — governance check IS called.
        mock_audit.assert_not_called()
        mock_governance.assert_called_once()
