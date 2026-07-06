"""
Canary Watchdog stage-timing tests.

The watchdog previously estimated all dwell times from rollout creation
(created_at), so every stage after the first effectively skipped its
observation window (auto-promoted on the next tick) and long-lived rollouts
were over-flagged as zombies. These tests pin the corrected anchors:

- auto_promote_eligible: observation window measured from stage entry
  (stage_started_at), created_at fallback for legacy rollouts.
- zombie detection (CANARY): stall clock anchored to stage_started_at.
- zombie detection (PAUSED): stall clock anchored to paused_at.
"""

import pytest

pytest.importorskip("baldur_pro")

pytestmark = pytest.mark.requires_pro


from datetime import timedelta
from unittest.mock import MagicMock, Mock, patch

from baldur.tasks.canary_watchdog import (
    CanaryWatchdogConfig,
    RolloutWatchdog,
    reset_watchdog,
)
from baldur.utils.time import utc_now
from baldur_pro.services.canary import CanaryRollout, CanaryStage, CanaryState

# =============================================================================
# Fixtures / helpers
# =============================================================================


@pytest.fixture(autouse=True)
def reset_singleton():
    """Reset the watchdog singleton around each test."""
    reset_watchdog()
    yield
    reset_watchdog()


@pytest.fixture
def watchdog_config():
    """Watchdog configuration for tests (notifications off)."""
    return CanaryWatchdogConfig(
        zombie_threshold_minutes=30,
        auto_rollback_after_minutes=60,
        enable_auto_promote=True,
        enable_auto_rollback=True,
        notification_enabled=False,
    )


def _make_rollout(
    state: CanaryState = CanaryState.CANARY,
    created_minutes_ago: float = 60,
    stage_started_minutes_ago: float | None = None,
    paused_minutes_ago: float | None = None,
    duration_minutes: int = 5,
) -> CanaryRollout:
    now = utc_now()
    return CanaryRollout(
        id="timing-test",
        config_type="circuit_breaker",
        previous_values={"failure_threshold": 5},
        new_values={"failure_threshold": 3},
        state=state,
        current_stage_index=0,
        stages=[
            CanaryStage(
                name="canary",
                clusters=["seoul-canary"],
                percentage=10.0,
                duration_minutes=duration_minutes,
                auto_promote=True,
            ),
        ],
        created_by="admin@example.com",
        created_at=now - timedelta(minutes=created_minutes_ago),
        stage_started_at=(
            now - timedelta(minutes=stage_started_minutes_ago)
            if stage_started_minutes_ago is not None
            else None
        ),
        paused_at=(
            now - timedelta(minutes=paused_minutes_ago)
            if paused_minutes_ago is not None
            else None
        ),
    )


def _watchdog_with(rollout: CanaryRollout, config) -> RolloutWatchdog:
    watchdog = RolloutWatchdog(config=config)
    mock_service = Mock()
    mock_service.get_active_rollouts.return_value = [rollout]
    mock_service.promote.return_value = True
    mock_service.rollback.return_value = False
    watchdog._service = mock_service
    return watchdog


# =============================================================================
# auto_promote_eligible — observation window from stage entry
# =============================================================================


class TestAutoPromoteStageAnchorBehavior:
    """Auto-promotion waits for the CURRENT stage's window, not rollout age."""

    def test_recent_stage_on_old_rollout_is_not_promoted(self):
        """An old rollout whose current stage just started must wait the
        stage's full observation window (the pre-fix behavior promoted it
        on the next tick because rollout age already exceeded duration)."""
        rollout = _make_rollout(
            created_minutes_ago=60,
            stage_started_minutes_ago=1,
            duration_minutes=5,
        )
        watchdog = _watchdog_with(
            rollout, CanaryWatchdogConfig(notification_enabled=False)
        )

        mock_governance = MagicMock()
        mock_governance.allowed = True
        with patch(
            "baldur_pro.services.governance.checks.check_all_governance",
            return_value=mock_governance,
        ):
            result = watchdog.auto_promote_eligible()

        assert result.promote_count == 0
        watchdog._service.promote.assert_not_called()

    def test_elapsed_stage_window_is_promoted(self):
        """Once the stage window has elapsed (from stage entry), promote."""
        rollout = _make_rollout(
            created_minutes_ago=60,
            stage_started_minutes_ago=6,
            duration_minutes=5,
        )
        watchdog = _watchdog_with(
            rollout, CanaryWatchdogConfig(notification_enabled=False)
        )

        mock_governance = MagicMock()
        mock_governance.allowed = True
        with patch(
            "baldur_pro.services.governance.checks.check_all_governance",
            return_value=mock_governance,
        ):
            result = watchdog.auto_promote_eligible()

        assert result.promote_count == 1
        watchdog._service.promote.assert_called_once_with(rollout.id, force=False)

    def test_legacy_rollout_falls_back_to_created_at(self):
        """Rollouts persisted before stage_started_at existed keep the old
        created_at anchor."""
        rollout = _make_rollout(
            created_minutes_ago=10,
            stage_started_minutes_ago=None,
            duration_minutes=5,
        )
        watchdog = _watchdog_with(
            rollout, CanaryWatchdogConfig(notification_enabled=False)
        )

        mock_governance = MagicMock()
        mock_governance.allowed = True
        with patch(
            "baldur_pro.services.governance.checks.check_all_governance",
            return_value=mock_governance,
        ):
            result = watchdog.auto_promote_eligible()

        assert result.promote_count == 1


# =============================================================================
# Zombie detection — per-state anchors
# =============================================================================


class TestZombieAnchorBehavior:
    """Stall clocks anchor to stage entry (CANARY) / pause time (PAUSED)."""

    def test_canary_recent_stage_on_old_rollout_is_not_zombie(self):
        """A 45-minute-old rollout whose stage started 2 minutes ago is
        healthy (pre-fix it was flagged from rollout age)."""
        rollout = _make_rollout(
            created_minutes_ago=45,
            stage_started_minutes_ago=2,
            duration_minutes=5,
        )
        watchdog = _watchdog_with(
            rollout, CanaryWatchdogConfig(notification_enabled=False)
        )

        result = watchdog.scan_and_handle()

        assert result.zombie_count == 0

    def test_canary_stalled_past_double_duration_is_zombie(self):
        """CANARY stuck past 2x the stage duration (from stage entry) is a
        zombie."""
        rollout = _make_rollout(
            created_minutes_ago=45,
            stage_started_minutes_ago=11,
            duration_minutes=5,
        )
        watchdog = _watchdog_with(
            rollout, CanaryWatchdogConfig(notification_enabled=False)
        )

        result = watchdog.scan_and_handle()

        assert result.zombie_count == 1
        assert result.zombies[0].stuck_minutes == pytest.approx(11, abs=0.5)

    def test_canary_fallback_to_created_at_when_anchor_missing(self):
        """Legacy rollouts (stage_started_at=None) keep the created_at clock."""
        rollout = _make_rollout(
            created_minutes_ago=45,
            stage_started_minutes_ago=None,
            duration_minutes=5,
        )
        watchdog = _watchdog_with(
            rollout, CanaryWatchdogConfig(notification_enabled=False)
        )

        result = watchdog.scan_and_handle()

        assert result.zombie_count == 1

    def test_paused_recent_pause_on_old_rollout_is_not_zombie(self):
        """PAUSED stall clock runs from paused_at, not rollout creation."""
        rollout = _make_rollout(
            state=CanaryState.PAUSED,
            created_minutes_ago=45,
            paused_minutes_ago=5,
        )
        watchdog = _watchdog_with(
            rollout,
            CanaryWatchdogConfig(
                zombie_threshold_minutes=30, notification_enabled=False
            ),
        )

        result = watchdog.scan_and_handle()

        assert result.zombie_count == 0

    def test_paused_past_threshold_is_zombie(self):
        """PAUSED past zombie_threshold_minutes (from paused_at) is a zombie."""
        rollout = _make_rollout(
            state=CanaryState.PAUSED,
            created_minutes_ago=120,
            paused_minutes_ago=35,
        )
        watchdog = _watchdog_with(
            rollout,
            CanaryWatchdogConfig(
                zombie_threshold_minutes=30, notification_enabled=False
            ),
        )

        result = watchdog.scan_and_handle()

        assert result.zombie_count == 1
        assert result.zombies[0].stuck_minutes == pytest.approx(35, abs=0.5)
