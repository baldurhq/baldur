"""Canary watchdog stall/rollback alert payload.

Both watchdog ``notify()`` call sites passed a Slack channel *name* where the
payload carries a channel *type* filter, so the resolved channel set was always
empty and every alert degraded to an INFO log. The payload is now pinned to the
slack type, carries the OPERATIONS category (a rollout is a deployment
operation; the chaos category belongs to chaos *experiments*), and gets a
dedup key scoped per rollout AND per event type.

Scoping matters in three directions and each is asserted below: two distinct
zombies in one scan both deliver, the same zombie re-detected on the next scan
collapses onto one key, and a rollback alert is not debounced by the earlier
detection alert for the same rollout.

The delivery half — that the resolved channel set is actually non-empty once
the filter is a type — is a composition property of the unified manager and is
covered by the PRO integration test, not here.
"""

from __future__ import annotations

import pytest

pytest.importorskip("baldur_pro")

pytestmark = pytest.mark.requires_pro


from datetime import timedelta
from unittest.mock import Mock, patch

from baldur.models.notification import (
    NotificationCategory,
    NotificationPriority,
)
from baldur.tasks.canary_watchdog import (
    CanaryWatchdogConfig,
    RolloutWatchdog,
    ZombieRollout,
)
from baldur.utils.time import utc_now
from baldur_pro.services.unified_notification.service import (
    UnifiedNotificationManager,
)

_MANAGER_PRODUCER = (
    "baldur_pro.services.unified_notification.get_unified_notification_manager"
)


def _zombie(rollout_id: str = "stuck1", config_type: str = "circuit_breaker"):
    return ZombieRollout(
        rollout_id=rollout_id,
        config_type=config_type,
        state="canary",
        stuck_since=utc_now() - timedelta(minutes=45),
        stuck_minutes=45.0,
        created_by="operator@example.com",
        affected_clusters=["seoul-canary", "tokyo"],
        reason="No stage progress for 45 minutes",
    )


def _notify_payloads(event_types, zombies=None):
    """Drive ``_send_notification`` once per event and collect the payloads."""
    watchdog = RolloutWatchdog(config=CanaryWatchdogConfig(notification_enabled=True))
    zombies = zombies or [_zombie() for _ in event_types]

    manager = Mock(spec=UnifiedNotificationManager)
    with patch(_MANAGER_PRODUCER, return_value=manager):
        for zombie, event_type in zip(zombies, event_types, strict=True):
            watchdog._send_notification(zombie, event_type)

    return [call.args[0] for call in manager.notify.call_args_list]


# =============================================================================
# Payload shape and dedup-key scoping
# =============================================================================


class TestCanaryWatchdogNotificationBehavior:
    """The stall alert routes to a channel type and dedups per rollout+event."""

    @pytest.mark.parametrize(
        "event_type",
        ["zombie_detected", "auto_rolled_back"],
    )
    def test_payload_filters_on_the_slack_channel_type(self, event_type):
        """``channels`` is a type filter, not a target.

        A channel name here matched no type, so the resolver intersected it to
        an empty set and the alert never left the process.
        """
        (payload,) = _notify_payloads([event_type])

        assert payload.channels == ["slack"]

    @pytest.mark.parametrize(
        "event_type",
        ["zombie_detected", "auto_rolled_back"],
    )
    def test_payload_carries_the_operations_category_at_high_priority(self, event_type):
        """A stalled rollout is a deployment operation, not a chaos experiment."""
        (payload,) = _notify_payloads([event_type])

        assert payload.category == NotificationCategory.OPERATIONS
        assert payload.priority == NotificationPriority.HIGH
        assert payload.source == "canary_watchdog"

    @pytest.mark.parametrize(
        ("event_type", "expected_prefix"),
        [
            ("zombie_detected", "canary_zombie:"),
            ("auto_rolled_back", "canary_rollback:"),
        ],
    )
    def test_dedup_key_is_scoped_per_rollout_and_event(
        self, event_type, expected_prefix
    ):
        """Rollout id and event type both appear in the key."""
        (payload,) = _notify_payloads([event_type], [_zombie(rollout_id="stuck7")])

        assert payload.dedup_key == f"{expected_prefix}stuck7"

    def test_two_zombies_in_one_scan_produce_two_distinct_keys(self):
        """Distinct rollouts must not debounce each other.

        A key scoped to the event alone would deliver the first stall and
        silence every other rollout that stalled in the same window.
        """
        payloads = _notify_payloads(
            ["zombie_detected", "zombie_detected"],
            [_zombie(rollout_id="stuck1"), _zombie(rollout_id="stuck2")],
        )

        assert [p.dedup_key for p in payloads] == [
            "canary_zombie:stuck1",
            "canary_zombie:stuck2",
        ]

    def test_same_zombie_re_detected_reuses_one_key(self):
        """Idempotent across scans: the cooldown window has something to collapse.

        The scan runs every 5 minutes and a stall lasts far longer, so a key
        that varied per scan would page on every tick.
        """
        payloads = _notify_payloads(
            ["zombie_detected", "zombie_detected"],
            [_zombie(rollout_id="stuck1"), _zombie(rollout_id="stuck1")],
        )

        assert len({p.dedup_key for p in payloads}) == 1

    def test_rollback_alert_is_not_debounced_by_the_detection_alert(self):
        """The two events for one rollout are different news.

        Auto-rollback follows detection for the same rollout inside one
        cooldown window; a rollout-only key would suppress the alert that says
        the config was actually reverted.
        """
        payloads = _notify_payloads(
            ["zombie_detected", "auto_rolled_back"],
            [_zombie(rollout_id="stuck1"), _zombie(rollout_id="stuck1")],
        )

        assert payloads[0].dedup_key != payloads[1].dedup_key

    def test_message_names_the_rollout_and_the_stall(self):
        """The alert has to be actionable without opening the console."""
        (payload,) = _notify_payloads(["zombie_detected"], [_zombie("stuck9")])

        assert "stuck9" in payload.message
        assert "circuit_breaker" in payload.message
        assert "45" in payload.message

    def test_rollback_message_names_the_affected_clusters(self):
        """The rollback alert says where the previous config was restored."""
        (payload,) = _notify_payloads(["auto_rolled_back"])

        assert "seoul-canary" in payload.message
        assert "tokyo" in payload.message

    def test_unknown_event_type_sends_nothing(self):
        """The builder is closed to its two events — no half-built payload."""
        watchdog = RolloutWatchdog(
            config=CanaryWatchdogConfig(notification_enabled=True)
        )
        manager = Mock(spec=UnifiedNotificationManager)

        with patch(_MANAGER_PRODUCER, return_value=manager):
            watchdog._send_notification(_zombie(), "something_else")

        manager.notify.assert_not_called()

    def test_notification_failure_does_not_escape_the_scan(self):
        """Fail-open: an alerting outage must not stop lock renewal or rollback."""
        watchdog = RolloutWatchdog(
            config=CanaryWatchdogConfig(notification_enabled=True)
        )

        with patch(_MANAGER_PRODUCER, side_effect=RuntimeError("notify down")):
            watchdog._send_notification(_zombie(), "zombie_detected")

    def test_notification_failure_is_reported_at_warning(self):
        """An undelivered stall alert is an operator-visible loss."""
        import baldur.tasks.canary_watchdog as watchdog_module

        watchdog = RolloutWatchdog(
            config=CanaryWatchdogConfig(notification_enabled=True)
        )

        with patch(_MANAGER_PRODUCER, side_effect=RuntimeError("notify down")):
            with patch.object(watchdog_module, "logger") as mock_logger:
                watchdog._send_notification(_zombie(), "zombie_detected")

        assert [call.args[0] for call in mock_logger.warning.call_args_list] == [
            "watchdog.notification_failed"
        ]
