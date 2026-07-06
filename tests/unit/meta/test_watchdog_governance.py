"""
MetaWatchdog governance integration unit tests (409 C11-33).

Test targets:
    - _attempt_guarded_recovery() governance check
    - trace_id generation before recovery
    - Prometheus counter on governance block

Test categories:
    A. Contract: governance parameters (check_kill_switch=False, emergency_min_level=3, etc.)
    B. Behavior: governance blocked → return False, governance allowed → proceed,
                 fail-open on ImportError, trace_id set before governance call
"""

from __future__ import annotations

import pytest

pytest.importorskip("baldur_pro")

pytestmark = pytest.mark.requires_pro


from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest

from baldur.meta.config import MetaWatchdogSettings
from baldur.meta.health_probe import HealthProbeManager, HealthStatus, ProbeResult
from baldur_pro.services.meta_watchdog import SelfHealerWatchdog

# Patch _update_state_store to avoid Redis connection attempts
_MOCK_STATE_STORE = patch(
    "baldur_pro.services.meta_watchdog.SelfHealerWatchdog._update_state_store",
    return_value=None,
)


@pytest.fixture
def watchdog():
    """Watchdog in FULL recovery mode (558 D7) for governance testing.

    recovery_enabled=True keeps the governance → cooldown → recovery path
    reachable; the v1.0 default (False) would short-circuit to slice-A
    escalate-only before governance is ever consulted.
    """
    settings = MetaWatchdogSettings(
        enabled=True,
        probe_interval_seconds=5,
        self_cb_enabled=False,
        dry_run_mode=False,
        recovery_enabled=True,
        recovery_cooldown_seconds=30,
        escalation_delay_seconds=9999,
    )
    return SelfHealerWatchdog(
        settings=settings,
        probe_manager=MagicMock(spec=HealthProbeManager),
        escalation_manager=MagicMock(),
    )


@pytest.fixture
def unhealthy_result():
    return ProbeResult(
        component="redis",
        status=HealthStatus.UNHEALTHY,
        latency_ms=10,
        timestamp=datetime.now(UTC),
        error="connection refused",
    )


# =============================================================================
# A. Contract Tests
# =============================================================================


class TestWatchdogGovernanceParametersContract:
    """409 C11-33: Governance call parameters contract."""

    def test_governance_called_with_kill_switch_false(self, watchdog, unhealthy_result):
        """check_kill_switch=False — watchdog is last line of defense."""
        with (
            patch(
                "baldur_pro.services.governance.checks.check_all_governance"
            ) as mock_gov,
            patch(
                "baldur.audit.trace.generate_trace_id",
                return_value="req-test",
            ),
            patch(
                "baldur.audit.trace.set_trace_id",
            ),
        ):
            mock_gov.return_value = MagicMock(allowed=True)
            watchdog._attempt_guarded_recovery("redis", unhealthy_result, 30.0)

            mock_gov.assert_called_once()
            _, kwargs = mock_gov.call_args
            assert kwargs["check_kill_switch"] is False

    def test_governance_called_with_emergency_min_level_3(
        self, watchdog, unhealthy_result
    ):
        """emergency_min_level=3 — only LEVEL_3 stops watchdog."""
        with (
            patch(
                "baldur_pro.services.governance.checks.check_all_governance"
            ) as mock_gov,
            patch(
                "baldur.audit.trace.generate_trace_id",
                return_value="req-test",
            ),
            patch(
                "baldur.audit.trace.set_trace_id",
            ),
        ):
            mock_gov.return_value = MagicMock(allowed=True)
            watchdog._attempt_guarded_recovery("redis", unhealthy_result, 30.0)

            _, kwargs = mock_gov.call_args
            assert kwargs["emergency_min_level"] == 3

    def test_governance_called_with_error_budget_false(
        self, watchdog, unhealthy_result
    ):
        """check_error_budget=False — recovery is not SLO-consuming."""
        with (
            patch(
                "baldur_pro.services.governance.checks.check_all_governance"
            ) as mock_gov,
            patch(
                "baldur.audit.trace.generate_trace_id",
                return_value="req-test",
            ),
            patch(
                "baldur.audit.trace.set_trace_id",
            ),
        ):
            mock_gov.return_value = MagicMock(allowed=True)
            watchdog._attempt_guarded_recovery("redis", unhealthy_result, 30.0)

            _, kwargs = mock_gov.call_args
            assert kwargs["check_error_budget"] is False

    def test_governance_operation_name_includes_component(
        self, watchdog, unhealthy_result
    ):
        """operation_name includes the component name."""
        with (
            patch(
                "baldur_pro.services.governance.checks.check_all_governance"
            ) as mock_gov,
            patch(
                "baldur.audit.trace.generate_trace_id",
                return_value="req-test",
            ),
            patch(
                "baldur.audit.trace.set_trace_id",
            ),
        ):
            mock_gov.return_value = MagicMock(allowed=True)
            watchdog._attempt_guarded_recovery("redis", unhealthy_result, 30.0)

            _, kwargs = mock_gov.call_args
            assert kwargs["operation_name"] == "watchdog_recovery:redis"
            assert kwargs["service_name"] == "MetaWatchdog"
            assert kwargs["domain"] == "meta"


# =============================================================================
# B. Behavior Tests
# =============================================================================


class TestWatchdogGovernanceBehavior:
    """409 C11-33: Governance blocking/allowing/fail-open behavior."""

    def test_governance_blocked_returns_false(self, watchdog, unhealthy_result):
        """Governance blocked → recovery skipped, returns False."""
        mock_result = MagicMock(allowed=False, block_message="LEVEL_3 active")

        with (
            patch(
                "baldur_pro.services.governance.checks.check_all_governance",
                return_value=mock_result,
            ),
            patch(
                "baldur.audit.trace.generate_trace_id",
                return_value="req-test",
            ),
            patch(
                "baldur.audit.trace.set_trace_id",
            ),
            patch(
                "baldur.metrics.recorders.watchdog.record_watchdog_governance_blocked"
            ) as mock_metric,
        ):
            result = watchdog._attempt_guarded_recovery("redis", unhealthy_result, 30.0)

        assert result is False
        mock_metric.assert_called_once_with("redis")

    def test_governance_allowed_proceeds_to_recovery(self, watchdog, unhealthy_result):
        """Governance allowed → proceeds to recovery attempt."""
        mock_result = MagicMock(allowed=True)

        with (
            patch(
                "baldur_pro.services.governance.checks.check_all_governance",
                return_value=mock_result,
            ),
            patch(
                "baldur.audit.trace.generate_trace_id",
                return_value="req-test",
            ),
            patch(
                "baldur.audit.trace.set_trace_id",
            ),
            patch.object(
                watchdog, "_attempt_recovery", return_value=True
            ) as mock_recovery,
        ):
            watchdog._attempt_guarded_recovery("redis", unhealthy_result, 30.0)

        mock_recovery.assert_called_once()

    def test_governance_import_error_is_fail_open(self, watchdog, unhealthy_result):
        """ImportError from governance → fail-open, proceeds to recovery."""
        with (
            patch(
                "baldur_pro.services.governance.checks.check_all_governance",
                side_effect=ImportError("no governance"),
            ),
            patch(
                "baldur.audit.trace.generate_trace_id",
                return_value="req-test",
            ),
            patch(
                "baldur.audit.trace.set_trace_id",
            ),
            patch.object(
                watchdog, "_attempt_recovery", return_value=True
            ) as mock_recovery,
        ):
            watchdog._attempt_guarded_recovery("redis", unhealthy_result, 30.0)

        mock_recovery.assert_called_once()

    def test_governance_runtime_error_is_fail_open(self, watchdog, unhealthy_result):
        """RuntimeError from governance → fail-open, proceeds to recovery."""
        with (
            patch(
                "baldur_pro.services.governance.checks.check_all_governance",
                side_effect=RuntimeError("broken"),
            ),
            patch(
                "baldur.audit.trace.generate_trace_id",
                return_value="req-test",
            ),
            patch(
                "baldur.audit.trace.set_trace_id",
            ),
            patch.object(
                watchdog, "_attempt_recovery", return_value=True
            ) as mock_recovery,
        ):
            watchdog._attempt_guarded_recovery("redis", unhealthy_result, 30.0)

        mock_recovery.assert_called_once()

    def test_trace_id_set_before_governance_call(self, watchdog, unhealthy_result):
        """trace_id is generated and set before governance check."""
        call_order = []

        def mock_set_trace(tid):
            call_order.append(("set_trace_id", tid))

        def mock_gov(**kwargs):
            call_order.append(("check_all_governance",))
            return MagicMock(allowed=True)

        with (
            patch(
                "baldur.audit.trace.generate_trace_id",
                return_value="req-abc123",
            ),
            patch(
                "baldur.audit.trace.set_trace_id",
                side_effect=mock_set_trace,
            ),
            patch(
                "baldur_pro.services.governance.checks.check_all_governance",
                side_effect=mock_gov,
            ),
            patch.object(watchdog, "_attempt_recovery", return_value=True),
        ):
            watchdog._attempt_guarded_recovery("redis", unhealthy_result, 30.0)

        # trace_id set before governance call
        assert call_order[0] == ("set_trace_id", "req-abc123")
        assert call_order[1] == ("check_all_governance",)

    def test_dry_run_skips_governance_entirely(self, unhealthy_result):
        """dry_run_mode=True → governance never called."""
        settings = MetaWatchdogSettings(
            enabled=True,
            probe_interval_seconds=5,
            self_cb_enabled=False,
            dry_run_mode=True,
        )
        wd = SelfHealerWatchdog(
            settings=settings,
            probe_manager=MagicMock(spec=HealthProbeManager),
            escalation_manager=MagicMock(),
        )

        with patch(
            "baldur_pro.services.governance.checks.check_all_governance"
        ) as mock_gov:
            result = wd._attempt_guarded_recovery("redis", unhealthy_result, 30.0)

        assert result is False
        mock_gov.assert_not_called()
