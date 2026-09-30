"""SystemControlManager kill-switch event emission and held-change guard.

Target: ``baldur.services.system_control.SystemControlManager`` — the flip
path that publishes ``KILL_SWITCH_ACTIVATED`` / ``KILL_SWITCH_DEACTIVATED``
and the held-change guard: a kill switch the store did not confirm is held in
this process, retried on each refresh pass, and never lets a stale write
overwrite newer state.

Verification techniques applied (§8):
  - §8.8 State transition — emission fires only on an observed transition
  - §8.3 Idempotency — a repeated flip publishes nothing
  - §8.4 Side effects — payload / source / priority reaching subscribers
  - §8.2 Exception/edge cases — a store write failure holds the change in this
    process and a refresh pass never resurrects the stale store value
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import Any
from unittest.mock import patch

import pytest

from baldur.core.control_state import get_control_state_refresher
from baldur.core.exceptions import SystemControlStoreError
from baldur.core.state_backend import (
    MemoryStateBackend,
    configure_state_backend,
    reset_state_backend,
)
from baldur.services.event_bus import BaldurEvent, EventType
from baldur.services.event_bus.bus.event_types import EventPriority
from baldur.services.system_control import (
    STATE_KEY,
    SystemControlManager,
    SystemState,
    reset_system_control,
)

# The flip actor/reason are test data, not a production contract — they only
# have to survive the emission path unchanged.
ACTOR = "oncall-admin"
REASON = "payment incident"


class _FlakyBackend(MemoryStateBackend):
    """Memory backend whose writes can be forced to fail.

    Models the shipped backends' real failure shape: a write raises (Redis
    OOM, read-only file mount) while reads keep serving the last value that
    actually landed.
    """

    def __init__(self) -> None:
        super().__init__()
        self.fail_writes = False

    def set(
        self, key: str, value: dict[str, Any], *, ttl_seconds: int | None = None
    ) -> None:
        if self.fail_writes:
            raise RuntimeError("backend write failed")
        super().set(key, value, ttl_seconds=ttl_seconds)

    def compare_and_set(self, key, expected_version, new_value, **kwargs) -> bool:
        if self.fail_writes:
            raise RuntimeError("backend write failed")
        return super().compare_and_set(key, expected_version, new_value, **kwargs)


@dataclass
class _FlipEnv:
    """A manager on a private backend plus every kill-switch event it emitted."""

    manager: SystemControlManager
    backend: _FlakyBackend
    events: list[BaldurEvent]

    def seed_backend(self, *, enabled: bool) -> None:
        """Write the shared backend directly — a flip made by another process."""
        self.backend.set(STATE_KEY, SystemState(enabled=enabled).to_dict())

    def set_local_mirror(self, *, enabled: bool) -> None:
        """Force this process's copy, leaving the backend untouched."""
        self.manager._snapshot = dataclasses.replace(
            self.manager._snapshot, enabled=enabled
        )

    def refresh(self) -> None:
        """Run one refresh pass, as this process's refresher would."""
        get_control_state_refresher().refresh_now()

    def event_types(self) -> list[EventType]:
        return [e.event_type for e in self.events]


@pytest.fixture
def flip_env():
    """Fresh manager on its own memory backend, with a recording subscriber."""
    from baldur.services.event_bus import get_event_bus

    reset_system_control()
    SystemControlManager._instance = None

    backend = _FlakyBackend()
    configure_state_backend(backend)
    manager = SystemControlManager()

    bus = get_event_bus()
    bus.reset()
    events: list[BaldurEvent] = []

    def record_kill_switch_event(event: BaldurEvent) -> None:
        events.append(event)

    bus.subscribe(EventType.KILL_SWITCH_ACTIVATED, record_kill_switch_event)
    bus.subscribe(EventType.KILL_SWITCH_DEACTIVATED, record_kill_switch_event)

    yield _FlipEnv(manager=manager, backend=backend, events=events)

    backend.fail_writes = False
    bus.reset()
    SystemControlManager._instance = None
    reset_system_control()
    reset_state_backend()


# =============================================================================
# Contract — payload, source and priority of the published events
# =============================================================================


class TestSystemControlKillSwitchEventContract:
    """Committed shape of the two kill-switch events (D6, D7)."""

    def test_disable_publishes_activated_with_reason_and_actor_payload(self, flip_env):
        """Payload is exactly {'reason', 'activated_by'} with the flip's values."""
        flip_env.manager.disable(actor=ACTOR, reason=REASON)

        (event,) = flip_env.events
        assert event.event_type == EventType.KILL_SWITCH_ACTIVATED
        assert event.data == {"reason": REASON, "activated_by": ACTOR}

    def test_enable_publishes_deactivated_with_reason_and_actor_payload(self, flip_env):
        """The re-enable event carries the same two payload keys."""
        flip_env.set_local_mirror(enabled=False)
        flip_env.seed_backend(enabled=False)

        flip_env.manager.enable(actor=ACTOR, reason=REASON)

        (event,) = flip_env.events
        assert event.event_type == EventType.KILL_SWITCH_DEACTIVATED
        assert event.data == {"reason": REASON, "activated_by": ACTOR}

    def test_kill_switch_events_carry_system_control_source(self, flip_env):
        """source='system_control' — the throttle handler's self-source filter
        drops 'throttle' and must not drop these."""
        flip_env.manager.disable(actor=ACTOR, reason=REASON)
        flip_env.manager.enable(actor=ACTOR, reason=REASON)

        assert [e.source for e in flip_env.events] == [
            "system_control",
            "system_control",
        ]

    def test_activated_is_critical_and_deactivated_is_high_priority(self, flip_env):
        """ACTIVATED = CRITICAL (force-propagated), DEACTIVATED = HIGH."""
        flip_env.manager.disable(actor=ACTOR, reason=REASON)
        flip_env.manager.enable(actor=ACTOR, reason=REASON)

        activated, deactivated = flip_env.events
        assert activated.priority == EventPriority.CRITICAL
        assert deactivated.priority == EventPriority.HIGH


# =============================================================================
# Behavior — which observed transitions emit, and which stay silent
# =============================================================================


class TestSystemControlKillSwitchEmissionBehavior:
    """Emission fires on any transition THIS process observes (D3)."""

    @pytest.mark.parametrize(
        ("mirror_enabled", "backend_enabled", "expected"),
        [
            (True, True, [EventType.KILL_SWITCH_ACTIVATED]),
            (True, False, [EventType.KILL_SWITCH_ACTIVATED]),
            (False, True, [EventType.KILL_SWITCH_ACTIVATED]),
            (False, False, []),
        ],
        ids=[
            "local_flip",
            "backend_already_disabled_remotely",
            "local_mirror_wrongly_disabled",
            "idempotent_repeat",
        ],
    )
    def test_disable_emits_when_either_view_differs_from_the_committed_value(
        self, flip_env, mirror_enabled, backend_enabled, expected
    ):
        """disable() emits unless BOTH this process's copy and the stored state
        it replaced were already disabled.

        The stored half covers a backend another pod already flipped; the copy
        half covers a local copy that was wrongly disabled.
        """
        # Given
        flip_env.seed_backend(enabled=backend_enabled)
        flip_env.set_local_mirror(enabled=mirror_enabled)

        # When
        flip_env.manager.disable(actor=ACTOR, reason=REASON)

        # Then
        assert flip_env.event_types() == expected

    @pytest.mark.parametrize(
        ("mirror_enabled", "backend_enabled", "expected"),
        [
            (False, False, [EventType.KILL_SWITCH_DEACTIVATED]),
            (False, True, [EventType.KILL_SWITCH_DEACTIVATED]),
            (True, False, [EventType.KILL_SWITCH_DEACTIVATED]),
            (True, True, []),
        ],
        ids=[
            "local_flip",
            "backend_already_enabled_remotely",
            "local_mirror_wrongly_enabled",
            "idempotent_repeat",
        ],
    )
    def test_enable_emits_when_either_view_differs_from_the_committed_value(
        self, flip_env, mirror_enabled, backend_enabled, expected
    ):
        """enable() is the symmetric twin of the disable() gate.

        Without the stored half, an operator's enable() over a store another
        process disabled would notify nobody while the store transitioned.
        """
        # Given
        flip_env.seed_backend(enabled=backend_enabled)
        flip_env.set_local_mirror(enabled=mirror_enabled)

        # When
        flip_env.manager.enable(actor=ACTOR, reason=REASON)

        # Then
        assert flip_env.event_types() == expected

    def test_disable_on_enabled_system_publishes_exactly_one_event(self, flip_env):
        """One flip is one event — not one per gate half."""
        flip_env.manager.disable(actor=ACTOR, reason=REASON)

        assert len(flip_env.events) == 1

    def test_reset_publishes_nothing(self, flip_env):
        """reset() is test cleanup: recovery events from cleanup are noise."""
        flip_env.manager.disable(actor=ACTOR, reason=REASON)
        flip_env.events.clear()

        flip_env.manager.reset()

        assert flip_env.events == []

    @pytest.mark.parametrize(
        "method_name",
        ["enable_dry_run", "disable_dry_run"],
        ids=["enable_dry_run", "disable_dry_run"],
    )
    def test_dry_run_transitions_publish_nothing(self, flip_env, method_name):
        """dry_run is orthogonal to enabled — no kill-switch subscriber cares."""
        getattr(flip_env.manager, method_name)(actor=ACTOR)

        assert flip_env.events == []

    def test_flip_still_commits_when_the_event_bus_is_unavailable(self, flip_env):
        """Emission is fail-safe: a broken bus never aborts the kill switch."""
        with patch.object(SystemControlManager, "_get_event_bus", return_value=None):
            change = flip_env.manager.disable(actor=ACTOR, reason=REASON)

        assert change.state.enabled is False
        assert change.persisted is True
        assert flip_env.events == []


# =============================================================================
# Behavior — held changes (a kill switch the store did not confirm)
# =============================================================================


class TestSystemControlPersistDirtyBehavior:
    """A disable the store did not confirm is held, retried, and never overwrites."""

    def test_failed_state_write_holds_the_disable_in_this_process(self, flip_env):
        """The response says where the change applies, and the flag records it."""
        flip_env.seed_backend(enabled=True)
        flip_env.backend.fail_writes = True

        change = flip_env.manager.disable(actor=ACTOR, reason=REASON)

        assert change.persisted is False
        assert change.applies == "this_process"
        assert flip_env.manager.is_persist_dirty() is True

    def test_successful_write_leaves_the_manager_clean(self, flip_env):
        """Negative twin — the flag is not simply always set after a flip."""
        change = flip_env.manager.disable(actor=ACTOR, reason=REASON)

        assert change.persisted is True
        assert flip_env.manager.is_persist_dirty() is False

    def test_refresh_while_held_does_not_resurrect_the_stale_backend_value(
        self, flip_env
    ):
        """The kill switch stays pulled locally after a failed write.

        Without the guard a refresh pass reads the unwritten store and assigns
        'enabled' back — turning a pulled brake into a released one.
        """
        # Given: the backend still holds the pre-flip 'enabled' value
        flip_env.seed_backend(enabled=True)
        flip_env.backend.fail_writes = True
        flip_env.manager.disable(actor=ACTOR, reason=REASON)

        # When: two refresh passes run
        flip_env.refresh()
        flip_env.refresh()

        # Then
        assert flip_env.manager.is_enabled() is False
        assert flip_env.backend.get(STATE_KEY)["enabled"] is True

    def test_refresh_retries_the_held_write_and_clears_the_flag(self, flip_env):
        """Persistence self-heals on the next pass once the backend is back."""
        flip_env.seed_backend(enabled=True)
        flip_env.backend.fail_writes = True
        flip_env.manager.disable(actor=ACTOR, reason=REASON)

        flip_env.backend.fail_writes = False
        flip_env.refresh()

        assert flip_env.manager.is_persist_dirty() is False
        assert flip_env.manager.is_enabled() is False
        assert flip_env.backend.get(STATE_KEY)["enabled"] is False

    def test_persist_dirty_gauge_is_set_on_failure_and_cleared_on_retry(self, flip_env):
        """The flag is exposed as a gauge so an operator can ask whether this
        node is diverged right now, not only at flip time."""
        flip_env.seed_backend(enabled=True)
        flip_env.backend.fail_writes = True

        with patch("baldur.services.system_control.set_sc_persist_dirty") as mock_gauge:
            flip_env.manager.disable(actor=ACTOR, reason=REASON)
            assert mock_gauge.call_args_list[-1].args == (True,)

            flip_env.backend.fail_writes = False
            flip_env.refresh()
            assert mock_gauge.call_args_list[-1].args == (False,)

    def test_repeated_retry_failures_are_announced_only_once(self, flip_env):
        """A held change is retried on every pass; each failed retry logs at
        DEBUG only — the standing signals are the gauge and the status field.
        """
        flip_env.seed_backend(enabled=True)
        flip_env.backend.fail_writes = True

        flip_env.manager.disable(actor=ACTOR, reason=REASON)

        with patch("baldur.services.system_control.logger") as mock_logger:
            for _ in range(3):
                flip_env.refresh()

        assert mock_logger.exception.call_count == 0
        assert mock_logger.warning.call_count == 0

    def test_unconfirmed_enable_never_overwrites_a_newer_remote_disable(self, flip_env):
        """A re-enable the store did not confirm changes nothing locally.

        It raises, keeps this process disabled, and leaves nothing to retry:
        a node whose enable never landed cannot resurrect it over a kill
        switch another node committed meanwhile.
        """
        # Given: this node's enable() could not be persisted
        flip_env.seed_backend(enabled=False)
        flip_env.set_local_mirror(enabled=False)
        flip_env.backend.fail_writes = True
        with pytest.raises(SystemControlStoreError) as excinfo:
            flip_env.manager.enable(actor=ACTOR, reason="incident resolved")
        assert excinfo.value.persisted is False
        assert excinfo.value.applies == "none"
        assert flip_env.manager.is_enabled() is False

        # And: the backend recovers, and another node commits a kill switch
        flip_env.backend.fail_writes = False
        flip_env.backend.set(
            STATE_KEY,
            SystemState(
                enabled=False, disabled_by="other-pod", disabled_reason=REASON
            ).to_dict(),
        )

        # When: this node refreshes
        flip_env.refresh()

        # Then: the remote kill switch stands, here and in the backend
        assert flip_env.manager.is_enabled() is False
        assert flip_env.backend.get(STATE_KEY)["enabled"] is False
        assert flip_env.backend.get(STATE_KEY)["disabled_by"] == "other-pod"

    def test_held_flip_still_announces_and_blocks_locally(self, flip_env):
        """The in-process flip, its local announcement and local stepping aside
        all survive a store outage — only cross-process reach waits for the
        retry."""
        flip_env.seed_backend(enabled=True)
        flip_env.backend.fail_writes = True

        change = flip_env.manager.disable(actor=ACTOR, reason=REASON)

        assert change.state.enabled is False
        assert flip_env.manager.is_enabled() is False
        assert flip_env.event_types() == [EventType.KILL_SWITCH_ACTIVATED]
