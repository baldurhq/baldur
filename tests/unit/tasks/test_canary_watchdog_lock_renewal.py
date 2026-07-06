"""Canary watchdog config-lock renewal wiring tests (623).

Covers the watchdog half of the renewal wiring:
    A. RolloutWatchdog._renew_lock — eligibility (renews CANARY / exempt-PAUSED
       / zombie-flagged; skips CREATED + terminal), per-rollout isolation, and
       result-counter increments per outcome (D1/D2).
    B. RolloutWatchdog.scan_and_handle — renews every active rollout with no
       governance gate (pins the absence — D5) and before the zombie check
       (D1).
    C. RolloutWatchdog._notify_lock_conflict — gated, config-type-scoped,
       CHAOS-category Slack alert (D4).
    D. WatchdogResult — renewal counters present in defaults and to_dict (D10).

The watchdog talks to a Mock service (existing precedent in
test_canary_watchdog.py); renewal is observable via WatchdogResult counters and
Mock.assert_* on renew_config_lock.
"""

import pytest

pytest.importorskip("baldur_pro")

pytestmark = pytest.mark.requires_pro


from datetime import timedelta
from unittest.mock import Mock, patch

from baldur.models.canary import LockRenewalOutcome
from baldur.models.notification import (
    NotificationCategory,
    NotificationPriority,
)
from baldur.tasks.canary_watchdog import (
    CanaryWatchdogConfig,
    RolloutWatchdog,
    WatchdogResult,
)
from baldur.utils.time import utc_now
from baldur_pro.services.canary import (
    CanaryRollout,
    CanaryStage,
    CanaryState,
)

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def watchdog_config():
    """Watchdog config with notifications off by default (tests opt in)."""
    return CanaryWatchdogConfig(
        zombie_threshold_minutes=30,
        auto_rollback_after_minutes=60,
        enable_auto_promote=True,
        enable_auto_rollback=True,
        notification_enabled=False,
        slack_channel="#canary-alerts",
    )


def _rollout(
    state: CanaryState = CanaryState.CANARY,
    rollout_id: str = "active1",
    config_type: str = "circuit_breaker",
    age_minutes: int = 1,
) -> CanaryRollout:
    return CanaryRollout(
        id=rollout_id,
        config_type=config_type,
        previous_values={"failure_threshold": 5},
        new_values={"failure_threshold": 3},
        state=state,
        current_stage_index=0,
        stages=[
            CanaryStage(
                name="canary",
                clusters=["seoul-canary"],
                percentage=10.0,
                duration_minutes=5,
            ),
        ],
        created_by="admin@example.com",
        created_at=utc_now() - timedelta(minutes=age_minutes),
        reason="Renewal wiring test",
    )


def _watchdog_with_service(config, service):
    watchdog = RolloutWatchdog(config=config)
    watchdog._service = service
    return watchdog


# =============================================================================
# A. _renew_lock eligibility, isolation, counters (D1/D2)
# =============================================================================


class TestWatchdogRenewLockBehavior:
    """_renew_lock renews every active rollout except CREATED/terminal, isolates
    failures, and increments the right counter per outcome."""

    @pytest.mark.parametrize(
        "state",
        [CanaryState.CANARY, CanaryState.PAUSED, CanaryState.PROMOTING],
        ids=["canary", "exempt_paused", "promoting"],
    )
    def test_renew_lock_renews_active_states(self, watchdog_config, state):
        # Given: an active (non-terminal, started) rollout.
        service = Mock()
        service.renew_config_lock.return_value = LockRenewalOutcome.RENEWED
        watchdog = _watchdog_with_service(watchdog_config, service)
        rollout = _rollout(state=state)
        result = WatchdogResult()

        # When
        watchdog._renew_lock(rollout, result)

        # Then
        service.renew_config_lock.assert_called_once_with(rollout)
        assert result.renewed_count == 1
        assert result.renewal_failed_count == 0

    def test_renew_lock_renews_zombie_flagged_rollout(self, watchdog_config):
        # Given: a stuck-but-applying rollout (would be zombie-flagged) is still
        # renewed — losing its lock yields silent corruption (D2).
        service = Mock()
        service.renew_config_lock.return_value = LockRenewalOutcome.RENEWED
        watchdog = _watchdog_with_service(watchdog_config, service)
        zombie = _rollout(state=CanaryState.CANARY, age_minutes=45)
        result = WatchdogResult()

        watchdog._renew_lock(zombie, result)

        service.renew_config_lock.assert_called_once_with(zombie)
        assert result.renewed_count == 1

    def test_created_rollout_not_renewed(self, watchdog_config):
        # Given: a never-started rollout has no liveness signal (D2 carve-out).
        service = Mock()
        watchdog = _watchdog_with_service(watchdog_config, service)
        rollout = _rollout(state=CanaryState.CREATED)
        result = WatchdogResult()

        watchdog._renew_lock(rollout, result)

        service.renew_config_lock.assert_not_called()
        assert result.renewed_count == 0
        assert result.renewal_failed_count == 0

    @pytest.mark.parametrize(
        "state",
        [
            CanaryState.COMPLETED,
            CanaryState.ROLLED_BACK,
            CanaryState.FAILED,
            CanaryState.CANCELLED,
        ],
    )
    def test_terminal_rollout_not_renewed(self, watchdog_config, state):
        service = Mock()
        watchdog = _watchdog_with_service(watchdog_config, service)
        result = WatchdogResult()

        watchdog._renew_lock(_rollout(state=state), result)

        service.renew_config_lock.assert_not_called()
        assert result.renewed_count == 0

    def test_renew_lock_reacquired_counts_as_renewed(self, watchdog_config):
        service = Mock()
        service.renew_config_lock.return_value = LockRenewalOutcome.REACQUIRED
        watchdog = _watchdog_with_service(watchdog_config, service)
        result = WatchdogResult()

        watchdog._renew_lock(_rollout(), result)

        assert result.renewed_count == 1
        assert result.renewal_failed_count == 0

    def test_renew_lock_failed_increments_failed_counter(self, watchdog_config):
        service = Mock()
        service.renew_config_lock.return_value = LockRenewalOutcome.FAILED
        watchdog = _watchdog_with_service(watchdog_config, service)
        result = WatchdogResult()

        watchdog._renew_lock(_rollout(), result)

        assert result.renewed_count == 0
        assert result.renewal_failed_count == 1

    def test_renew_lock_skipped_increments_nothing(self, watchdog_config):
        service = Mock()
        service.renew_config_lock.return_value = LockRenewalOutcome.SKIPPED
        watchdog = _watchdog_with_service(watchdog_config, service)
        result = WatchdogResult()

        watchdog._renew_lock(_rollout(), result)

        assert result.renewed_count == 0
        assert result.renewal_failed_count == 0

    def test_renew_lock_isolates_service_exception(self, watchdog_config):
        # Given: the service raises — one bad rollout must not abort the scan.
        service = Mock()
        service.renew_config_lock.side_effect = RuntimeError("boom")
        watchdog = _watchdog_with_service(watchdog_config, service)
        result = WatchdogResult()

        # When / Then: no exception propagates, counted as a failure.
        watchdog._renew_lock(_rollout(), result)

        assert result.renewal_failed_count == 1

    def test_renew_lock_conflict_notifies_when_enabled(self):
        # Given: notifications enabled and a CONFLICT outcome.
        config = CanaryWatchdogConfig(
            notification_enabled=True, slack_channel="#canary-alerts"
        )
        service = Mock()
        service.renew_config_lock.return_value = LockRenewalOutcome.CONFLICT
        watchdog = _watchdog_with_service(config, service)
        rollout = _rollout()
        result = WatchdogResult()

        with patch.object(watchdog, "_notify_lock_conflict") as mock_notify:
            watchdog._renew_lock(rollout, result)

        mock_notify.assert_called_once_with(rollout)
        assert result.renewal_failed_count == 1

    def test_renew_lock_conflict_silent_when_disabled(self, watchdog_config):
        # Given: notifications disabled (watchdog_config) and a CONFLICT.
        service = Mock()
        service.renew_config_lock.return_value = LockRenewalOutcome.CONFLICT
        watchdog = _watchdog_with_service(watchdog_config, service)
        result = WatchdogResult()

        with patch.object(watchdog, "_notify_lock_conflict") as mock_notify:
            watchdog._renew_lock(_rollout(), result)

        mock_notify.assert_not_called()
        assert result.renewal_failed_count == 1


# =============================================================================
# B. scan_and_handle renewal wiring (D1/D5)
# =============================================================================


class TestWatchdogScanRenewsLocks:
    """scan_and_handle renews every active rollout, ungated by governance and
    before the zombie check."""

    def test_scan_renews_each_active_rollout(self, watchdog_config):
        service = Mock()
        rollouts = [
            _rollout(state=CanaryState.CANARY, rollout_id="r1"),
            _rollout(state=CanaryState.PAUSED, rollout_id="r2", config_type="dlq"),
        ]
        service.get_active_rollouts.return_value = rollouts
        service.renew_config_lock.return_value = LockRenewalOutcome.RENEWED
        watchdog = _watchdog_with_service(watchdog_config, service)

        result = watchdog.scan_and_handle()

        assert service.renew_config_lock.call_count == 2
        assert result.renewed_count == 2

    def test_scan_renews_lock_even_when_governance_blocked(self, watchdog_config):
        # scan_and_handle consults no governance gate on the renewal path: even
        # when governance reports blocked, each active rollout is still renewed
        # (this pins the absence of a gate — D5).
        service = Mock()
        rollout = _rollout(state=CanaryState.PAUSED, age_minutes=40)
        service.get_active_rollouts.return_value = [rollout]
        service.renew_config_lock.return_value = LockRenewalOutcome.RENEWED
        watchdog = _watchdog_with_service(watchdog_config, service)

        blocked = Mock()
        blocked.allowed = False
        blocked.block_message = "kill switch active"
        with patch("baldur.factory.registry.ProviderRegistry") as mock_registry:
            mock_registry.governance.get.return_value.check_all_governance.return_value = blocked
            result = watchdog.scan_and_handle()

        service.renew_config_lock.assert_called_once_with(rollout)
        assert result.renewed_count == 1

    def test_scan_renews_before_zombie_check(self, watchdog_config):
        # A zombie's lock must be renewed before the zombie is handled so it
        # cannot lapse mid-handling (D1).
        order: list[str] = []
        service = Mock()
        zombie = _rollout(
            state=CanaryState.CANARY, rollout_id="zombie1", age_minutes=45
        )
        service.get_active_rollouts.return_value = [zombie]
        service.rollback.return_value = False

        def _record_renew(_rollout):
            order.append("renew")
            return LockRenewalOutcome.RENEWED

        service.renew_config_lock.side_effect = _record_renew
        watchdog = _watchdog_with_service(watchdog_config, service)

        real_check = watchdog._check_zombie

        def _record_zombie(rollout, now):
            order.append("zombie_check")
            return real_check(rollout, now)

        with patch.object(watchdog, "_check_zombie", side_effect=_record_zombie):
            result = watchdog.scan_and_handle()

        assert order == ["renew", "zombie_check"]
        assert result.renewed_count == 1
        assert result.zombie_count == 1


# =============================================================================
# C. _notify_lock_conflict — gated, dedup-keyed, CHAOS-category alert (D4)
# =============================================================================


class TestLockConflictNotification:
    """The conflict alert is built directly (not via _send_notification) with a
    config-type-scoped dedup_key and CHAOS category."""

    def test_notify_lock_conflict_builds_dedup_keyed_chaos_payload(self):
        config = CanaryWatchdogConfig(
            notification_enabled=True, slack_channel="#canary-alerts"
        )
        watchdog = RolloutWatchdog(config=config)
        rollout = _rollout(rollout_id="victim1", config_type="circuit_breaker")

        with patch(
            "baldur_pro.services.unified_notification.get_unified_notification_manager"
        ) as mock_get:
            manager = Mock()
            mock_get.return_value = manager
            watchdog._notify_lock_conflict(rollout)

        manager.notify.assert_called_once()
        payload = manager.notify.call_args.args[0]
        assert payload.dedup_key == "canary_lock_conflict:circuit_breaker"
        assert payload.category == NotificationCategory.CHAOS
        assert payload.priority == NotificationPriority.HIGH
        assert payload.source == "canary_watchdog"
        assert payload.channels == ["#canary-alerts"]
        assert "victim1" in payload.message

    def test_notify_lock_conflict_isolates_notification_error(self):
        # A failing notification manager must not raise out of the watchdog.
        config = CanaryWatchdogConfig(notification_enabled=True)
        watchdog = RolloutWatchdog(config=config)

        with patch(
            "baldur_pro.services.unified_notification.get_unified_notification_manager",
            side_effect=RuntimeError("notify down"),
        ):
            # Should not raise.
            watchdog._notify_lock_conflict(_rollout())


# =============================================================================
# D. WatchdogResult renewal counters (D10)
# =============================================================================


class TestWatchdogResultContract:
    """WatchdogResult exposes renewal counters in defaults and to_dict."""

    def test_renewal_counters_default_to_zero(self):
        result = WatchdogResult()

        assert result.renewed_count == 0
        assert result.renewal_failed_count == 0

    def test_to_dict_includes_renewal_counters(self):
        result = WatchdogResult(renewed_count=3, renewal_failed_count=2)

        d = result.to_dict()

        assert d["renewed_count"] == 3
        assert d["renewal_failed_count"] == 2
