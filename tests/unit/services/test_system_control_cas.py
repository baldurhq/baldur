"""System Control flips: versioned writes, and what a change the store did not confirm does.

Target: ``baldur.services.system_control.SystemControlManager`` — every flip is
a versioned write re-evaluated on the stored state (never on this process's
copy); its ``SystemControlChange`` says whether the store holds it and where it
applies; a change toward "Baldur acts less" (``disable``, ``enable_dry_run``)
the store did not confirm is held in this process and retried by every refresh
pass under one comparison of its field group, while a change the other way
raises ``SystemControlStoreError`` and changes nothing locally; an unknown
outcome is decided by the next successful read of the writer token; a later
flip withdraws a held change of its group and says whether it may still land.

Verification techniques applied (§8):
  - §8.1 Contract — the REST-visible outcome vocabulary and the store key
  - §8.12 Branch outcome — flip direction × store answer (committed / not
    applied / unknown); the held-change comparison (carries the held values /
    equals its base / changed); ``may_still_land`` for each withdrawal outcome
  - §8.8 State transition — held → committed / dropped / withdrawn; unknown →
    committed / not applied at the next read
  - §8.3 Idempotency — a landed change is never written a second time
  - §8.4 Side effects — audit and events from the replaced stored state; one
    WARNING per held change, DEBUG per failed retry, INFO on commit
  - §8.13 Proximate cause — on a WATCH-honoring Redis double, a withdrawn
    disable's stalled EXEC is aborted by the committed enable's write, never
    landing after it
"""

from __future__ import annotations

import dataclasses
import os
from unittest.mock import patch

import pytest
from structlog.testing import capture_logs

from baldur.core.exceptions import BaldurError, SystemControlStoreError
from baldur.core.state_backend import (
    OCC_VERSION_FIELD,
    OCC_WRITER_FIELD,
    MemoryStateBackend,
    RedisStateBackend,
    configure_state_backend,
    stored_version,
)
from baldur.services.event_bus.bus.event_types import EventType
from baldur.services.system_control import (
    APPLIES_EVERYWHERE,
    APPLIES_NONE,
    APPLIES_THIS_PROCESS,
    STATE_KEY,
    SYSTEM_CONTROL_REFRESH_INTERVAL_SECONDS,
    SystemControlChange,
    SystemState,
)
from tests.factories.state_backend_doubles import (
    CAS_LAND_THEN_RAISE,
    CAS_PEER,
    CAS_RAISE,
)

ACTOR = "oncall-admin"
REASON = "payment incident"
_AUDIT = "baldur.services.system_control.log_system_control_audit"

# (flip, the state the process starts in, the field the flip moves, its target)
_ACTS_LESS = [
    ("disable", {"enabled": True}, "enabled", False),
    ("enable_dry_run", {"dry_run": False}, "dry_run", True),
]
_ACTS_MORE = [
    ("enable", {"enabled": False}, "enabled", True),
    ("disable_dry_run", {"dry_run": True}, "dry_run", False),
]


def _flip(manager, name: str) -> SystemControlChange:
    method = getattr(manager, name)
    if name in ("enable", "disable"):
        return method(actor=ACTOR, reason=REASON)
    return method(actor=ACTOR)


def _make_not_applied(store) -> None:
    """Every write raises before landing; the read-back sees the old version."""
    store.fail_writes = ConnectionError("write refused")


def _make_unknown(store) -> None:
    """The write raises and the read-back cannot answer."""
    store.cas_script = [CAS_RAISE]
    store.read_errors = [None, TimeoutError("read-back timed out")]


def _make_landed_unknown(store) -> None:
    """The write lands, its reply is lost, and the read-back cannot answer."""
    store.cas_script = [CAS_LAND_THEN_RAISE]
    store.read_errors = [None, TimeoutError("read-back timed out")]


# =============================================================================
# Contract — the outcome vocabulary the REST answers carry
# =============================================================================


class TestSystemControlChangeContract:
    """Names and values operators and the status API read (D5, D9)."""

    def test_store_key_and_refresh_interval(self):
        """The stored key a runbook edits, and the 5 s reach interval."""
        assert STATE_KEY == "system_control"
        assert SYSTEM_CONTROL_REFRESH_INTERVAL_SECONDS == 5.0

    def test_applies_vocabulary(self):
        """Where a change is in force: everywhere, this process, or nowhere."""
        assert (APPLIES_EVERYWHERE, APPLIES_THIS_PROCESS, APPLIES_NONE) == (
            "everywhere",
            "this_process",
            "none",
        )

    def test_response_fields_are_the_four_outcome_fields(self):
        """Every flip response carries exactly these outcome fields."""
        change = SystemControlChange(
            SystemState(),
            persisted=None,
            applies=APPLIES_THIS_PROCESS,
            withdrew_held_change=True,
            may_still_land=True,
        )

        assert change.response_fields() == {
            "persisted": None,
            "applies": "this_process",
            "withdrew_held_change": True,
            "may_still_land": True,
        }

    def test_store_error_is_a_baldur_error_with_the_503_code(self):
        """The OSS handlers map this code to 503 across the PRO boundary."""
        error = SystemControlStoreError(change="enable", persisted=None)

        assert isinstance(error, BaldurError)
        assert error.code == "control_state_store_unavailable"
        assert error.applies == "none"
        assert "has an unknown outcome" in str(error)
        assert "was not applied" in str(SystemControlStoreError(change="enable"))


# =============================================================================
# Behavior — flips are versioned writes on the stored state
# =============================================================================


class TestSystemControlCasBehavior:
    """A flip applies its intent to the stored state and reports the outcome."""

    @pytest.mark.parametrize(
        ("flip", "initial", "field", "target"),
        _ACTS_LESS + _ACTS_MORE,
        ids=[case[0] for case in _ACTS_LESS + _ACTS_MORE],
    )
    def test_committed_flip_applies_everywhere(
        self, control_env, flip, initial, field, target
    ):
        """Committed: in the store, in this copy, ``persisted`` and ``everywhere``."""
        control_env.load(**initial)

        change = _flip(control_env.manager, flip)

        assert change.persisted is True
        assert change.applies == APPLIES_EVERYWHERE
        assert getattr(change.state, field) is target
        assert control_env.stored()[field] is target
        assert control_env.manager.is_persist_dirty() is False

    @pytest.mark.parametrize(
        ("flip", "initial", "field", "target"), _ACTS_LESS, ids=["disable", "dry_run"]
    )
    @pytest.mark.parametrize(
        ("make_outcome", "persisted"),
        [(_make_not_applied, False), (_make_unknown, None)],
        ids=["not_applied", "unknown"],
    )
    def test_unconfirmed_acts_less_flip_is_held_in_this_process(
        self, control_env, flip, initial, field, target, make_outcome, persisted
    ):
        """The brake holds here, and the response says only here."""
        # Given
        control_env.load(**initial)
        stored_before = control_env.stored()
        make_outcome(control_env.store)

        # When
        change = _flip(control_env.manager, flip)

        # Then
        assert change.persisted is persisted
        assert change.applies == APPLIES_THIS_PROCESS
        assert getattr(control_env.manager.get_state(refresh=False), field) is target
        assert control_env.manager.is_persist_dirty() is True
        assert control_env.stored() == stored_before

    @pytest.mark.parametrize(
        ("flip", "initial", "field", "target"), _ACTS_MORE, ids=["enable", "go_live"]
    )
    @pytest.mark.parametrize(
        ("make_outcome", "persisted"),
        [(_make_not_applied, False), (_make_unknown, None)],
        ids=["not_applied", "unknown"],
    )
    def test_unconfirmed_acts_more_flip_raises_and_changes_nothing(
        self, control_env, flip, initial, field, target, make_outcome, persisted
    ):
        """A release the store did not confirm is in force nowhere."""
        # Given
        control_env.load(**initial)
        make_outcome(control_env.store)

        # When
        with pytest.raises(SystemControlStoreError) as raised:
            _flip(control_env.manager, flip)

        # Then
        assert raised.value.persisted is persisted
        assert raised.value.applies == APPLIES_NONE
        assert getattr(control_env.manager.get_state(refresh=False), field) is (
            not target
        )
        assert control_env.manager.is_persist_dirty() is False

    def test_flip_applies_its_fields_over_the_stored_state_not_the_copy(
        self, control_env
    ):
        """A peer's dry-run the copy has not seen yet survives this process's disable."""
        # Given: the store has a peer's dry-run; this copy is still live
        control_env.load(enabled=True)
        control_env.seed(version=2, enabled=True, dry_run=True)

        # When
        change = control_env.manager.disable(actor=ACTOR, reason=REASON)

        # Then
        assert control_env.stored()["dry_run"] is True
        assert control_env.stored()["enabled"] is False
        assert change.state.dry_run is True

    def test_flip_that_loses_to_a_peer_reapplies_on_the_peer_state(self, control_env):
        """The mutate re-runs on what the peer committed, then commits once."""
        # Given: a peer toggles dry-run between this flip's read and its write
        control_env.load(enabled=True)
        control_env.store.peer_value = {
            **SystemState(enabled=True, dry_run=True).to_dict(),
            OCC_VERSION_FIELD: 2,
            OCC_WRITER_FIELD: "peer",
        }
        control_env.store.cas_script = [CAS_PEER]

        # When
        change = control_env.manager.disable(actor=ACTOR, reason=REASON)

        # Then
        stored = control_env.stored()
        assert change.persisted is True
        assert (stored["enabled"], stored["dry_run"]) == (False, True)
        assert stored_version(stored) == 3

    def test_each_committed_flip_carries_its_own_writer_token(self, control_env):
        """The token alone identifies a change in the store."""
        control_env.load(enabled=True)

        control_env.manager.disable(actor=ACTOR, reason=REASON)
        first = control_env.stored()[OCC_WRITER_FIELD]
        control_env.manager.enable(actor=ACTOR, reason=REASON)
        second = control_env.stored()[OCC_WRITER_FIELD]

        assert first != second
        assert first != "peer"

    def test_go_live_stamps_its_time_so_the_dry_run_group_changes(self, control_env):
        """``disable_dry_run`` writes ``dry_run_disabled_at``, not only ``dry_run``."""
        control_env.load(dry_run=True)

        control_env.manager.disable_dry_run(actor=ACTOR)

        assert control_env.stored()["dry_run"] is False
        assert control_env.stored()["dry_run_disabled_at"] is not None

    def test_audit_records_the_stored_state_the_flip_replaced(self, control_env):
        """A stale copy never becomes the audit's old state."""
        # Given: this copy still says disabled; the store was re-enabled by a peer
        control_env.load(enabled=False)
        control_env.seed(version=2, enabled=True)

        # When
        with patch(_AUDIT) as audit:
            control_env.manager.disable(actor=ACTOR, reason=REASON)

        # Then
        audit.assert_called_once()
        assert audit.call_args.kwargs["old_state"]["enabled"] is True
        assert audit.call_args.kwargs["new_state"]["enabled"] is False

    def test_flip_onto_a_state_already_stored_audits_nothing_but_tells_local_subscribers(
        self, control_env
    ):
        """No stored transition → no audit; this process's copy did change → one event."""
        # Given: a peer already disabled; this copy is still enabled
        control_env.load(enabled=True)
        control_env.seed(version=2, enabled=False)
        control_env.events.clear()

        # When
        with patch(_AUDIT) as audit:
            control_env.manager.disable(actor=ACTOR, reason=REASON)

        # Then
        audit.assert_not_called()
        assert control_env.kill_switch_events() == [
            (EventType.KILL_SWITCH_ACTIVATED, "system_control")
        ]

    def test_unparseable_stored_value_is_never_overwritten_by_a_flip(self, control_env):
        """A value this release cannot read is not applied over, not blindly replaced."""
        MemoryStateBackend.set(control_env.store, STATE_KEY, ["not", "a", "state"])

        with pytest.raises(SystemControlStoreError) as raised:
            control_env.manager.enable(actor=ACTOR, reason=REASON)

        assert raised.value.persisted is False
        assert control_env.stored() == ["not", "a", "state"]

    def test_unknown_release_that_landed_commits_its_side_effects_at_the_next_read(
        self, control_env
    ):
        """An enable whose reply was lost is decided by its token, then announced."""
        # Given: an enable that landed but ended unknown
        control_env.load(enabled=False)
        _make_landed_unknown(control_env.store)
        with pytest.raises(SystemControlStoreError):
            control_env.manager.enable(actor=ACTOR, reason=REASON)
        still_disabled = control_env.manager.is_enabled()
        control_env.events.clear()

        # When: the next pass reads the store
        with patch(_AUDIT) as audit:
            control_env.refresh()
            control_env.refresh()

        # Then: committed once, with its audit and event, and the copy follows
        assert still_disabled is False
        assert control_env.manager.is_enabled() is True
        assert [c.kwargs["action"] for c in audit.call_args_list] == ["enable"]
        # The audit's new state is what the store holds, not the copy the
        # deciding pass had not yet replaced.
        assert audit.call_args.kwargs["old_state"]["enabled"] is False
        assert audit.call_args.kwargs["new_state"]["enabled"] is True
        assert control_env.kill_switch_events() == [
            (EventType.KILL_SWITCH_DEACTIVATED, "system_control")
        ]

    def test_unknown_release_that_did_not_land_is_dropped_at_the_next_read(
        self, control_env
    ):
        """No token in the store → not applied: no audit, no event, still disabled."""
        control_env.load(enabled=False)
        _make_unknown(control_env.store)
        with pytest.raises(SystemControlStoreError):
            control_env.manager.enable(actor=ACTOR, reason=REASON)
        control_env.events.clear()

        with patch(_AUDIT) as audit, capture_logs() as logs:
            control_env.refresh()

        audit.assert_not_called()
        assert control_env.kill_switch_events() == []
        assert control_env.manager.is_enabled() is False
        assert "system_control.unknown_change_not_applied" in [
            log["event"] for log in logs
        ]


# =============================================================================
# Behavior — held changes
# =============================================================================


def _hold_a_disable(control_env, make_outcome=_make_not_applied) -> None:
    """This process pulled the brake while the store refused the write."""
    control_env.load(enabled=True)
    make_outcome(control_env.store)
    control_env.manager.disable(actor=ACTOR, reason=REASON)
    control_env.store.fail_writes = None
    control_env.store.cas_script = []
    control_env.store.read_errors = []


class TestSystemControlHeldChangeBehavior:
    """One comparison of the stored field group, on every retry attempt (D9)."""

    def test_held_change_whose_values_are_stored_commits_without_writing(
        self, control_env
    ):
        """The stored group already carries the held values → committed, no write.

        The values carry the held change's own timestamps, so it landed: its
        audit runs once, from the state it was based on.
        """
        # Given: the group the held disable would write is in the store already
        _hold_a_disable(control_env)
        held = control_env.manager._held["enabled"]
        control_env.seed(
            version=5,
            token="hand-copy",
            enabled=False,
            **{name: value for name, value in held.values.items() if name != "enabled"},
        )
        writes_before = len(control_env.store.cas_calls)

        # When
        with patch(_AUDIT) as audit:
            control_env.refresh()

        # Then
        assert control_env.manager.is_persist_dirty() is False
        assert len(control_env.store.cas_calls) == writes_before
        assert control_env.stored()[OCC_WRITER_FIELD] == "hand-copy"
        assert [c.kwargs["action"] for c in audit.call_args_list] == ["disable"]
        assert audit.call_args.kwargs["old_state"]["enabled"] is True

    def test_held_change_over_an_unchanged_group_is_written(self, control_env):
        """The stored group still equals the held change's base → write it."""
        _hold_a_disable(control_env)
        token = control_env.manager._held["enabled"].token

        control_env.refresh()

        assert control_env.stored()["enabled"] is False
        assert control_env.stored()[OCC_WRITER_FIELD] == token
        assert control_env.manager.is_persist_dirty() is False

    def test_held_change_over_a_changed_group_is_dropped_and_the_store_applied(
        self, control_env
    ):
        """A peer's later enable wins: the held disable is dropped with a WARNING."""
        # Given
        _hold_a_disable(control_env)
        control_env.seed(
            version=2, enabled=True, enabled_at="2026-09-30T12:00:00+00:00"
        )

        # When
        with capture_logs() as logs:
            control_env.refresh()

        # Then
        assert control_env.manager.is_enabled() is True
        assert control_env.manager.is_persist_dirty() is False
        assert control_env.stored()["enabled"] is True
        dropped = [
            log for log in logs if log["event"] == "system_control.held_change_dropped"
        ]
        assert [log["log_level"] for log in dropped] == ["warning"]

    def test_unrelated_peer_write_never_costs_the_held_brake(self, control_env):
        """A peer's dry-run toggle leaves the enabled group at base → the disable lands."""
        _hold_a_disable(control_env)
        control_env.seed(version=2, enabled=True, dry_run=True)

        control_env.refresh()

        stored = control_env.stored()
        assert (stored["enabled"], stored["dry_run"]) == (False, True)
        assert control_env.manager.is_enabled() is False

    def test_previous_release_blind_enable_at_version_zero_is_not_undone(
        self, control_env
    ):
        """A version-0 write the holder never saw changed the group → dropped."""
        # Given: a previous-release process re-enables with a blind (unstamped) write
        _hold_a_disable(control_env)
        control_env.seed(enabled=True, enabled_at="2026-09-30T12:00:00+00:00")

        # When
        control_env.refresh()

        # Then
        stored = control_env.stored()
        assert stored["enabled"] is True
        assert stored_version(stored) == 0
        assert control_env.manager.is_enabled() is True

    def test_held_unknown_disable_that_landed_commits_without_a_second_write(
        self, control_env
    ):
        """The retry sees the held change's own token → committed, nothing rewritten."""
        _hold_a_disable(control_env, make_outcome=_make_landed_unknown)
        writes_before = len(control_env.store.cas_calls)

        control_env.refresh()

        assert control_env.manager.is_persist_dirty() is False
        assert len(control_env.store.cas_calls) == writes_before
        assert control_env.stored()["enabled"] is False

    def test_held_disable_that_landed_audits_the_state_it_was_based_on(
        self, control_env
    ):
        """A retry that finds its own token audits the real prior state, not defaults."""
        # Given: the brake was released at a known time, then a disable landed
        # with its reply lost and was held
        enabled_at = "2026-09-01T00:00:00+00:00"
        control_env.load(enabled=True, enabled_at=enabled_at)
        _make_landed_unknown(control_env.store)
        control_env.manager.disable(actor=ACTOR, reason=REASON)

        # When
        with patch(_AUDIT) as audit:
            control_env.refresh()

        # Then
        assert [c.kwargs["action"] for c in audit.call_args_list] == ["disable"]
        old_state = audit.call_args.kwargs["old_state"]
        assert (old_state["enabled"], old_state["enabled_at"]) == (True, enabled_at)

    def test_held_disable_that_landed_before_an_unrelated_write_is_still_audited(
        self, control_env
    ):
        """A peer's dry-run write over the landed disable does not erase its audit."""
        # Given: a disable landed with its reply lost, then a peer toggled dry-run
        # over it (the stored token is the peer's, the enabled group is ours)
        control_env.load(enabled=True)
        _make_landed_unknown(control_env.store)
        control_env.manager.disable(actor=ACTOR, reason=REASON)
        landed = dict(control_env.stored())
        control_env.seed(
            version=landed[OCC_VERSION_FIELD] + 1,
            **{
                **SystemState.from_dict(landed).to_dict(),
                "dry_run": True,
                "dry_run_enabled_at": "2026-10-01T00:00:00+00:00",
            },
        )

        # When
        with patch(_AUDIT) as audit:
            control_env.refresh()

        # Then: committed once, with its audit and the fleet-wide event
        assert control_env.manager.is_persist_dirty() is False
        assert [c.kwargs["action"] for c in audit.call_args_list] == ["disable"]
        assert audit.call_args.kwargs["old_state"]["enabled"] is True
        assert (
            EventType.KILL_SWITCH_ACTIVATED,
            "system_control",
        ) in control_env.kill_switch_events()

    def test_go_live_elsewhere_after_a_held_dry_run_is_never_undone(self, control_env):
        """An operator's later go-live served by a peer wins over a held dry-run."""
        # Given: dry-run on is held here; a peer then goes live — the stored
        # dry_run value is unchanged, but the go-live stamps its time
        control_env.load(dry_run=False)
        _make_not_applied(control_env.store)
        control_env.manager.enable_dry_run(actor=ACTOR)
        control_env.store.fail_writes = None
        control_env.seed(
            version=2, dry_run=False, dry_run_disabled_at="2026-10-01T01:00:00+00:00"
        )

        # When
        control_env.refresh()

        # Then: the held dry-run is dropped, the fleet stays live
        assert control_env.stored()["dry_run"] is False
        assert control_env.manager.is_dry_run() is False
        assert control_env.manager.is_persist_dirty() is False

    def test_held_change_inherited_across_fork_is_dropped_never_retried(
        self, control_env
    ):
        """A fork child holds nothing it did not make: no retry, no brake, no dirty."""
        # Given: the parent's held disable, as a fork child inherits it
        _hold_a_disable(control_env)
        held = control_env.manager._held["enabled"]
        control_env.manager._held["enabled"] = dataclasses.replace(
            held, origin_pid=os.getpid() + 1
        )
        writes_before = len(control_env.store.cas_calls)

        # When
        dirty_before_pass = control_env.manager.is_persist_dirty()
        control_env.refresh()

        # Then: nothing written; the copy follows the store
        assert dirty_before_pass is False
        assert len(control_env.store.cas_calls) == writes_before
        assert control_env.stored()["enabled"] is True
        assert control_env.manager.is_enabled() is True
        assert control_env.manager._held == {}

    def test_held_change_is_announced_once_then_debug_per_retry_then_info(
        self, control_env
    ):
        """One WARNING when held; DEBUG per failed retry; INFO when a retry commits."""
        # Given
        control_env.load(enabled=True)
        _make_not_applied(control_env.store)

        # When
        with capture_logs() as logs:
            control_env.manager.disable(actor=ACTOR, reason=REASON)
            for _ in range(3):
                control_env.refresh()
            control_env.store.fail_writes = None
            control_env.refresh()

        # Then
        trail = [
            (log["event"], log["log_level"])
            for log in logs
            if log["event"].startswith("system_control.")
            and ("save_state" in log["event"] or "persist_retry" in log["event"])
        ]
        assert trail == [
            ("system_control.save_state_failed", "warning"),
            ("system_control.persist_retry_failed", "debug"),
            ("system_control.persist_retry_failed", "debug"),
            ("system_control.persist_retry_failed", "debug"),
            ("system_control.persist_retry_succeeded", "info"),
        ]

    def test_persist_dirty_clears_when_a_held_change_is_dropped(self, control_env):
        """``persist_dirty`` is true exactly while a change is held."""
        _hold_a_disable(control_env)
        dirty_while_held = control_env.manager.is_persist_dirty()
        control_env.seed(
            version=2, enabled=True, enabled_at="2026-09-30T12:00:00+00:00"
        )

        control_env.refresh()

        assert dirty_while_held is True
        assert control_env.manager.is_persist_dirty() is False

    def test_retry_skipped_while_a_flip_holds_the_flip_lock(self, control_env):
        """A flip in progress keeps the held group as it is for that pass."""
        # Given: a held disable, and a peer's later enable in the store
        _hold_a_disable(control_env)
        control_env.seed(
            version=2, enabled=True, enabled_at="2026-09-30T12:00:00+00:00"
        )
        writes_before = len(control_env.store.cas_calls)

        # When: a pass runs while a flip holds the lock, then one after
        control_env.manager._flip_lock.acquire()
        try:
            control_env.refresh()
            during = (
                control_env.manager.is_enabled(),
                control_env.manager.is_persist_dirty(),
            )
        finally:
            control_env.manager._flip_lock.release()
        control_env.refresh()

        # Then: skipped (brake kept, nothing written), then dropped on the next pass
        assert during == (False, True)
        assert len(control_env.store.cas_calls) == writes_before
        assert control_env.manager.is_enabled() is True

    def test_later_flip_withdraws_the_held_change_which_is_never_retried(
        self, control_env
    ):
        """An enable in the holding process ends the held disable for good."""
        # Given
        _hold_a_disable(control_env)

        # When
        change = control_env.manager.enable(actor=ACTOR, reason="resolved")
        control_env.refresh()
        control_env.refresh()

        # Then
        assert (change.withdrew_held_change, change.may_still_land) == (True, False)
        assert control_env.stored()["enabled"] is True
        assert control_env.manager.is_persist_dirty() is False

    def test_flip_of_the_other_switch_leaves_a_held_change_in_place(self, control_env):
        """Withdrawal is per field group: a disable does not withdraw a held dry-run."""
        control_env.load(dry_run=False)
        _make_not_applied(control_env.store)
        control_env.manager.enable_dry_run(actor=ACTOR)
        control_env.store.fail_writes = None

        change = control_env.manager.disable(actor=ACTOR, reason=REASON)

        assert change.withdrew_held_change is False
        assert control_env.manager.is_persist_dirty() is True
        assert control_env.manager.is_dry_run() is True

    @pytest.mark.parametrize(
        ("held_outcome", "enable_commits", "expected_may_still_land"),
        [
            (_make_unknown, False, True),
            (_make_unknown, True, False),
            (_make_not_applied, False, False),
        ],
        ids=[
            "unknown_held_enable_fails",
            "unknown_held_enable_commits",
            "not_applied_held_enable_fails",
        ],
    )
    def test_may_still_land_only_for_an_unknown_write_and_an_uncommitted_withdrawal(
        self, control_env, held_outcome, enable_commits, expected_may_still_land
    ):
        """A sent write cannot be recalled — unless a committed flip superseded it."""
        # Given: a held disable whose attempt ended as ``held_outcome``
        _hold_a_disable(control_env, make_outcome=held_outcome)
        if not enable_commits:
            _make_not_applied(control_env.store)

        # When
        if enable_commits:
            change = control_env.manager.enable(actor=ACTOR, reason="resolved")
            outcome = (change.withdrew_held_change, change.may_still_land)
        else:
            with pytest.raises(SystemControlStoreError) as raised:
                control_env.manager.enable(actor=ACTOR, reason="resolved")
            outcome = (raised.value.withdrew_held_change, raised.value.may_still_land)

        # Then
        assert outcome == (True, expected_may_still_land)


# =============================================================================
# Behavior — a withdrawn disable's stalled EXEC (Redis store, external review E1)
# =============================================================================


class _StallingPipeline:
    """A WATCH / MULTI / EXEC round whose EXEC the server may apply late."""

    def __init__(self, redis: _StallingRedis) -> None:
        self._redis = redis
        self._watched: dict[str, str | None] = {}
        self._queued: list[tuple[str, str]] = []

    def __enter__(self) -> _StallingPipeline:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def watch(self, key: str) -> None:
        self._watched[key] = self._redis.store.get(key)

    def unwatch(self) -> None:
        self._watched.clear()

    def get(self, key: str) -> str | None:
        return self._redis.get(key)

    def multi(self) -> None:
        return None

    def set(self, key: str, value: str) -> None:
        self._queued.append((key, value))

    def _apply(self) -> bool:
        # Like Redis: a key changed since WATCH aborts the transaction.
        if any(self._redis.store.get(k) != v for k, v in self._watched.items()):
            return False
        for key, value in self._queued:
            self._redis.store[key] = value
        return True

    def execute(self) -> list[bool]:
        if self._redis.stall_next_exec:
            self._redis.stall_next_exec = False
            self._redis.stalled.append(self._apply)
            self._redis.unreachable = True
            raise TimeoutError("EXEC sent; its reply was lost")
        if not self._apply():
            from redis import WatchError

            raise WatchError("watched key changed")
        return [True]


class _StallingRedis:
    """Decoded-response Redis double whose next EXEC can stall server-side.

    A stalled EXEC loses its reply (the client raises) while the server still
    holds it; ``release()`` lets the server run it, honoring WATCH then. The
    store is unreachable from the moment the reply is lost until ``reconnect()``.
    """

    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.stall_next_exec = False
        self.unreachable = False
        self.stalled: list = []

    def get(self, key: str) -> str | None:
        if self.unreachable:
            raise ConnectionError("redis unreachable")
        return self.store.get(key)

    def set(self, key: str, value: str) -> None:
        self.store[key] = value

    def pipeline(self) -> _StallingPipeline:
        return _StallingPipeline(self)

    def reconnect(self) -> None:
        self.unreachable = False

    def release(self) -> list[bool]:
        applied = [apply() for apply in self.stalled]
        self.stalled.clear()
        return applied


def _redis_store_over(client: _StallingRedis) -> RedisStateBackend:
    backend = RedisStateBackend.__new__(RedisStateBackend)
    backend._key_prefix = "baldur:state:"
    backend._client = client
    return backend


class TestSystemControlWithdrawnStalledExecBehavior:
    """A sent write cannot be recalled — but it cannot land after a committed flip."""

    @pytest.mark.parametrize(
        "release_before_enable",
        [False, True],
        ids=["released_after", "released_before"],
    )
    def test_withdrawn_disable_whose_exec_stalled_never_outlives_a_committed_enable(
        self, control_env, release_before_enable
    ):
        """Either order ends enabled; the committed enable says it may not still land."""
        # Given: a disable whose EXEC stalled with its reply lost → held, unknown
        client = _StallingRedis()
        backend = _redis_store_over(client)
        configure_state_backend(backend)
        backend.set(
            STATE_KEY,
            {
                **SystemState(enabled=True).to_dict(),
                OCC_VERSION_FIELD: 1,
                OCC_WRITER_FIELD: "peer",
            },
        )
        control_env.refresh()
        client.stall_next_exec = True
        held = control_env.manager.disable(actor=ACTOR, reason=REASON)
        client.reconnect()

        # When: the stalled EXEC reaches the server before or after the enable
        if release_before_enable:
            client.release()
        enabled = control_env.manager.enable(actor=ACTOR, reason="resolved")
        late = client.release()

        # Then
        assert (held.persisted, held.applies) == (None, "this_process")
        assert (enabled.persisted, enabled.withdrew_held_change) == (True, True)
        assert enabled.may_still_land is False
        assert late == ([] if release_before_enable else [False])
        assert backend.get_strict(STATE_KEY)["enabled"] is True
