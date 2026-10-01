"""
System Control Service

Global kill-switch and dry-run state for the Baldur system.

Features:
- One copy of the switch state per process, read without a lock or store I/O
- Pluggable backends (File, Redis, Memory); Redis is used by default when a
  Redis URL is named
- A per-process refresher keeps every process's copy within
  ``SYSTEM_CONTROL_REFRESH_INTERVAL_SECONDS`` of the store
- Versioned writes: a process holding an older copy never overwrites newer state
- A change the store did not confirm is reported as such, never as success

Configuration:
    # Django settings.py
    BALDUR_SYSTEM_CONTROL_BACKEND = "redis"  # or "file"
    BALDUR_REDIS_URL = "redis://localhost:6379/0"

    # Or environment variables
    BALDUR_SYSTEM_CONTROL_BACKEND=redis
    BALDUR_REDIS_URL=redis://localhost:6379/0
"""

from __future__ import annotations

import dataclasses
import os
from dataclasses import dataclass, field
from functools import partial
from typing import Any, Final

import structlog

from baldur.audit.helpers import log_system_control_audit
from baldur.core.control_state import get_control_state_refresher
from baldur.core.exceptions import SystemControlStoreError
from baldur.core.process_utils import fork_safe_lock
from baldur.core.serializable import SerializableMixin
from baldur.core.state_backend import (
    ALREADY_SATISFIED,
    Declined,
    MutateAnswer,
    PendingChange,
    StateBackend,
    VersionedWriteOutcome,
    VersionedWriteResult,
    get_state_backend,
    new_writer_token,
    settle_pending_changes,
    update_versioned,
)
from baldur.services.event_bus.bus.event_types import EventPriority, EventType
from baldur.services.event_bus.emitter import EventEmitterMixin
from baldur.utils.time import utc_now

try:
    from baldur.metrics.recorders.system_control import (
        record_sc_disabled,
        record_sc_disabled_duration,
        record_sc_state_change,
        set_sc_dry_run,
        set_sc_enabled,
        set_sc_persist_dirty,
    )
except ImportError:

    def set_sc_enabled(enabled: bool) -> None:
        return None

    def set_sc_dry_run(dry_run: bool) -> None:
        return None

    def set_sc_persist_dirty(dirty: bool) -> None:
        return None

    def record_sc_state_change(action: str) -> None:
        return None

    def record_sc_disabled_duration(duration: float) -> None:
        return None

    def record_sc_disabled() -> None:
        return None


logger = structlog.get_logger()


# =============================================================================
# Data Classes
# =============================================================================


@dataclass
class SystemState(SerializableMixin):
    """Baldur system state."""

    enabled: bool = True
    dry_run: bool = False  # Dry run mode: observe only, no actual actions
    disabled_at: str | None = None
    disabled_by: str | None = None
    disabled_reason: str | None = None
    enabled_at: str | None = None
    enabled_by: str | None = None
    dry_run_enabled_at: str | None = None
    dry_run_enabled_by: str | None = None
    dry_run_disabled_at: str | None = None


# State key for backend storage
STATE_KEY = "system_control"

#: How often every process re-reads the switch state from the store. A flip made
#: in one process reaches the others within this interval plus one read of each
#: other registered control-state key.
SYSTEM_CONTROL_REFRESH_INTERVAL_SECONDS: Final = 5.0

#: Where a change is in force.
APPLIES_EVERYWHERE: Final = "everywhere"
APPLIES_THIS_PROCESS: Final = "this_process"
APPLIES_NONE: Final = "none"

# The field group each switch owns. A change held in this process is compared
# on its group only, so an unrelated write (a peer's dry-run toggle) never
# costs the operator a pulled brake.
_ENABLED_GROUP: Final = ("enabled", "enabled_at", "disabled_at")
# Each group carries a timestamp that every change of that switch stamps, so a
# change of the switch always changes the group: a go-live that only rewrote
# ``dry_run: False`` would be invisible to a held dry-run-on based on it.
_DRY_RUN_GROUP: Final = ("dry_run", "dry_run_enabled_at", "dry_run_disabled_at")

# The outcome fields every change response carries.
_RESPONSE_FIELDS: Final = (
    "persisted",
    "applies",
    "withdrew_held_change",
    "may_still_land",
)


@dataclass(frozen=True)
class SystemControlChange:
    """The outcome of one kill-switch or dry-run change.

    Attributes:
        state: This process's copy of the switch state after the change.
        persisted: ``True`` when the store holds the change; ``False`` when it
            was not applied there; ``None`` when its outcome is unknown.
        applies: Where the change is in force — ``"everywhere"`` (every
            process sharing the store, within the refresh interval) or
            ``"this_process"`` (held here, retried on each refresh, for as long
            as this process lives).
        withdrew_held_change: Whether this change withdrew a change of the same
            switch that this process was holding unpersisted.
        may_still_land: ``True`` only when that withdrawn change had a write
            with an unknown outcome and this change did not commit — a write
            already sent cannot be recalled.
    """

    state: SystemState
    persisted: bool | None
    applies: str
    withdrew_held_change: bool = False
    may_still_land: bool = False

    def response_fields(self) -> dict[str, Any]:
        """The change's outcome fields for an API response."""
        return {name: getattr(self, name) for name in _RESPONSE_FIELDS}


@dataclass
class _HeldChange:
    """A change toward "Baldur acts less" the store did not confirm.

    Applied in this process and retried on every refresh pass while the
    process lives. Every retry compares the stored group with ``values``
    (already there → committed) and ``base`` (unchanged since the change was
    made → write); anything else means a change this process did not see, which
    wins. ``base_state`` is the whole state the change was based on — the state
    a commit replaced when no retry read it. A ``fork()`` child does not
    inherit the change: it belongs to ``origin_pid``.
    """

    action: str
    group: tuple[str, ...]
    updates: dict[str, Any]
    base: dict[str, Any]
    values: dict[str, Any]
    token: str
    actor: str
    reason: str
    base_state: SystemState = field(default_factory=SystemState)
    unknown_attempt: bool = False
    origin_pid: int = field(default_factory=os.getpid)


def _state_from(stored: dict[str, Any] | None) -> SystemState:
    """The switch state a stored value describes; defaults when absent."""
    if stored is None:
        return SystemState()
    return SystemState.from_dict(stored)


def _group_values(state: SystemState, group: tuple[str, ...]) -> dict[str, Any]:
    return {name: getattr(state, name) for name in group}


# =============================================================================
# System Control Manager
# =============================================================================


class SystemControlManager(EventEmitterMixin):
    """
    Manages global baldur system state with pluggable backend.

    Features:
    - Readers answer from this process's copy: no lock, no store I/O, never raise
    - The copy follows the store within one refresh interval in every process
    - Flips are versioned writes re-evaluated on the stored state
    - A kill switch or dry-run the store could not confirm is held in this
      process and retried; a re-enable it could not confirm raises
    - Kill-switch transitions this process observes reach its own subscribers

    Usage:
        manager = SystemControlManager()

        # Check if system is enabled
        if manager.is_enabled():
            do_healing()

        # Disable system (Kill Switch)
        manager.disable(reason="Emergency maintenance", actor="admin")

        # Re-enable system
        manager.enable(actor="admin")

    Configuration:
        # Django settings.py
        BALDUR_SYSTEM_CONTROL_BACKEND = "redis"  # or "file"
        BALDUR_REDIS_URL = "redis://localhost:6379/0"
    """

    _instance: SystemControlManager | None = None
    _lock = fork_safe_lock()

    # EventEmitterMixin: event source identifier
    _event_source = "system_control"

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return

        # Serializes flips (and held-change retries) so one process's events
        # follow its commit order. Readers never take it; the refresher only
        # try-acquires it.
        self._flip_lock = fork_safe_lock()
        # Guards snapshot assignment; never held across I/O.
        self._apply_lock = fork_safe_lock()
        self._pending_lock = fork_safe_lock()
        # The copy readers answer from. Replaced whole, never mutated in place,
        # so a reader's single reference read sees one consistent state.
        self._snapshot: SystemState = SystemState()
        # Exported now, not at the first pass: a store that cannot be read at
        # boot keeps this default copy in force, while the enabled gauge would
        # read its initial 0 ("disabled").
        self._export_copy(self._snapshot)
        # Bumped by every local write in the same apply-lock section that
        # assigns its outcome; a pass whose read began before it is discarded.
        self._write_generation: int = 0
        self._held: dict[str, _HeldChange] = {}
        self._pending: list[PendingChange] = []
        # Last ``enabled`` this process heard from a system-control source;
        # ``None`` (nothing heard) reads as enabled, what every subscriber
        # starts from.
        self._heard_enabled: bool | None = None
        self._subscribed: bool = False
        self._refresher = get_control_state_refresher()
        self._refresher.register(
            STATE_KEY,
            interval_seconds=SYSTEM_CONTROL_REFRESH_INTERVAL_SECONDS,
            refresh=self._refresh_from_store,
        )
        self._initialized = True

    # =========================================================================
    # Readers
    # =========================================================================

    def _ensure_live(self) -> None:
        try:
            self._refresher.ensure_live()
        except Exception as e:
            logger.debug("system_control.refresher_check_failed", error=str(e))

    def is_enabled(self) -> bool:
        """Whether Baldur is enabled (the kill switch is not pulled), in this process.

        Answers from this process's copy: no lock, no store I/O, never raises.
        Before the first successful read the copy is the default (enabled).
        """
        self._ensure_live()
        return self._snapshot.enabled

    def is_dry_run(self) -> bool:
        """Whether dry-run mode is on, in this process (same contract as ``is_enabled``)."""
        self._ensure_live()
        return self._snapshot.dry_run

    def switches(self) -> tuple[bool, bool]:
        """``(enabled, dry_run)`` from one read of this process's copy."""
        self._ensure_live()
        snapshot = self._snapshot
        return snapshot.enabled, snapshot.dry_run

    def is_persist_dirty(self) -> bool:
        """Whether this process holds a change the store has not confirmed.

        True means this process applies a kill switch or dry-run that other
        processes do not see; it is retried on every refresh pass.
        """
        return bool(self._own_held())

    def is_state_known(self) -> bool:
        """Whether this process has read the switch state from the store at least once."""
        return self._refresher.health(STATE_KEY).refreshed_at is not None

    def get_state(self, refresh: bool = True) -> SystemState:
        """
        Get the system state.

        Args:
            refresh: If True, read the store fresh and report what it holds —
                without assigning it to this process's copy (the refresher
                does that). Falls back to this process's copy when the store
                cannot be read. If False, return this process's copy.
        """
        if refresh:
            try:
                return _state_from(get_state_backend().get_strict(STATE_KEY))
            except Exception as e:
                logger.debug("system_control.status_read_failed", error=str(e))
        self._ensure_live()
        return SystemState.from_dict(self._snapshot.to_dict())

    # =========================================================================
    # Flips
    # =========================================================================

    def enable(self, actor: str = "system", reason: str = "") -> SystemControlChange:
        """Enable baldur system.

        Raises:
            SystemControlStoreError: The store did not confirm the change. This
                process's copy is unchanged; an unknown outcome is decided by
                the next successful read of the store.
        """
        now = utc_now().isoformat()
        return self._flip(
            action="enable",
            group=_ENABLED_GROUP,
            updates={"enabled": True, "enabled_at": now, "enabled_by": actor},
            actor=actor,
            reason=reason,
            acts_less=False,
        )

    def disable(self, actor: str = "system", reason: str = "") -> SystemControlChange:
        """
        Disable baldur system (Kill Switch).

        Baldur's automatic interventions step aside everywhere dry-run holds
        them back — one attempt, no breaker record or refusal, no DLQ capture —
        while what the application configured on the call and an operator's
        breaker Block stay in force. Every process sharing the store applies it
        within the refresh interval.

        When the store does not confirm the write, the kill switch is held in
        this process (``applies == "this_process"``) and retried on every
        refresh pass for as long as this process lives.
        """
        now = utc_now().isoformat()
        return self._flip(
            action="disable",
            group=_ENABLED_GROUP,
            updates={
                "enabled": False,
                "disabled_at": now,
                "disabled_by": actor,
                "disabled_reason": reason,
            },
            actor=actor,
            reason=reason,
            acts_less=True,
        )

    def enable_dry_run(self, actor: str = "system") -> SystemControlChange:
        """
        Enable dry run mode.

        In dry run mode:
        - All baldur logic executes normally
        - But actual actions (circuit breaking, retries, DLQ writes) are skipped
        - Actions that "would have been taken" are logged instead

        Use this to safely test baldur on production traffic. Held in this
        process when the store does not confirm it, like ``disable()``.
        """
        now = utc_now().isoformat()
        return self._flip(
            action="enable_dry_run",
            group=_DRY_RUN_GROUP,
            updates={
                "dry_run": True,
                "dry_run_enabled_at": now,
                "dry_run_enabled_by": actor,
            },
            actor=actor,
            reason="dry_run_mode",
            acts_less=True,
        )

    def disable_dry_run(self, actor: str = "system") -> SystemControlChange:
        """
        Disable dry run mode (go live).

        After disabling dry run, all baldur actions will be executed for real.

        Raises:
            SystemControlStoreError: The store did not confirm the change.
        """
        now = utc_now().isoformat()
        return self._flip(
            action="disable_dry_run",
            group=_DRY_RUN_GROUP,
            updates={"dry_run": False, "dry_run_disabled_at": now},
            actor=actor,
            reason="go_live",
            acts_less=False,
        )

    def _flip(
        self,
        *,
        action: str,
        group: tuple[str, ...],
        updates: dict[str, Any],
        actor: str,
        reason: str,
        acts_less: bool,
    ) -> SystemControlChange:
        """Write one flip and apply its outcome; see the class docstring for the rules."""
        with self._flip_lock:
            withdrawn = self._withdraw_held(group)
            token = new_writer_token()
            result = self._write(partial(self._apply_updates, updates), token)
            withdrew = withdrawn is not None
            if result.committed:
                local_before = self._snapshot
                after = _state_from(result.after)
                self._assign_local(after)
                self._run_commit_side_effects(
                    action,
                    _state_from(result.before),
                    after,
                    local_before,
                    actor,
                    reason,
                )
                return SystemControlChange(
                    self._copy_state(),
                    persisted=True,
                    applies=APPLIES_EVERYWHERE,
                    withdrew_held_change=withdrew,
                )

            unknown = result.outcome is VersionedWriteOutcome.UNKNOWN
            may_still_land = withdrew and bool(withdrawn and withdrawn.unknown_attempt)
            if acts_less:
                self._hold(action, group, updates, result, actor, reason, unknown)
                return SystemControlChange(
                    self._copy_state(),
                    persisted=None if unknown else False,
                    applies=APPLIES_THIS_PROCESS,
                    withdrew_held_change=withdrew,
                    may_still_land=may_still_land,
                )

            if unknown:
                self._add_pending(
                    PendingChange(
                        token=token,
                        description=action,
                        on_committed=partial(
                            self._on_pending_committed,
                            action,
                            _state_from(result.before),
                            actor,
                            reason,
                        ),
                    )
                )
                logger.warning(
                    "system_control.change_outcome_unknown",
                    action=action,
                    actor=actor,
                    error=str(result.error),
                )
            else:
                logger.warning(
                    "system_control.change_not_applied",
                    action=action,
                    actor=actor,
                    error=str(result.error),
                )
            raise SystemControlStoreError(
                change=action,
                persisted=None if unknown else False,
                applies=APPLIES_NONE,
                withdrew_held_change=withdrew,
                may_still_land=may_still_land,
            )

    @staticmethod
    def _apply_updates(
        updates: dict[str, Any], stored: dict[str, Any] | None
    ) -> MutateAnswer:
        """A flip's mutate: its fields over the state stored now."""
        return dataclasses.replace(_state_from(stored), **updates).to_dict()

    def _write(self, mutate: Any, token: str) -> VersionedWriteResult:
        try:
            backend = get_state_backend()
        except Exception as e:
            return VersionedWriteResult(
                VersionedWriteOutcome.NOT_APPLIED, token, error=e
            )
        try:
            return update_versioned(backend, STATE_KEY, mutate, token=token)
        except Exception as e:
            # A stored value this release cannot parse: nothing was written.
            return VersionedWriteResult(
                VersionedWriteOutcome.NOT_APPLIED, token, error=e
            )

    def _copy_state(self) -> SystemState:
        return SystemState.from_dict(self._snapshot.to_dict())

    # =========================================================================
    # Held changes
    # =========================================================================

    def _hold(
        self,
        action: str,
        group: tuple[str, ...],
        updates: dict[str, Any],
        result: VersionedWriteResult,
        actor: str,
        reason: str,
        unknown: bool,
    ) -> None:
        """Hold an acts-less change the store did not confirm. Caller holds ``_flip_lock``."""
        base_state = (
            _state_from(result.before) if result.read_stored else self._snapshot
        )
        base = _group_values(base_state, group)
        values = {name: updates.get(name, base[name]) for name in group}
        self._held[group[0]] = _HeldChange(
            action=action,
            group=group,
            updates=updates,
            base=base,
            values=values,
            token=result.token,
            actor=actor,
            reason=reason,
            base_state=base_state,
            unknown_attempt=unknown,
        )
        set_sc_persist_dirty(True)
        self._assign_local(self._snapshot)
        # Announced once; each failed retry logs at DEBUG. The standing
        # signals are the persist_dirty gauge and status field — under a
        # write-only failure the read health looks normal.
        logger.warning(
            "system_control.save_state_failed",
            action=action,
            actor=actor,
            outcome=result.outcome.value,
            applies=APPLIES_THIS_PROCESS,
            error=str(result.error),
        )
        if action == "disable":
            self._announce_if_unheard(False)

    def _withdraw_held(self, group: tuple[str, ...]) -> _HeldChange | None:
        """Withdraw this process's held change of ``group``. Caller holds ``_flip_lock``."""
        withdrawn = self._held.pop(group[0], None)
        if withdrawn is not None and withdrawn.origin_pid != os.getpid():
            # Inherited across fork: never this process's change to withdraw.
            withdrawn = None
        if withdrawn is not None:
            set_sc_persist_dirty(bool(self._own_held()))
            logger.info(
                "system_control.held_change_withdrawn",
                action=withdrawn.action,
                unknown_attempt=withdrawn.unknown_attempt,
            )
        return withdrawn

    @staticmethod
    def _held_mutate(held: _HeldChange, stored: dict[str, Any] | None) -> MutateAnswer:
        """A held change's retry: one comparison of the stored group, every attempt."""
        current = _state_from(stored)
        stored_group = _group_values(current, held.group)
        if stored_group == held.values:
            return ALREADY_SATISFIED
        if stored_group == held.base:
            return dataclasses.replace(current, **held.updates).to_dict()
        return Declined("changed_since_held")

    def _own_held(self) -> dict[str, _HeldChange]:
        """The held changes this process made (a ``fork()`` child made none)."""
        pid = os.getpid()
        return {k: h for k, h in self._held.items() if h.origin_pid == pid}

    def _retry_held_changes(self, backend: StateBackend) -> dict[str, Any] | None:
        """Retry every held change; returns a fresher stored value when one declined.

        Runs inside a refresh pass. ``_flip_lock`` is taken by try-acquire: a
        flip in progress keeps the held groups as they are for this pass. A
        change inherited across ``fork()`` is dropped here, never retried: it is
        held only in the process that made it.
        """
        if not self._held or not self._flip_lock.acquire(blocking=False):
            return None
        fresher: dict[str, Any] | None = None
        try:
            own = self._own_held()
            if len(own) != len(self._held):
                self._held = own
                set_sc_persist_dirty(bool(own))
            for group_name, held in list(self._held.items()):
                result = update_versioned(
                    backend,
                    STATE_KEY,
                    partial(self._held_mutate, held),
                    token=held.token,
                )
                if result.committed:
                    del self._held[group_name]
                    set_sc_persist_dirty(bool(self._held))
                    logger.info(
                        "system_control.persist_retry_succeeded", action=held.action
                    )
                    local_before = self._snapshot
                    after = _state_from(result.after)
                    self._assign_local(after)
                    # The stored group carries this change's values, whose
                    # timestamps are its own: it landed — through this retry, or
                    # through an earlier attempt whose reply was lost. Its side
                    # effects run now, from the state it replaced: the one this
                    # retry read before writing, else the state it was based on.
                    before = (
                        None if result.before is None else _state_from(result.before)
                    )
                    if before is None or _group_values(before, held.group) != held.base:
                        before = held.base_state
                    self._run_commit_side_effects(
                        held.action,
                        before,
                        after,
                        local_before,
                        held.actor,
                        held.reason,
                    )
                elif result.outcome is VersionedWriteOutcome.DECLINED:
                    del self._held[group_name]
                    set_sc_persist_dirty(bool(self._held))
                    logger.warning(
                        "system_control.held_change_dropped",
                        action=held.action,
                        reason=result.decline_reason,
                    )
                    fresher = result.after
                else:
                    if result.outcome is VersionedWriteOutcome.UNKNOWN:
                        held.unknown_attempt = True
                    logger.debug(
                        "system_control.persist_retry_failed",
                        action=held.action,
                        outcome=result.outcome.value,
                        error=str(result.error),
                    )
        finally:
            self._flip_lock.release()
        return fresher

    # =========================================================================
    # Unknown outcomes
    # =========================================================================

    def _add_pending(self, change: PendingChange) -> None:
        with self._pending_lock:
            self._pending.append(change)

    def _settle_pending(self, stored: dict[str, Any] | None) -> None:
        with self._pending_lock:
            pending, self._pending = self._pending, []
        if not pending:
            return
        committed, not_applied = settle_pending_changes(pending, stored)
        for change in committed:
            logger.info(
                "system_control.unknown_change_committed", action=change.description
            )
            try:
                change.on_committed(stored or {})
            except Exception as e:
                logger.warning(
                    "system_control.unknown_change_side_effects_failed",
                    action=change.description,
                    error=str(e),
                )
        for change in not_applied:
            logger.info(
                "system_control.unknown_change_not_applied", action=change.description
            )

    def _on_pending_committed(
        self,
        action: str,
        before: SystemState,
        actor: str,
        reason: str,
        stored: dict[str, Any],
    ) -> None:
        # The new state is the one the store holds, not this process's copy:
        # the pass that decides a pending change assigns its read afterwards.
        self._run_commit_side_effects(
            action, before, _state_from(stored), self._snapshot, actor, reason
        )

    # =========================================================================
    # Commit side effects
    # =========================================================================

    def _run_commit_side_effects(
        self,
        action: str,
        before: SystemState,
        after: SystemState,
        local_before: SystemState,
        actor: str,
        reason: str,
    ) -> None:
        """Audit, metrics, log and event of a committed flip.

        ``before`` is the stored state the flip replaced and ``after`` the
        stored state it wrote; ``local_before`` is this process's copy before
        the flip was applied here (it decides whether this process's
        subscribers still need the transition).
        """
        record_sc_state_change(action)
        handlers = {
            "enable": self._record_enable,
            "disable": self._record_disable,
            "enable_dry_run": self._record_dry_run_enabled,
            "disable_dry_run": self._record_dry_run_disabled,
        }
        handlers[action](before, after, local_before, actor, reason)

    def _record_enable(
        self,
        before: SystemState,
        after: SystemState,
        local_before: SystemState,
        actor: str,
        reason: str,
    ) -> None:
        if not before.enabled and before.disabled_at:
            try:
                from baldur.utils.time import from_iso_string

                disabled_at = from_iso_string(before.disabled_at)
                record_sc_disabled_duration((utc_now() - disabled_at).total_seconds())
            except (ValueError, TypeError):
                logger.warning(
                    "system_control.disabled_duration_parse_failed",
                    disabled_at=before.disabled_at,
                )
        if not before.enabled:
            logger.info(
                "system_control.system_enabled_reason",
                actor=actor,
                value=reason or "N/A",
            )
            self._log_audit("enable", actor, before.to_dict(), after.to_dict(), reason)
        if not local_before.enabled or not before.enabled:
            self._emit_kill_switch_event(activated=False, actor=actor, reason=reason)

    def _record_disable(
        self,
        before: SystemState,
        after: SystemState,
        local_before: SystemState,
        actor: str,
        reason: str,
    ) -> None:
        record_sc_disabled()
        if before.enabled:
            logger.warning(
                "system_control.system_disabled_kill_switch",
                actor=actor,
                value=reason or "N/A",
            )
            self._log_audit("disable", actor, before.to_dict(), after.to_dict(), reason)
        if local_before.enabled or before.enabled:
            self._emit_kill_switch_event(activated=True, actor=actor, reason=reason)

    def _record_dry_run_enabled(
        self,
        before: SystemState,
        after: SystemState,
        local_before: SystemState,
        actor: str,
        reason: str,
    ) -> None:
        if not before.dry_run:
            logger.info("system_control.dry_run_mode_enabled", actor=actor)
            self._log_audit(
                "enable_dry_run",
                actor,
                before.to_dict(),
                after.to_dict(),
                "dry_run_mode",
            )

    def _record_dry_run_disabled(
        self,
        before: SystemState,
        after: SystemState,
        local_before: SystemState,
        actor: str,
        reason: str,
    ) -> None:
        if before.dry_run:
            logger.warning("system_control.dry_run_mode_disabled", actor=actor)
            self._log_audit(
                "disable_dry_run",
                actor,
                before.to_dict(),
                after.to_dict(),
                "go_live",
            )

    def _log_audit(
        self,
        action: str,
        actor: str,
        old_state: dict,
        new_state: dict,
        reason: str,
    ) -> None:
        """
        Record a system-control change in the Audit log.

        Fail-Open principle: an Audit failure does not stop system-control logic.
        """
        log_system_control_audit(
            action=action,
            actor=actor,
            old_state=old_state,
            new_state=new_state,
            reason=reason,
        )

    def _emit_kill_switch_event(self, activated: bool, actor: str, reason: str) -> None:
        """Publish a kill-switch transition this process committed.

        Called under ``_flip_lock`` (so one process's events follow its commit
        order) and outside ``_apply_lock``: subscribers of these events are
        awaited, and readers must never wait on them.

        Emission is fail-safe (``EventEmitterMixin`` drops and logs); every
        process's refresher still applies the store within its interval.
        """
        if activated:
            event_type = EventType.KILL_SWITCH_ACTIVATED
            priority = EventPriority.CRITICAL
        else:
            # Not force-propagated during an infra outage: a lost re-enable
            # only delays resumption elsewhere, which is the safe direction.
            event_type = EventType.KILL_SWITCH_DEACTIVATED
            priority = EventPriority.HIGH

        # Subscribed first, so this process records its own transition as
        # heard and the next pass does not announce it a second time.
        self._ensure_subscribed()
        self._emit_event(
            event_type,
            data={"reason": reason, "activated_by": actor},
            priority=priority,
        )

    # =========================================================================
    # Snapshot assignment
    # =========================================================================

    def _overlay_held(self, state: SystemState) -> SystemState:
        for held in self._own_held().values():
            state = dataclasses.replace(state, **held.updates)
        return state

    def _assign_local(self, state: SystemState) -> None:
        """Assign a local write's outcome and bump the write generation, in one section."""
        with self._apply_lock:
            self._write_generation += 1
            old = self._snapshot
            new = self._overlay_held(state)
            self._snapshot = new
        self._record_copy_change(old, new, log=False)

    def _assign_read(self, state: SystemState, generation: int) -> bool:
        """Assign a pass's read unless a local write landed since the pass began."""
        with self._apply_lock:
            if self._write_generation != generation:
                return False
            old = self._snapshot
            new = self._overlay_held(state)
            self._snapshot = new
        self._record_copy_change(old, new, log=True)
        return True

    @staticmethod
    def _export_copy(state: SystemState) -> None:
        """Set the switch gauges to what this process's copy holds."""
        set_sc_enabled(state.enabled)
        set_sc_dry_run(state.dry_run)

    @classmethod
    def _record_copy_change(
        cls, old: SystemState, new: SystemState, *, log: bool
    ) -> None:
        # Every assignment exports the copy, a change or not: a copy that never
        # moves is still exported, and a recreated metrics registry catches up.
        cls._export_copy(new)
        if log and (old.enabled != new.enabled or old.dry_run != new.dry_run):
            logger.info(
                "system_control.state_changed",
                old_enabled=old.enabled,
                new_enabled=new.enabled,
                old_dry_run=old.dry_run,
                new_dry_run=new.dry_run,
            )

    # =========================================================================
    # Refresh pass
    # =========================================================================

    def _refresh_from_store(self, backend: StateBackend) -> None:
        """The refresher's callback: read strictly, decide, retry held, assign.

        Raises when the store cannot be read (the copy is kept).
        """
        generation = self._write_generation
        stored = backend.get_strict(STATE_KEY)
        state = _state_from(stored)
        self._ensure_subscribed()
        self._settle_pending(stored)
        fresher = self._retry_held_changes(backend)
        if fresher is not None:
            state = _state_from(fresher)
        if self._assign_read(state, generation):
            self._announce_if_unheard(self._snapshot.enabled)

    # =========================================================================
    # Observed transitions → this process's subscribers
    # =========================================================================

    def _ensure_subscribed(self) -> None:
        """Subscribe to kill-switch events on this process's bus, once."""
        if self._subscribed:
            return
        bus = self._get_event_bus()
        if bus is None:
            return
        try:
            bus.subscribe(EventType.KILL_SWITCH_ACTIVATED, self._on_kill_switch_event)
            bus.subscribe(EventType.KILL_SWITCH_DEACTIVATED, self._on_kill_switch_event)
            self._subscribed = True
        except Exception as e:
            logger.debug("system_control.event_subscription_failed", error=str(e))

    def _on_kill_switch_event(self, event: Any) -> None:
        """Record the ``enabled`` value a system-control event carried.

        Only a system-control source counts: the throttle's Full Stop emits
        ``KILL_SWITCH_ACTIVATED`` without a flip, and recording it would make
        the next pass announce the opposite and undo the Full Stop.
        """
        if getattr(event, "source", None) != self._event_source:
            return
        self._heard_enabled = event.event_type == EventType.KILL_SWITCH_DEACTIVATED

    def _announce_if_unheard(self, enabled: bool) -> None:
        """Tell this process's own subscribers about a transition they did not hear.

        Published to local handlers only (never to peers). Recorded as heard
        only after the publish returned, so a failed announcement repeats at
        the next pass — a duplicate, never a miss.
        """
        heard = True if self._heard_enabled is None else self._heard_enabled
        if enabled == heard:
            return
        bus = self._get_event_bus()
        publish_local = getattr(bus, "publish_local", None) if bus is not None else None
        if publish_local is None:
            return
        from baldur.services.event_bus.bus.models import create_event

        event = create_event(
            EventType.KILL_SWITCH_DEACTIVATED
            if enabled
            else EventType.KILL_SWITCH_ACTIVATED,
            {"reason": "observed_state_change", "activated_by": "system_control"},
            self._event_source,
            EventPriority.HIGH if enabled else EventPriority.CRITICAL,
        )
        try:
            publish_local(event)
        except Exception as e:
            logger.warning("system_control.announcement_failed", error=str(e))
            return
        self._heard_enabled = enabled

    # =========================================================================
    # Reset / info
    # =========================================================================

    def reset(self) -> None:
        """Reset to default state (enabled). Tests only.

        A blind write of the defaults — it must reset even a stored value this
        release cannot parse.
        """
        with self._flip_lock:
            old_state = self._snapshot.to_dict()
            self._held.clear()
            with self._pending_lock:
                self._pending.clear()
            set_sc_persist_dirty(False)
            try:
                # No TTL: control state must outlive any expiry.
                get_state_backend().set(
                    STATE_KEY, SystemState().to_dict(), ttl_seconds=None
                )
            except Exception as e:
                logger.warning("system_control.reset_write_failed", error=str(e))
            self._assign_local(SystemState())
            record_sc_state_change("reset")
            logger.info("system_control.system_state_reset_defaults")
            self._log_audit(
                "reset",
                "system",
                old_state,
                self._snapshot.to_dict(),
                "reset_to_defaults",
            )

    def close(self) -> None:
        """Unsubscribe from the event bus and drop the refresher registration."""
        if self._subscribed:
            bus = self._get_event_bus()
            if bus is not None:
                try:
                    bus.unsubscribe(
                        EventType.KILL_SWITCH_ACTIVATED, self._on_kill_switch_event
                    )
                    bus.unsubscribe(
                        EventType.KILL_SWITCH_DEACTIVATED, self._on_kill_switch_event
                    )
                except Exception:
                    pass
            self._subscribed = False
        self._refresher.unregister(STATE_KEY)

    def get_backend_info(self) -> dict[str, Any]:
        """Which store this process uses; the file store's absolute directory."""
        from baldur.settings.system_control import get_system_control_settings

        info: dict[str, Any] = {}
        try:
            backend = get_state_backend()
            info["backend_type"] = type(backend).__name__
            info["backend_class"] = (
                f"{type(backend).__module__}.{type(backend).__name__}"
            )
            directory = getattr(backend, "directory", None)
            if directory is not None:
                info["directory"] = directory
        except Exception as e:
            settings = get_system_control_settings()
            info["backend_type"] = settings.backend
            info["error"] = f"{type(e).__name__}: {e}"
            if settings.backend == "file":
                from pathlib import Path

                info["directory"] = str(Path(settings.state_dir).resolve())
        return info

    def get_refresh_status(self) -> dict[str, Any]:
        """This process's read health of the switch state, for the status response."""
        health = self._refresher.health(STATE_KEY)
        return {
            "store_reachable": health.store_reachable,
            "state_refreshed_at": (
                health.refreshed_at.isoformat() if health.refreshed_at else None
            ),
            "state_age_seconds": (
                None if health.age_seconds is None else round(health.age_seconds, 3)
            ),
            "last_store_error": health.last_error,
            "refresher_running": self._refresher.is_running,
        }


# =============================================================================
# Singleton & Factory Functions
# =============================================================================


def _cleanup_system_control(ctrl: SystemControlManager) -> None:
    SystemControlManager._instance = None
    ctrl.reset()
    ctrl.close()


from baldur.utils.singleton import make_singleton_factory

get_system_control, configure_system_control, reset_system_control = (
    make_singleton_factory(
        "system_control",
        SystemControlManager,
        cleanup_fn=_cleanup_system_control,
    )
)


def is_baldur_enabled() -> bool:
    """
    Quick check if baldur is enabled (the kill switch is not pulled).

    Answers from this process's copy and never raises. Baldur's own protected
    call paths already consult it; call it from code of your own that should
    step aside while an operator has pulled the switch:

        from baldur.services.system_control import is_baldur_enabled

        def my_healing_function():
            if not is_baldur_enabled():
                return  # Kill switch is active

            # ... healing logic
    """
    try:
        return get_system_control().is_enabled()
    except Exception:
        return True


def is_dry_run() -> bool:
    """
    Quick check if dry run mode is enabled (never raises).

    Use this before taking any action:

        from baldur.services.system_control import is_dry_run

        def trigger_circuit_breaker(service_name):
            if is_dry_run():
                logger.info(
                    "dry_run_open_circuit",
                    service_name=service_name,
                )
                return

            # Actually open the circuit breaker
            circuit_breaker.open(service_name)
    """
    try:
        return get_system_control().is_dry_run()
    except Exception:
        return False


__all__ = [
    "SYSTEM_CONTROL_REFRESH_INTERVAL_SECONDS",
    "SystemControlChange",
    "SystemState",
    "SystemControlManager",
    "get_system_control",
    "configure_system_control",
    "reset_system_control",
    "is_baldur_enabled",
    "is_dry_run",
]
