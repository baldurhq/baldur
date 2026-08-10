"""Derived coverage gates for the audit pipeline's fork repair.

Three components re-own their state after ``fork()``: the async logger, the
WAL sync worker, and the WAL itself. Which entry points must trigger that
repair, and which attributes it must renew, were twice maintained by hand and
twice came out incomplete — a different member missing each time. So the
contract is not a list any more, it is derived here:

- **Entry-point gate** — every public callable on a repaired class carries the
  ``@fork_repaired`` marker, or appears in that class's written-down
  ``EXEMPT`` set. Nothing can be added to the class and silently escape.
- **Attribute gate** — every attribute that is a lock, an ``Event``, one of
  the composed rate-limiting primitives, or an object that owns one of those,
  is actually replaced by a repair run, or appears in that class's
  written-down attribute exemptions. Proven by object identity across a
  simulated fork, not by reading the repair's source.

Both gates carry a negative half (``TestGatesFailWhenCoverageRegresses``):
without it, a gate that silently derives an empty set reports green forever,
which is exactly the failure mode these gates exist to end.

The behavioural fork-revival suites live alongside this module; this one is
purely structural.
"""

from __future__ import annotations

import inspect
import os
import threading

import pytest

from baldur.audit.sync_worker import AuditSyncWorker
from baldur.audit.wal import WriteAheadLog
from baldur.audit.wal._models import WALConfig
from baldur.core.rate_limiting import CooldownGate, SlidingWindowCounter
from baldur.utils.async_logger import AsyncHealingLogger

# =============================================================================
# Written-down exemptions
# =============================================================================

# Public callables that legitimately do NOT repair.
EXEMPT_ENTRY_POINTS: dict[type, set[str]] = {
    # Nothing is exempt: every public classmethod reaches one of the two class
    # locks, directly or through a helper.
    AsyncHealingLogger: set(),
    AuditSyncWorker: {
        # Delegates to the WAL, which repairs itself; touches no state the
        # worker's repair renews.
        "get_lag",
        # Pure injection of a collaborator.
        "set_checkpoint_strategy",
        # A status read must not mutate; honesty comes from composing thread
        # aliveness instead.
        "is_running",
        # Singleton classmethods: no instance in hand at entry.
        "get_instance",
        # ... and this one repairs transitively, through stop().
        "reset_instance",
    },
    WriteAheadLog: {
        # Properties: plain reads of resolution results, no fork-fragile state.
        "wal_dir",
        "resolved_dir",
    },
}

# Fork-fragile attributes the repair deliberately does NOT replace.
EXEMPT_ATTRIBUTES: dict[type, set[str]] = {
    AsyncHealingLogger: {
        # The gate that serializes the repair itself. It is only ever acquired
        # after an origin-PID mismatch, and the process that owns the state
        # never mismatches, so it is never inherited held.
        "_repair_gate",
    },
    AuditSyncWorker: {
        "_repair_gate",
        # Singleton construction lock. get_instance double-checks before
        # locking, so after the first process's init it is never acquired
        # again in a served deployment.
        "_instance_lock",
        # A collaborator that owns both an RLock and an OS file lock. Making
        # inherited collaborator singletons fork-safe needs a decision about
        # the collaborator layer as a whole (which singletons hold locks or
        # handles, and what a plugin is required to do about it), so it is
        # tracked as its own piece of work rather than settled inside the
        # audit pipeline. The exemption exists so that work cannot be
        # forgotten silently.
        "_checkpoint_strategy",
    },
    WriteAheadLog: {
        "_repair_gate",
    },
}

# Process-local latches and counters the repair must clear. These carry no
# distinguishing type, so unlike the locks above they cannot be derived — the
# gate checks the written-down set is honoured rather than deriving it.
RESET_LATCHES: dict[type, dict[str, object]] = {
    AuditSyncWorker: {
        "_no_adapter_warned": False,
        "_cursor_stall_alerted": False,
        "_stall_cycles": 0,
        "_batches_since_checkpoint": 0,
        "_orphans_absorbed": False,
    },
    WriteAheadLog: {
        "_total_entries": 0,
        "_corrupted_entries": 0,
        "_recovered_entries": 0,
        "_last_write_time": None,
    },
}

_FORK_FRAGILE_TYPES = (
    type(threading.Lock()),
    type(threading.RLock()),
    threading.Event,
    SlidingWindowCounter,
    CooldownGate,
)


# =============================================================================
# Derivation helpers — these ARE the gate; the tests only assert over them
# =============================================================================


def _contributed_functions(raw: object) -> list[object]:
    """Underlying functions a raw class attribute contributes.

    Unwraps ``classmethod`` / ``staticmethod`` (otherwise a function-only
    predicate matches zero members on the class with the most entry points)
    and ``property`` (otherwise a callable-only predicate skips every status
    surface).
    """
    if isinstance(raw, (classmethod, staticmethod)):
        return [raw.__func__]
    if isinstance(raw, property):
        return [f for f in (raw.fget, raw.fset, raw.fdel) if f is not None]
    if inspect.isfunction(raw):
        return [raw]
    return []


def public_entry_points(cls: type) -> dict[str, object]:
    """Public callables of ``cls``, walking the MRO.

    A ``vars(cls)``-based scan sees none of the members a mixin contributes —
    on the WAL that is the entire write, read and disk-management surface —
    and reports green over them. Dunders are exempt as a class: they route to
    covered public methods.
    """
    found: dict[str, object] = {}
    for klass in cls.__mro__:
        if klass is object:
            continue
        for name, raw in vars(klass).items():
            if name.startswith("_") or name in found:
                continue
            functions = _contributed_functions(raw)
            if functions:
                found[name] = functions[0]
    return found


def undecorated_entry_points(cls: type, exempt: set[str]) -> set[str]:
    """Public callables missing the repair marker, minus the exemptions."""
    undecorated = {
        name
        for name, fn in public_entry_points(cls).items()
        if not getattr(fn, "__fork_repaired__", False)
    }
    return undecorated - exempt


def misordered_decorations(cls: type) -> set[str]:
    """Members decorated in the wrong order.

    ``@classmethod`` must be outermost. The reverse hands the repair decorator
    a ``classmethod`` object, which is not callable on the supported
    interpreter range — and leaves a plain function in the class body that
    still carries the marker, so the entry-point gate would pass.
    """
    misordered = set()
    for klass in cls.__mro__:
        if klass is object:
            continue
        for name, raw in vars(klass).items():
            if not inspect.isfunction(raw):
                continue
            if not getattr(raw, "__fork_repaired__", False):
                continue
            if isinstance(
                getattr(raw, "__wrapped__", None), (classmethod, staticmethod)
            ):
                misordered.add(name)
    return misordered


def attribute_names(subject: object) -> set[str]:
    """Every attribute name of ``subject``, without invoking anything.

    Reads the union of the MRO's class attributes, the instance ``__dict__``
    and every name declared through ``__slots__``. A slotted instance has no
    ``__dict__`` at all, so a ``vars(obj)``-only scan reports green over
    everything such a class owns. ``dir()`` and ``inspect.getmembers()`` are
    deliberately not used as the source: both *invoke* property getters.
    """
    cls = subject if inspect.isclass(subject) else type(subject)
    names: set[str] = set()

    for klass in cls.__mro__:
        if klass is object:
            continue
        names |= set(vars(klass))
        slots = vars(klass).get("__slots__", ())
        if isinstance(slots, str):
            slots = (slots,)
        names |= set(slots)

    if not inspect.isclass(subject):
        names |= set(getattr(subject, "__dict__", {}))

    return {name for name in names if not name.startswith("__")}


def attribute_value(subject: object, name: str) -> object:
    """Read an attribute without running any user code.

    An ordinary ``getattr`` would invoke property getters — the same objection
    that rules out ``dir()`` as the enumeration source — so the value comes
    from the instance dict or from the MRO's class dicts. The one descriptor
    that must be resolved is a ``__slots__`` member: it is the only place a
    slotted instance's value lives, and reading it runs nothing.
    """
    if not inspect.isclass(subject):
        instance_dict = getattr(subject, "__dict__", {})
        if name in instance_dict:
            return instance_dict[name]

    cls = subject if inspect.isclass(subject) else type(subject)
    for klass in cls.__mro__:
        if name not in vars(klass):
            continue
        raw = vars(klass)[name]
        if inspect.ismemberdescriptor(raw) and not inspect.isclass(subject):
            return getattr(subject, name, None)
        return raw
    return None


def is_fork_fragile(value: object) -> bool:
    """True for a lock/Event/rate-limiting primitive, or an object owning one.

    The one-level descent is what catches a lock held inside a composed
    collaborator: those are invisible to a scan that only looks at the
    attribute's own type, while being exactly as unusable after a fork.
    """
    if isinstance(value, _FORK_FRAGILE_TYPES):
        return True
    inner = getattr(value, "__dict__", None)
    if isinstance(inner, dict):
        return any(isinstance(v, _FORK_FRAGILE_TYPES) for v in inner.values())
    return False


def fork_fragile_attributes(subject: object) -> set[str]:
    """Names on ``subject`` holding fork-fragile state."""
    fragile = set()
    for name in attribute_names(subject):
        raw = attribute_value(subject, name)
        if isinstance(raw, property) or inspect.isfunction(raw):
            continue
        if is_fork_fragile(raw):
            fragile.add(name)
    return fragile


def unrepaired_fork_fragile_attributes(subject: object, exempt: set[str]) -> set[str]:
    """Fork-fragile attributes a repair run leaves as the same object.

    Runs the repair for real against a simulated fork and compares identity,
    so the answer describes the repair's behaviour rather than its source.
    """
    names = fork_fragile_attributes(subject)
    before = {name: attribute_value(subject, name) for name in names}

    subject._origin_pid = os.getpid() + 1  # type: ignore[attr-defined]
    subject._repair_if_forked()  # type: ignore[attr-defined]

    unrepaired = {
        name for name in names if attribute_value(subject, name) is before[name]
    }
    return unrepaired - exempt


# =============================================================================
# Subjects
# =============================================================================


@pytest.fixture
def wal(tmp_path):
    instance = WriteAheadLog(
        config=WALConfig(
            wal_dir=str(tmp_path), sync_on_write=False, file_prefix="gate_wal"
        )
    )
    yield instance
    instance.close()


@pytest.fixture
def sync_worker():
    from baldur.audit.checkpoint import FileCheckpointStorage

    worker = AuditSyncWorker(wal=None, central_adapter=None)
    # Wire the collaborator whose nested lock the attribute gate must see; an
    # unset collaborator would make the one-level descent vacuous here.
    worker.set_checkpoint_strategy(FileCheckpointStorage())
    return worker


@pytest.fixture
def logger_class():
    AsyncHealingLogger.reset()
    yield AsyncHealingLogger
    AsyncHealingLogger.reset()


# =============================================================================
# Gate 1 — entry-point coverage
# =============================================================================


class TestForkRepairEntryPointCoverage:
    """Every public callable on a repaired class repairs, or is exempt."""

    @pytest.mark.parametrize(
        "cls",
        [AsyncHealingLogger, AuditSyncWorker, WriteAheadLog],
        ids=["async_logger", "sync_worker", "wal"],
    )
    def test_no_public_callable_escapes_the_repair(self, cls):
        assert undecorated_entry_points(cls, EXEMPT_ENTRY_POINTS[cls]) == set()

    @pytest.mark.parametrize(
        "cls",
        [AsyncHealingLogger, AuditSyncWorker, WriteAheadLog],
        ids=["async_logger", "sync_worker", "wal"],
    )
    def test_exemptions_name_real_members(self, cls):
        """An exemption for a member that no longer exists hides a gap."""
        members = set(public_entry_points(cls)) | {
            name
            for klass in cls.__mro__
            for name in vars(klass)
            if not name.startswith("_")
        }
        assert EXEMPT_ENTRY_POINTS[cls] <= members

    @pytest.mark.parametrize(
        "cls",
        [AsyncHealingLogger, AuditSyncWorker, WriteAheadLog],
        ids=["async_logger", "sync_worker", "wal"],
    )
    def test_decorator_order_is_pinned(self, cls):
        assert misordered_decorations(cls) == set()

    def test_wal_entry_points_come_from_every_mixin(self):
        """The MRO-walk proof: a vars(cls) scan would see none of these."""
        derived = set(public_entry_points(WriteAheadLog))

        assert {"write", "recover_orphans", "check_disk_recovery"} <= derived
        assert "write" not in vars(WriteAheadLog)

    def test_spawn_helpers_repair_even_though_they_are_private(self):
        """Both revival triggers converge on the spawn helpers.

        The respawn contract forbids consulting the running flag, so a
        watchdog respawn reaches the helper without passing any public entry
        point; the repair has to live there too.
        """
        logger_spawn = vars(AsyncHealingLogger)["_spawn_worker_thread"].__func__
        worker_spawn = vars(AuditSyncWorker)["_spawn_thread"]

        assert logger_spawn.__fork_repaired__ is True
        assert worker_spawn.__fork_repaired__ is True


# =============================================================================
# Gate 2 — attribute coverage
# =============================================================================


class TestForkRepairAttributeCoverage:
    """Every fork-fragile attribute is replaced by a repair, or is exempt."""

    def test_async_logger_replaces_its_fork_fragile_attributes(self, logger_class):
        assert (
            unrepaired_fork_fragile_attributes(
                logger_class, EXEMPT_ATTRIBUTES[AsyncHealingLogger]
            )
            == set()
        )

    def test_sync_worker_replaces_its_fork_fragile_attributes(self, sync_worker):
        assert (
            unrepaired_fork_fragile_attributes(
                sync_worker, EXEMPT_ATTRIBUTES[AuditSyncWorker]
            )
            == set()
        )

    def test_wal_replaces_its_fork_fragile_attributes(self, wal):
        assert (
            unrepaired_fork_fragile_attributes(wal, EXEMPT_ATTRIBUTES[WriteAheadLog])
            == set()
        )

    def test_nested_collaborator_locks_are_visible_to_the_gate(self, sync_worker):
        """A lock owned by a collaborator counts, and is named, not ignored."""
        assert "_checkpoint_strategy" in fork_fragile_attributes(sync_worker)
        assert "_checkpoint_strategy" in EXEMPT_ATTRIBUTES[AuditSyncWorker]

    def test_sync_worker_clears_its_process_local_latches(self, sync_worker):
        sync_worker._no_adapter_warned = True
        sync_worker._cursor_stall_alerted = True
        sync_worker._stall_cycles = 7
        sync_worker._batches_since_checkpoint = 4
        sync_worker._orphans_absorbed = True

        sync_worker._origin_pid = os.getpid() + 1
        sync_worker._repair_if_forked()

        for name, expected in RESET_LATCHES[AuditSyncWorker].items():
            assert getattr(sync_worker, name) == expected

    def test_wal_clears_its_process_local_counters(self, wal):
        wal.write({"event": "before-fork"})
        assert wal._total_entries > 0

        wal._origin_pid = os.getpid() + 1
        wal._repair_if_forked()

        for name, expected in RESET_LATCHES[WriteAheadLog].items():
            assert getattr(wal, name) == expected


# =============================================================================
# The gates' own negative half
# =============================================================================


class TestGatesFailWhenCoverageRegresses:
    """Without these, a gate that derives nothing reports green forever."""

    def test_entry_point_gate_catches_an_undecorated_public_method(self):
        class Subject(WriteAheadLog):
            def newly_added_public_method(self):
                with self._lock:
                    return self._sequence

        assert undecorated_entry_points(
            Subject, EXEMPT_ENTRY_POINTS[WriteAheadLog]
        ) == {"newly_added_public_method"}

    def test_entry_point_gate_catches_a_bare_wrapper_without_the_marker(self):
        import functools

        def looks_like_the_decorator(method):
            @functools.wraps(method)
            def wrapper(self, *args, **kwargs):
                self._repair_if_forked()
                return method(self, *args, **kwargs)

            return wrapper

        class Subject(WriteAheadLog):
            @looks_like_the_decorator
            def newly_added_public_method(self):
                return None

        assert "newly_added_public_method" in undecorated_entry_points(
            Subject, EXEMPT_ENTRY_POINTS[WriteAheadLog]
        )

    def test_decorator_order_gate_catches_the_reversed_order(self):
        from baldur.core.process_utils import fork_repaired

        class Subject(WriteAheadLog):
            fork_repaired_outside = fork_repaired(classmethod(lambda cls: None))

        assert "fork_repaired_outside" in misordered_decorations(Subject)

    def test_attribute_gate_catches_a_lock_the_repair_does_not_replace(self, wal):
        wal._newly_added_lock = threading.Lock()

        assert unrepaired_fork_fragile_attributes(
            wal, EXEMPT_ATTRIBUTES[WriteAheadLog]
        ) == {"_newly_added_lock"}

    def test_attribute_gate_catches_a_lock_declared_through_slots(self):
        class SlottedCollaborator:
            __slots__ = ("_slotted_lock",)

            def __init__(self):
                self._slotted_lock = threading.Lock()

            def _repair_if_forked(self):
                return None

        subject = SlottedCollaborator()

        assert "_slotted_lock" in attribute_names(subject)
        assert fork_fragile_attributes(subject) == {"_slotted_lock"}

    def test_attribute_gate_never_invokes_a_property_getter(self, tmp_path):
        """Enumeration must not touch the status surfaces it walks over."""
        calls = []

        class Subject(WriteAheadLog):
            @property
            def observed_property(self):
                calls.append("invoked")
                return None

        subject = Subject(
            config=WALConfig(
                wal_dir=str(tmp_path), sync_on_write=False, file_prefix="gate_probe"
            )
        )
        try:
            attribute_names(subject)
            fork_fragile_attributes(subject)
        finally:
            subject.close()

        assert calls == []
