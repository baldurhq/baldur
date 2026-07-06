"""
Canary Error Budget Gate unit tests.

Verifies the 172_CANARY_ERROR_BUDGET_GATE.md implementation.

Tests:
    - auto_promote_eligible governance check
    - Zombie-exemption logic
    - pause() signature extension
    - promote() governance check
    - Break Glass behavior
    - Redis backward compatibility
"""

from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest


class TestWatchdogResult:
    """WatchdogResult extension tests."""

    def test_watchdog_result_has_governance_fields(self):
        """Verify WatchdogResult has governance fields."""
        from baldur.tasks.canary_watchdog import WatchdogResult

        result = WatchdogResult()

        assert hasattr(result, "governance_blocked")
        assert hasattr(result, "governance_block_reason")
        assert result.governance_blocked is False
        assert result.governance_block_reason == ""

    def test_watchdog_result_to_dict_includes_governance_fields(self):
        """Verify to_dict() includes the governance fields."""
        from baldur.tasks.canary_watchdog import WatchdogResult

        result = WatchdogResult(
            governance_blocked=True,
            governance_block_reason="Error budget critically low",
        )

        result_dict = result.to_dict()

        assert result_dict["governance_blocked"] is True
        assert result_dict["governance_block_reason"] == "Error budget critically low"


class TestCanaryRolloutModelExtensions:
    """CanaryRollout model extension tests."""

    @pytest.fixture(autouse=True)
    def _require_pro(self):
        pytest.importorskip("baldur_pro")

    def test_canary_rollout_has_pause_fields(self):
        """Verify CanaryRollout has the pause-related fields."""
        from baldur_pro.services.canary.models import CanaryRollout

        rollout = CanaryRollout(
            id="test-123",
            config_type="circuit_breaker",
            previous_values={},
            new_values={},
        )

        assert hasattr(rollout, "pause_reason")
        assert hasattr(rollout, "pause_triggered_by")
        assert hasattr(rollout, "paused_at")
        assert rollout.pause_reason is None
        assert rollout.pause_triggered_by is None
        assert rollout.paused_at is None

    def test_pause_trigger_priority_enum_exists(self):
        """Verify the PauseTriggerPriority Enum exists."""
        from baldur_pro.services.canary.models import PauseTriggerPriority

        assert PauseTriggerPriority.METRICS == 100
        assert PauseTriggerPriority.ERROR_BUDGET == 80
        assert PauseTriggerPriority.MANUAL == 10

    def test_trigger_priority_map_exists(self):
        """Verify TRIGGER_PRIORITY_MAP exists with correct values."""
        from baldur_pro.services.canary.models import TRIGGER_PRIORITY_MAP

        assert "error_budget" in TRIGGER_PRIORITY_MAP
        assert "governance" in TRIGGER_PRIORITY_MAP
        assert "manual" in TRIGGER_PRIORITY_MAP
        assert TRIGGER_PRIORITY_MAP["error_budget"] == 80

    def test_zombie_exempt_triggers_exists(self):
        """Verify ZOMBIE_EXEMPT_TRIGGERS exists."""
        from baldur_pro.services.canary.models import ZOMBIE_EXEMPT_TRIGGERS

        assert "error_budget" in ZOMBIE_EXEMPT_TRIGGERS
        assert "governance" in ZOMBIE_EXEMPT_TRIGGERS
        assert "manual" not in ZOMBIE_EXEMPT_TRIGGERS


class TestAutoPromoteGovernance:
    """auto_promote_eligible governance check tests."""

    @pytest.fixture(autouse=True)
    def _require_pro(self):
        pytest.importorskip("baldur_pro")

    @pytest.fixture
    def watchdog(self):
        """Watchdog fixture."""
        from baldur.tasks.canary_watchdog import (
            CanaryWatchdogConfig,
            RolloutWatchdog,
        )

        config = CanaryWatchdogConfig(enable_auto_promote=True)
        watchdog = RolloutWatchdog(config)
        watchdog._service = MagicMock()
        return watchdog

    def test_blocked_by_error_budget(self, watchdog):
        """Auto-promotion is blocked when the error budget is low."""
        from baldur_pro.services.governance.checks import (
            BlockReason,
            GovernanceCheckResult,
        )

        blocked_result = GovernanceCheckResult(
            allowed=False,
            block_reason=BlockReason.ERROR_BUDGET,
            block_message="Error budget critically low (5.0%)",
        )

        with patch(
            "baldur_pro.services.governance.checks.check_all_governance"
        ) as mock_gov:
            mock_gov.return_value = blocked_result

            result = watchdog.auto_promote_eligible()

            assert result.governance_blocked is True
            assert "error budget" in result.governance_block_reason.lower()
            assert result.promote_count == 0

    def test_blocked_by_emergency_mode(self, watchdog):
        """Auto-promotion is blocked in emergency mode."""
        from baldur_pro.services.governance.checks import (
            BlockReason,
            GovernanceCheckResult,
        )

        blocked_result = GovernanceCheckResult(
            allowed=False,
            block_reason=BlockReason.EMERGENCY_MODE,
            block_message="Emergency mode LEVEL_2 is active",
        )

        with patch(
            "baldur_pro.services.governance.checks.check_all_governance"
        ) as mock_gov:
            mock_gov.return_value = blocked_result

            result = watchdog.auto_promote_eligible()

            assert result.governance_blocked is True
            assert "emergency" in result.governance_block_reason.lower()

    def test_blocked_by_kill_switch(self, watchdog):
        """Auto-promotion is blocked when the Kill Switch is active."""
        from baldur_pro.services.governance.checks import (
            BlockReason,
            GovernanceCheckResult,
        )

        blocked_result = GovernanceCheckResult(
            allowed=False,
            block_reason=BlockReason.KILL_SWITCH,
            block_message="Kill Switch is active",
        )

        with patch(
            "baldur_pro.services.governance.checks.check_all_governance"
        ) as mock_gov:
            mock_gov.return_value = blocked_result

            result = watchdog.auto_promote_eligible()

            assert result.governance_blocked is True
            assert "kill switch" in result.governance_block_reason.lower()

    def test_allowed_when_governance_passes(self, watchdog):
        """Proceeds normally when governance passes."""
        from baldur_pro.services.governance.checks import GovernanceCheckResult

        allowed_result = GovernanceCheckResult.allowed_result()

        with patch(
            "baldur_pro.services.governance.checks.check_all_governance"
        ) as mock_gov:
            mock_gov.return_value = allowed_result
            watchdog.service.get_active_rollouts.return_value = []

            result = watchdog.auto_promote_eligible()

            assert result.governance_blocked is False
            mock_gov.assert_called_once()

    def test_fail_closed_on_governance_error(self, watchdog):
        """Fail-Closed when the governance check fails."""
        with patch(
            "baldur_pro.services.governance.checks.check_all_governance"
        ) as mock_gov:
            mock_gov.side_effect = Exception("Redis connection failed")

            result = watchdog.auto_promote_eligible()

            # Fail-Closed: blocked on error
            assert result.governance_blocked is True
            assert "error" in result.governance_block_reason.lower()


class TestZombieExemption:
    """Zombie-exemption tests."""

    @pytest.fixture(autouse=True)
    def _require_pro(self):
        pytest.importorskip("baldur_pro")

    @pytest.fixture
    def watchdog(self):
        """Watchdog fixture."""
        from baldur.tasks.canary_watchdog import (
            CanaryWatchdogConfig,
            RolloutWatchdog,
        )

        config = CanaryWatchdogConfig(zombie_threshold_minutes=30)
        return RolloutWatchdog(config)

    def test_error_budget_paused_not_zombie(self, watchdog):
        """A rollout PAUSED for the error_budget reason is not a Zombie."""
        from baldur.models.canary import CanaryState
        from baldur_pro.services.canary.models import CanaryRollout

        rollout = CanaryRollout(
            id="test-123",
            config_type="circuit_breaker",
            previous_values={},
            new_values={},
            state=CanaryState.PAUSED,
            pause_triggered_by="error_budget",
            created_at=datetime.utcnow() - timedelta(minutes=60),  # 60 min elapsed
        )

        now = datetime.utcnow()
        result = watchdog._check_zombie(rollout, now)

        assert result is None  # not a Zombie

    def test_governance_paused_not_zombie(self, watchdog):
        """A rollout PAUSED for the governance reason is not a Zombie."""
        from baldur.models.canary import CanaryState
        from baldur_pro.services.canary.models import CanaryRollout

        rollout = CanaryRollout(
            id="test-456",
            config_type="circuit_breaker",
            previous_values={},
            new_values={},
            state=CanaryState.PAUSED,
            pause_triggered_by="governance",
            created_at=datetime.utcnow() - timedelta(minutes=60),
        )

        now = datetime.utcnow()
        result = watchdog._check_zombie(rollout, now)

        assert result is None  # not a Zombie

    def test_manual_paused_is_zombie(self, watchdog):
        """A rollout PAUSED for the manual reason is a Zombie."""
        from baldur.models.canary import CanaryState
        from baldur_pro.services.canary.models import CanaryRollout

        rollout = CanaryRollout(
            id="test-789",
            config_type="circuit_breaker",
            previous_values={},
            new_values={},
            state=CanaryState.PAUSED,
            pause_triggered_by="manual",
            created_at=datetime.utcnow() - timedelta(minutes=60),
        )

        now = datetime.utcnow()
        result = watchdog._check_zombie(rollout, now)

        assert result is not None  # is a Zombie
        assert "Paused for" in result.reason

    def test_metrics_paused_is_zombie(self, watchdog):
        """A rollout PAUSED for the metrics reason is a Zombie."""
        from baldur.models.canary import CanaryState
        from baldur_pro.services.canary.models import CanaryRollout

        rollout = CanaryRollout(
            id="test-abc",
            config_type="circuit_breaker",
            previous_values={},
            new_values={},
            state=CanaryState.PAUSED,
            pause_triggered_by="metrics",
            created_at=datetime.utcnow() - timedelta(minutes=60),
        )

        now = datetime.utcnow()
        result = watchdog._check_zombie(rollout, now)

        assert result is not None  # is a Zombie

    def test_no_trigger_paused_is_zombie(self, watchdog):
        """A PAUSED rollout without pause_triggered_by is a Zombie."""
        from baldur.models.canary import CanaryState
        from baldur_pro.services.canary.models import CanaryRollout

        rollout = CanaryRollout(
            id="test-old",
            config_type="circuit_breaker",
            previous_values={},
            new_values={},
            state=CanaryState.PAUSED,
            # no pause_triggered_by (legacy data)
            created_at=datetime.utcnow() - timedelta(minutes=60),
        )

        now = datetime.utcnow()
        result = watchdog._check_zombie(rollout, now)

        assert result is not None  # is a Zombie


class TestBreakGlass:
    """Break Glass (emergency escape hatch) tests."""

    def test_break_glass_settings_exist(self):
        """Verify GovernanceSettings has the Break Glass settings."""
        from baldur.settings.governance import GovernanceSettings

        settings = GovernanceSettings()

        assert hasattr(settings, "break_glass_enabled")
        assert hasattr(settings, "break_glass_audit_required")
        assert settings.break_glass_enabled is False
        assert settings.break_glass_audit_required is True

    def test_break_glass_bypasses_all_checks(self):
        """When Break Glass is enabled, all checks are bypassed."""
        pytest.importorskip("baldur_pro")
        from baldur_pro.services.governance.checks import check_all_governance

        with patch(
            "baldur.settings.governance.get_governance_settings"
        ) as mock_settings:
            mock_settings.return_value.break_glass_enabled = True
            mock_settings.return_value.break_glass_audit_required = True

            with patch("baldur_pro.services.governance.checks._log_governance_blocked"):
                result = check_all_governance(
                    check_kill_switch=True,
                    check_emergency=True,
                    check_error_budget=True,
                    operation_name="test_operation",
                )

                assert result.allowed is True


class TestCanaryGovernanceSettings:
    """CanaryGovernanceSettings test."""

    def test_settings_exist(self):
        """CanaryGovernanceSettings has expected fields."""
        from baldur.settings.canary_governance import (
            get_canary_governance_settings,
        )

        settings = get_canary_governance_settings()

        assert hasattr(settings, "start_emergency_min_level")
        assert hasattr(settings, "bypass_min_reason_length")

    def test_default_values(self):
        """Verify default values."""
        from baldur.settings.canary_governance import CanaryGovernanceSettings

        settings = CanaryGovernanceSettings()

        assert settings.start_emergency_min_level == 2
        assert settings.rollback_emergency_min_level == 2
        assert settings.promote_emergency_min_level == 2
        assert settings.bypass_min_reason_length == 10


class TestCanaryAuditActionsExtension:
    """CANARY_ACTIONS extension tests."""

    def test_governance_actions_exist(self):
        """Verify governance-related actions are in CANARY_ACTIONS."""
        pytest.importorskip("baldur_pro")
        from baldur_pro.services.canary.audit import CANARY_ACTIONS

        assert "governance_blocked" in CANARY_ACTIONS
        assert "governance_bypass" in CANARY_ACTIONS


class TestRedisBackwardCompatibility:
    """Redis data backward-compatibility tests."""

    @pytest.fixture(autouse=True)
    def _require_pro(self):
        pytest.importorskip("baldur_pro")

    def test_deserialize_without_pause_fields(self):
        """Deserialize legacy data (no pause fields)."""
        from baldur_pro.services.canary.service import CanaryRolloutService

        old_data = {
            "id": "test-123",
            "config_type": "circuit_breaker",
            "previous_values": {},
            "new_values": {},
            "state": "paused",
            "current_stage_index": 0,
            "stages": [
                {
                    "name": "canary",
                    "clusters": ["test-cluster"],
                    "percentage": 10.0,
                }
            ],
            "created_by": "admin",
            "created_at": "2026-02-04T10:00:00",
            "reason": "Test",
            # no pause_reason, pause_triggered_by, paused_at
        }

        service = CanaryRolloutService()
        rollout = service._deserialize_rollout(old_data)

        assert rollout.pause_reason is None
        assert rollout.pause_triggered_by is None
        assert rollout.paused_at is None

    def test_deserialize_with_pause_fields(self):
        """Deserialize new data (with pause fields)."""
        from baldur_pro.services.canary.service import CanaryRolloutService

        new_data = {
            "id": "test-456",
            "config_type": "circuit_breaker",
            "previous_values": {},
            "new_values": {},
            "state": "paused",
            "current_stage_index": 0,
            "stages": [
                {
                    "name": "canary",
                    "clusters": ["test-cluster"],
                    "percentage": 10.0,
                }
            ],
            "created_by": "admin",
            "created_at": "2026-02-04T10:00:00",
            "reason": "Test",
            "pause_reason": "Error budget low",
            "pause_triggered_by": "error_budget",
            "paused_at": "2026-02-04T10:30:00",
        }

        service = CanaryRolloutService()
        rollout = service._deserialize_rollout(new_data)

        assert rollout.pause_reason == "Error budget low"
        assert rollout.pause_triggered_by == "error_budget"
        assert rollout.paused_at is not None

    def test_serialize_includes_pause_fields(self):
        """Verify serialization includes the pause fields."""
        from datetime import datetime

        from baldur.models.canary import CanaryStage, CanaryState
        from baldur_pro.services.canary.models import CanaryRollout
        from baldur_pro.services.canary.service import CanaryRolloutService

        rollout = CanaryRollout(
            id="test-789",
            config_type="circuit_breaker",
            previous_values={},
            new_values={},
            state=CanaryState.PAUSED,
            stages=[CanaryStage(name="canary", clusters=["test"], percentage=10.0)],
            created_by="admin",
            pause_reason="Error budget low",
            pause_triggered_by="error_budget",
            paused_at=datetime(2026, 2, 4, 10, 30, 0),
        )

        service = CanaryRolloutService()
        data = service._serialize_rollout(rollout)

        assert "pause_reason" in data
        assert "pause_triggered_by" in data
        assert "paused_at" in data
        assert data["pause_reason"] == "Error budget low"
        assert data["pause_triggered_by"] == "error_budget"


class TestPrometheusMetrics:
    """Prometheus metrics tests."""

    def test_canary_governance_metrics_exist(self):
        """Verify the Canary governance metrics exist."""
        from baldur.services.metrics.definitions import (
            canary_governance_blocked_total,
            canary_governance_bypass_total,
            canary_pending_promotion_gauge,
        )

        # Verify the metrics exist (imported without error).
        assert canary_governance_blocked_total is not None
        assert canary_pending_promotion_gauge is not None
        assert canary_governance_bypass_total is not None
