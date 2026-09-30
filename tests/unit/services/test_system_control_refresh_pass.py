"""System Control copy: lock-free readers and the refresh pass that keeps them current.

Target: ``baldur.services.system_control.SystemControlManager`` — readers
answer from an immutable snapshot with no lock, no store I/O and no raise; a
refresh pass reads strictly and assigns the result unless a local write landed
since the pass began (the generation guard); a pass that observes a kill-switch
transition this process's subscribers did not hear announces it to them only
(``publish_local``), recording it as heard only once the publish returned, and
never counting a throttle Full Stop as heard.

Verification techniques applied (§8):
  - §8.7 Concurrency — readers finish while every manager lock is held; a
    local write racing a pass's read is never overwritten by it
  - §8.4 Side effects — the local announcement, its source and payload, the
    INFO line and gauges on a copy change
  - §8.12 Branch outcome — heard vs unheard, system-control vs throttle source,
    a failed announcement repeated
  - §8.2 Exception/edge cases — every reader stays answerable when the store,
    the refresher or the manager itself fails
  - §8.6 Data immutability — ``get_state`` hands out a copy
"""

from __future__ import annotations

import threading
from unittest.mock import patch

from structlog.testing import capture_logs

from baldur.services.event_bus.bus.event_types import EventType
from baldur.services.event_bus.bus.models import create_event
from baldur.services.system_control import (
    SystemState,
    is_baldur_enabled,
    is_dry_run,
)

ACTOR = "oncall-admin"
REASON = "payment incident"
_JOIN_SECONDS = 5.0


def _read_everything(manager) -> tuple:
    return (
        manager.is_enabled(),
        manager.is_dry_run(),
        manager.switches(),
        manager.get_state(refresh=False).to_dict(),
        manager.is_persist_dirty(),
    )


# =============================================================================
# Behavior — readers
# =============================================================================


class TestSystemControlSnapshotBehavior:
    """Readers take no lock, do no store I/O and never raise (D6)."""

    def test_readers_finish_while_every_manager_lock_is_held(self, control_env):
        """A flip or a pass holding its locks across I/O never delays a reader."""
        # Given: every lock the manager owns is held by this thread
        manager = control_env.manager
        control_env.load(enabled=False, dry_run=True)
        results: list[tuple] = []
        locks = (manager._flip_lock, manager._apply_lock, manager._pending_lock)
        for lock in locks:
            lock.acquire()

        # When: another thread reads
        try:
            reader = threading.Thread(
                target=lambda: results.append(_read_everything(manager))
            )
            reader.start()
            reader.join(_JOIN_SECONDS)
            finished = not reader.is_alive()
        finally:
            for lock in locks:
                lock.release()

        # Then
        assert finished is True
        assert results[0][:3] == (False, True, (False, True))

    def test_readers_perform_no_store_io(self, control_env):
        """A thousand reads after the copy is loaded never touch the store."""
        control_env.load(enabled=True)
        reads_before = control_env.store.strict_reads

        for _ in range(250):
            _read_everything(control_env.manager)

        assert control_env.store.strict_reads == reads_before

    def test_copy_before_any_read_is_enabled_and_live(self, control_env):
        """The default every process starts from."""
        assert control_env.manager.switches() == (True, False)
        assert control_env.manager.is_state_known() is False

    def test_is_state_known_only_after_a_successful_read(self, control_env):
        """A failed pass leaves the state unknown; a successful one knows it."""
        control_env.store.fail_reads = ConnectionError("store down")
        control_env.refresh()
        known_after_failure = control_env.manager.is_state_known()
        control_env.store.fail_reads = None

        control_env.refresh()

        assert known_after_failure is False
        assert control_env.manager.is_state_known() is True

    def test_reader_answers_when_the_refresher_hook_raises(self, control_env):
        """A broken liveness check never reaches the caller."""
        control_env.load(enabled=False)

        with patch.object(
            control_env.manager._refresher,
            "ensure_live",
            side_effect=RuntimeError("refresher broken"),
        ):
            assert control_env.manager.is_enabled() is False
            assert control_env.manager.switches() == (False, False)

    def test_module_quick_checks_fall_back_when_the_manager_cannot_be_built(self):
        """``is_baldur_enabled`` / ``is_dry_run`` never raise: enabled and live."""
        with patch(
            "baldur.services.system_control.get_system_control",
            side_effect=RuntimeError("early init"),
        ):
            assert is_baldur_enabled() is True
            assert is_dry_run() is False

    def test_status_read_reports_the_store_without_assigning_the_copy(
        self, control_env
    ):
        """``get_state(refresh=True)`` reads fresh; only a pass assigns the copy."""
        control_env.load(enabled=True)
        control_env.seed(version=2, enabled=False)

        reported = control_env.manager.get_state(refresh=True)

        assert reported.enabled is False
        assert control_env.manager.is_enabled() is True

    def test_status_read_falls_back_to_the_copy_when_the_store_fails(self, control_env):
        """A status request during an outage reports this process's copy."""
        control_env.load(enabled=False)
        control_env.store.fail_reads = ConnectionError("store down")

        assert control_env.manager.get_state(refresh=True).enabled is False

    def test_get_state_hands_out_a_copy(self, control_env):
        """Mutating what a caller got never changes what readers see."""
        control_env.load(enabled=True)

        state = control_env.manager.get_state(refresh=False)
        state.enabled = False

        assert control_env.manager.is_enabled() is True


# =============================================================================
# Behavior — the refresh pass
# =============================================================================


class TestSystemControlRefreshPassBehavior:
    """A pass assigns the store to the copy and tells local subscribers (D6, D12)."""

    def test_pass_assigns_a_peer_flip_and_logs_the_change_once(self, control_env):
        """The copy follows the store; one INFO names old and new."""
        control_env.load(enabled=True)
        control_env.seed(version=2, enabled=False)

        with (
            patch("baldur.services.system_control.set_sc_enabled") as gauge,
            capture_logs() as logs,
        ):
            control_env.refresh()
            control_env.refresh()

        assert control_env.manager.is_enabled() is False
        gauge.assert_called_once_with(False)
        changed = [
            log for log in logs if log["event"] == "system_control.state_changed"
        ]
        assert [(log["old_enabled"], log["new_enabled"]) for log in changed] == [
            (True, False)
        ]

    def test_pass_read_that_began_before_a_local_write_is_discarded(self, control_env):
        """The generation guard: an older read never lands after a newer flip."""
        # Given: a pass whose read returns the pre-flip value after a local flip
        control_env.load(dry_run=False)
        manager = control_env.manager
        control_env.store.before_next_read = lambda: manager.enable_dry_run(actor=ACTOR)

        # When
        control_env.refresh()

        # Then: the flip's outcome stands in the copy and the store
        assert manager.is_dry_run() is True
        assert control_env.stored()["dry_run"] is True

    def test_pass_that_observes_a_peer_disable_announces_it_locally_once(
        self, control_env
    ):
        """Unheard transition → one local KILL_SWITCH_ACTIVATED from system_control."""
        # Given
        control_env.load(enabled=True)
        control_env.events.clear()
        control_env.seed(version=2, enabled=False)

        # When
        with patch.object(
            control_env.bus, "publish_local", wraps=control_env.bus.publish_local
        ) as publish_local:
            control_env.refresh()
            control_env.refresh()

        # Then
        assert control_env.kill_switch_events() == [
            (EventType.KILL_SWITCH_ACTIVATED, "system_control")
        ]
        [announced] = control_env.events
        assert announced.data == {
            "reason": "observed_state_change",
            "activated_by": "system_control",
        }
        assert publish_local.call_count == 1

    def test_first_load_of_a_disabled_store_announces_the_brake(self, control_env):
        """Nothing heard reads as enabled — a fresh process pins its throttle too."""
        control_env.seed(version=1, enabled=False)

        control_env.refresh()

        assert control_env.kill_switch_events() == [
            (EventType.KILL_SWITCH_ACTIVATED, "system_control")
        ]

    def test_pass_after_a_local_commit_does_not_announce_it_again(self, control_env):
        """This process heard its own flip's event: the next pass stays quiet."""
        control_env.load(enabled=True)
        control_env.events.clear()

        control_env.manager.disable(actor=ACTOR, reason=REASON)
        control_env.refresh()

        assert control_env.kill_switch_events() == [
            (EventType.KILL_SWITCH_ACTIVATED, "system_control")
        ]

    def test_throttle_full_stop_is_not_heard_and_not_undone(self, control_env):
        """A Full Stop (source ``throttle``, no flip) never leads to a DEACTIVATED."""
        # Given: the manager is subscribed, the store is enabled
        control_env.load(enabled=True)
        control_env.bus.publish(
            create_event(
                EventType.KILL_SWITCH_ACTIVATED,
                {"reason": "full_stop"},
                "throttle",
            )
        )
        control_env.events.clear()

        # When
        control_env.refresh()

        # Then
        assert (EventType.KILL_SWITCH_DEACTIVATED, "system_control") not in (
            control_env.kill_switch_events()
        )

    def test_system_control_event_is_heard_and_a_differing_store_announced(
        self, control_env
    ):
        """Positive twin: a heard system-control disable, then an enabled store."""
        control_env.load(enabled=True)
        control_env.bus.publish(
            create_event(
                EventType.KILL_SWITCH_ACTIVATED,
                {"reason": REASON},
                "system_control",
            )
        )
        control_env.events.clear()

        control_env.refresh()

        assert control_env.kill_switch_events() == [
            (EventType.KILL_SWITCH_DEACTIVATED, "system_control")
        ]

    def test_failed_announcement_is_repeated_at_the_next_pass(self, control_env):
        """Recorded as heard only after the publish returned: a duplicate, never a miss."""
        # Given: the first local publish fails
        control_env.load(enabled=True)
        control_env.events.clear()
        control_env.seed(version=2, enabled=False)
        real_publish_local = control_env.bus.publish_local
        attempts: list[int] = []

        def failing_once(event):
            attempts.append(1)
            if len(attempts) == 1:
                raise RuntimeError("handler dispatch failed")
            return real_publish_local(event)

        # When
        with (
            patch.object(control_env.bus, "publish_local", failing_once),
            capture_logs() as logs,
        ):
            control_env.refresh()
            control_env.refresh()
            control_env.refresh()

        # Then
        assert len(attempts) == 2
        assert control_env.kill_switch_events() == [
            (EventType.KILL_SWITCH_ACTIVATED, "system_control")
        ]
        assert "system_control.announcement_failed" in [log["event"] for log in logs]

    def test_pass_that_cannot_read_keeps_the_copy(self, control_env):
        """A failed read never turns the brake off (last known state)."""
        control_env.load(enabled=False, dry_run=True)
        control_env.store.fail_reads = ConnectionError("store down")

        control_env.refresh()

        assert control_env.manager.switches() == (False, True)

    def test_dry_run_change_observed_by_a_pass_announces_no_kill_switch_event(
        self, control_env
    ):
        """Only the enabled field drives the kill-switch announcement."""
        control_env.load(enabled=True)
        control_env.events.clear()
        control_env.seed(version=2, **SystemState(enabled=True, dry_run=True).to_dict())

        control_env.refresh()

        assert control_env.manager.is_dry_run() is True
        assert control_env.kill_switch_events() == []
