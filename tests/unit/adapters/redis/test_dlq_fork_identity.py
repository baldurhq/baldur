"""Fork-safe composite entry-id identity for the Redis DLQ repository (782).

A prefork pool child inherits the repository object its parent built. Before
this, the composite entry id was read straight off state captured at
construction, so every sibling allocated the same ``{pod}:{pid}:{nonce}:{seq}``
tokens and each ``create()`` overwrote the previous sibling's entry — twelve
deterministically failing tasks over twelve child recycles stored one entry,
with no error anywhere.

``os.fork()`` is absent on the Windows dev box, so the inherited state is
simulated here by stamping the identity with an ``origin_pid`` this process
cannot have (the outbox suite's foreign-pid idiom). What a simulation cannot
show is an object that really crossed a process boundary carrying its state:
that claim belongs to the real-fork node,
``integration/dlq/test_dlq_real_fork_uniqueness.py``, which is the only place a
child's own pid is a fact rather than a constructor argument.

Test classes:
    - TestEntryIdentityContract: the value object's shape
    - TestForkIdentityRepairBehavior: re-owning, the no-op path, idempotency,
      the sequence restart, and the raising-log exit path
    - TestForkRepairReachesEveryAccessorBehavior: every holder of the instance
    - TestConcurrentFirstAllocationBehavior: two first allocations in one
      fresh child
    - TestEntryIdentitySingleReaderContract: the identity has one reader
"""

from __future__ import annotations

import ast
import os
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import FrozenInstanceError
from functools import lru_cache
from pathlib import Path
from typing import Any, NamedTuple
from unittest.mock import MagicMock

import pytest
import structlog
from structlog.testing import capture_logs

import baldur
from baldur.adapters.redis.dlq import (
    _RUN_NONCE_BYTES,
    RedisDLQRepository,
    _EntryIdentity,
    get_redis_dlq_repo,
    reset_redis_dlq_repo,
)
from baldur.adapters.resilient.backend import ResilientStorageBackend
from baldur.factory.registry import ProviderRegistry
from baldur.services.dlq_capture.service import DLQCaptureService

_REPAIR_EVENT = "redis_dlq.fork_state_repaired"

# A pid this process cannot have: os.getpid() is positive on every supported
# platform, so a negative stamp is unambiguously "some other process".
_FOREIGN_PID = -1

# What the inherited identity carries. An id built from the parent's object is
# recognisable on sight, which is what the negative assertions key on.
_INHERITED_POD = "parent-pod"
_INHERITED_PID = 424242
_INHERITED_NONCE = "1nher1ted0nce0aa"

# Edge-triggered bounds: every wait below is released by a Barrier or a thread
# exit, so these bound a hang rather than pace the test.
_BARRIER_TIMEOUT_SECONDS = 10.0
_THREAD_JOIN_TIMEOUT_SECONDS = 10.0


def _make_repo(**identity_seams: Any) -> RedisDLQRepository:
    """Repository whose backend is never reached — ``_allocate_id`` is pure."""
    return RedisDLQRepository(MagicMock(spec=ResilientStorageBackend), **identity_seams)


def _stamp_inherited_identity(repo: RedisDLQRepository) -> _EntryIdentity:
    """Install the identity a fork child inherits: another process's origin."""
    inherited = _EntryIdentity(
        pod_id=_INHERITED_POD,
        pid=_INHERITED_PID,
        run_nonce=_INHERITED_NONCE,
        origin_pid=_FOREIGN_PID,
    )
    repo._entry_identity = inherited
    return inherited


class _Id(NamedTuple):
    """The four segments of a composite entry id."""

    pod_id: str
    pid: str
    run_nonce: str
    seq: str


def _parse_id(entry_id: str) -> _Id:
    """Split ``{pod_id}:{pid}:{run_nonce}:{seq}`` from the right.

    From the right because a pod id is a hostname and may carry a colon of its
    own; the three trailing segments never do.
    """
    return _Id(*entry_id.rsplit(":", 3))


def _repair_lines(logs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The repair announcements among captured log entries."""
    return [log for log in logs if log["event"] == _REPAIR_EVENT]


@contextmanager
def _emission_of(event: str, raises: BaseException) -> Iterator[None]:
    """Make one structlog event's emission raise; drop every other event.

    The processor list instance is mutated in place rather than replaced —
    ``capture_logs`` does the same, because a bound logger holds a reference to
    the list it was configured with.
    """
    processors = structlog.get_config()["processors"]
    saved = list(processors)

    def _raise_on_event(
        logger: Any, method_name: str, event_dict: dict[str, Any]
    ) -> dict[str, Any]:
        if event_dict.get("event") == event:
            raise raises
        raise structlog.DropEvent

    processors.clear()
    processors.append(_raise_on_event)
    structlog.configure(processors=processors)
    try:
        yield
    finally:
        processors.clear()
        processors.extend(saved)
        structlog.configure(processors=processors)


class TestEntryIdentityContract:
    """The value object's shape — what makes the lock-free swap safe."""

    def test_identity_rejects_field_rebinding(self):
        """Frozen: a repair replaces the object rather than mutating fields, so
        an allocation can never observe a half-updated identity."""
        identity = _EntryIdentity("pod", 1, "nonce", origin_pid=os.getpid())

        with pytest.raises(FrozenInstanceError):
            identity.run_nonce = "rebound"

    def test_identity_without_an_origin_pid_is_rejected(self):
        """``origin_pid`` is the fork check's only input, so it cannot default
        — a defaulted one would make an un-stamped identity look native."""
        with pytest.raises(TypeError):
            _EntryIdentity("pod", 1, "nonce")

    def test_each_identity_owns_its_own_sequence_counter(self):
        """The counter belongs to the namespace ``run_nonce`` names, so a
        replaced identity takes its counter with it."""
        first = _EntryIdentity("pod", 1, "nonce-a", origin_pid=os.getpid())
        second = _EntryIdentity("pod", 1, "nonce-b", origin_pid=os.getpid())

        assert next(first.seq) == 0
        assert next(first.seq) == 1
        assert next(second.seq) == 0

    def test_run_nonce_carries_64_bits_of_entropy(self):
        """Design contract: collision freedom across uncoordinated processes
        rests on the nonce's width and on nothing else — there is deliberately
        no collision detector on the write path."""
        assert _RUN_NONCE_BYTES == 8

        repo = _make_repo()

        assert len(repo._run_nonce) == _RUN_NONCE_BYTES * 2


class TestForkIdentityRepairBehavior:
    """``_repair_if_forked()`` — the child re-owns pid, nonce and counter."""

    def test_allocation_after_an_inherited_identity_carries_this_process_pid(self):
        """The whole defect in one assertion: an id allocated by a process that
        did not construct the repository must not name the constructor's."""
        # Given the identity a fork child inherits.
        repo = _make_repo()
        inherited = _stamp_inherited_identity(repo)

        # When it allocates for the first time.
        entry_id = repo._allocate_id()

        # Then it allocated from an identity of its own.
        parsed = _parse_id(entry_id)
        assert parsed.pid == str(os.getpid())
        assert parsed.pid != str(_INHERITED_PID)
        assert parsed.run_nonce != inherited.run_nonce
        assert parsed.seq == "0"
        assert parsed.pod_id == _INHERITED_POD
        assert repo._entry_identity is not inherited
        assert repo._entry_identity.origin_pid == os.getpid()

    def test_repair_line_names_the_nonce_the_id_was_built_from(self):
        """The line is the operator's only view of a repair. One naming a nonce
        that no id carries would be worse than no line at all."""
        repo = _make_repo()
        inherited = _stamp_inherited_identity(repo)

        with capture_logs() as logs:
            entry_id = repo._allocate_id()

        lines = _repair_lines(logs)
        assert len(lines) == 1
        assert lines[0]["log_level"] == "info"
        assert lines[0]["pid"] == os.getpid()
        assert lines[0]["inherited_pid"] == _FOREIGN_PID
        assert lines[0]["inherited_run_nonce"] == inherited.run_nonce
        assert lines[0]["run_nonce"] == _parse_id(entry_id).run_nonce

    def test_matching_origin_pid_leaves_an_injected_identity_untouched(self):
        """The check keys on ``origin_pid``, never on ``pid``: a pid-keyed
        check would fire on the next allocation of any injected identity and
        overwrite the constructor seam N-worker tests are built on."""
        repo = _make_repo(pod_id="pod-a", pid=100, run_nonce="nonce0")
        identity = repo._entry_identity

        with capture_logs() as logs:
            entry_id = repo._allocate_id()

        assert entry_id == "pod-a:100:nonce0:0"
        assert repo._entry_identity is identity
        assert _repair_lines(logs) == []

    def test_second_allocation_repairs_nothing_and_announces_once(self):
        """The repair is lazy and one-shot: an announcement per allocation
        would make the line useless as a per-child fork signal."""
        repo = _make_repo()
        _stamp_inherited_identity(repo)

        with capture_logs() as logs:
            entry_ids = [repo._allocate_id() for _ in range(3)]

        assert len(_repair_lines(logs)) == 1
        assert {_parse_id(entry_id).run_nonce for entry_id in entry_ids} == {
            repo._entry_identity.run_nonce
        }
        assert [_parse_id(entry_id).seq for entry_id in entry_ids] == ["0", "1", "2"]

    def test_repair_restarts_the_sequence_the_parent_had_advanced(self):
        """The child restarts at 0 while inheriting a counter the parent had
        advanced — safe only because the nonce it restarts under is its own."""
        # Given a parent that has already allocated, then a fork.
        repo = _make_repo()
        parent_ids = [repo._allocate_id() for _ in range(3)]
        inherited = repo._entry_identity
        repo._entry_identity = _EntryIdentity(
            pod_id=inherited.pod_id,
            pid=inherited.pid,
            run_nonce=inherited.run_nonce,
            origin_pid=_FOREIGN_PID,
            seq=inherited.seq,
        )

        # When the child allocates.
        child_id = repo._allocate_id()

        # Then the sequence restarts under a namespace of the child's own.
        parsed = _parse_id(child_id)
        assert parsed.seq == "0"
        assert [_parse_id(entry_id).seq for entry_id in parent_ids] == ["0", "1", "2"]
        assert parsed.run_nonce != _parse_id(parent_ids[0]).run_nonce
        assert parsed.pid == str(os.getpid())
        assert child_id not in parent_ids

    def test_a_raising_repair_line_still_leaves_the_identity_re_owned(self):
        """The store precedes the log call, so a transient logging failure
        fails one allocation and heals. With the two swapped, the identity
        would stay inherited and every later call would mint another nonce —
        turning a logging outage into a permanently re-raising ``create()``."""
        # Given a fork child whose repair announcement cannot be emitted.
        repo = _make_repo()
        _stamp_inherited_identity(repo)
        failure = RuntimeError("log sink down")

        # When the first allocation runs into it.
        with (
            _emission_of(_REPAIR_EVENT, raises=failure),
            pytest.raises(RuntimeError) as raised,
        ):
            repo._allocate_id()

        # Then the repair itself survived — only its report failed.
        assert raised.value is failure
        assert repo._entry_identity.origin_pid == os.getpid()

        with capture_logs() as logs:
            entry_id = repo._allocate_id()

        parsed = _parse_id(entry_id)
        assert parsed.pid == str(os.getpid())
        assert parsed.seq == "0"
        assert _repair_lines(logs) == []


@contextmanager
def _registry_held_repo(
    backend: ResilientStorageBackend,
) -> Iterator[Callable[[], RedisDLQRepository]]:
    """The repository resolved through the provider registry slot."""
    with ProviderRegistry.failed_op_repo.isolated_context() as isolated:
        isolated.register("redis", lambda: RedisDLQRepository(backend))
        yield lambda: isolated.get("redis")


@contextmanager
def _module_singleton_repo(
    backend: ResilientStorageBackend,
) -> Iterator[Callable[[], RedisDLQRepository]]:
    """The repository held by the adapter module's own singleton."""
    reset_redis_dlq_repo()
    try:
        yield lambda: get_redis_dlq_repo(backend=backend)
    finally:
        reset_redis_dlq_repo()


@contextmanager
def _service_held_repo(
    backend: ResilientStorageBackend,
) -> Iterator[Callable[[], RedisDLQRepository]]:
    """The repository a capture service was constructed around."""
    service = DLQCaptureService(repository=RedisDLQRepository(backend))
    yield lambda: service.repository


_ACCESSORS: dict[str, Any] = {
    "registry": _registry_held_repo,
    "module_singleton": _module_singleton_repo,
    "service": _service_held_repo,
}


class TestForkRepairReachesEveryAccessorBehavior:
    """Every holder of the instance hands back the object the repair re-owns."""

    @pytest.mark.parametrize("accessor_name", list(_ACCESSORS), ids=list(_ACCESSORS))
    def test_identity_is_re_owned_through_every_holder_of_the_instance(
        self, accessor_name
    ):
        """A repair sited where one holder cannot reach it leaves that holder's
        children allocating under the parent's namespace. Siting it on the
        identity's single reader is what makes all three equivalent."""
        backend = MagicMock(spec=ResilientStorageBackend)

        with _ACCESSORS[accessor_name](backend) as resolve:
            held = resolve()
            _stamp_inherited_identity(held)

            resolved = resolve()
            entry_id = resolved._allocate_id()

        assert resolved is held
        parsed = _parse_id(entry_id)
        assert parsed.pid == str(os.getpid())
        assert parsed.run_nonce != _INHERITED_NONCE
        assert resolved._entry_identity.origin_pid == os.getpid()


def _race_two_first_allocations(
    repo: RedisDLQRepository,
) -> tuple[list[str], list[str]]:
    """Release two threads into the first allocation of a fresh child.

    Returns the ids they allocated and the nonces the repair lines announced.
    """
    barrier = threading.Barrier(2)
    allocated: list[str] = []

    def _allocate() -> None:
        barrier.wait(timeout=_BARRIER_TIMEOUT_SECONDS)
        allocated.append(repo._allocate_id())

    threads = [threading.Thread(target=_allocate) for _ in range(2)]
    with capture_logs() as logs:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=_THREAD_JOIN_TIMEOUT_SECONDS)

    assert not any(thread.is_alive() for thread in threads)
    assert len(allocated) == 2
    return allocated, [line["run_nonce"] for line in _repair_lines(logs)]


class TestConcurrentFirstAllocationBehavior:
    """Two threads reaching the first allocation of a fresh child at once."""

    def test_concurrent_first_allocations_stay_distinct_under_any_interleaving(self):
        """The repair takes no lock on purpose — one taken here would be held
        exactly by a process whose pid mismatches, so a grandchild forked in
        that window would inherit it held and block forever. The whole-object
        swap replaces it: whichever store wins, both ids must be distinct and
        each must carry a nonce some repair line announced."""
        repo = _make_repo()
        _stamp_inherited_identity(repo)

        allocated, announced = _race_two_first_allocations(repo)

        assert allocated[0] != allocated[1]
        for entry_id in allocated:
            assert _parse_id(entry_id).pid == str(os.getpid())
        carried = [_parse_id(entry_id).run_nonce for entry_id in allocated]
        assert set(carried) <= set(announced)
        if len(announced) == 2:
            # Both threads loaded the inherited object, so each allocated from
            # the identity it installed itself: distinct nonces, both seq 0.
            assert set(carried) == set(announced)
            assert {_parse_id(entry_id).seq for entry_id in allocated} == {"0"}
        else:
            # One thread installed; the other loaded that object and shares its
            # counter: one nonce, distinct seqs.
            assert len(announced) == 1
            assert carried[0] == carried[1]
            assert {_parse_id(entry_id).seq for entry_id in allocated} == {"0", "1"}

    def test_allocations_after_the_race_continue_the_surviving_identity(self):
        """The identity that lost the race is unreachable afterwards: a later
        allocation must not resurrect its namespace."""
        repo = _make_repo()
        _stamp_inherited_identity(repo)
        allocated, _ = _race_two_first_allocations(repo)

        with capture_logs() as logs:
            later = repo._allocate_id()

        surviving = repo._entry_identity
        assert _parse_id(later).run_nonce == surviving.run_nonce
        assert _parse_id(later).pid == str(os.getpid())
        assert later not in allocated
        assert _repair_lines(logs) == []


_IDENTITY_ATTRIBUTE = "_entry_identity"
_ALLOCATOR = "_allocate_id"
_REPAIR = "_repair_if_forked"
# The repair, plus the three read-only diagnostic views. The walk is asserted
# to find exactly these four, spelled out, so widening this set to admit a new
# reader is a visible failure rather than a silent waiver.
_ALLOWED_READERS = frozenset({_REPAIR, "_pod_id", "_pid", "_run_nonce"})


class _Load(NamedTuple):
    """One read of the identity attribute, with the member that performs it."""

    module: str
    lineno: int
    member: str


class _Allocator(NamedTuple):
    """One ``_allocate_id`` definition, as the contract sees it."""

    module: str
    binds_the_repair: bool
    reads_the_identity: bool


class _PackageScan(NamedTuple):
    """Everything the single-reader contract needs, from one walk."""

    modules: int
    loads: tuple[_Load, ...]
    allocators: tuple[_Allocator, ...]
    name_constants: tuple[tuple[str, int], ...]


class _IdentityLoadCollector(ast.NodeVisitor):
    """Collect identity reads together with the member enclosing each."""

    def __init__(self, module: str) -> None:
        self._module = module
        self._members: list[str] = []
        self.loads: list[_Load] = []

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._members.append(node.name)
        self.generic_visit(node)
        self._members.pop()

    visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr == _IDENTITY_ATTRIBUTE and isinstance(node.ctx, ast.Load):
            member = self._members[-1] if self._members else "<module>"
            self.loads.append(_Load(self._module, node.lineno, member))
        self.generic_visit(node)


def _first_statement(node: ast.FunctionDef) -> ast.stmt | None:
    """The first statement of a body, looking past a docstring."""
    body = node.body
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body = body[1:]
    return body[0] if body else None


def _binds_the_repair(statement: ast.stmt | None) -> bool:
    """``<name> = self._repair_if_forked()`` and nothing else."""
    if not isinstance(statement, ast.Assign):
        return False
    call = statement.value
    return (
        isinstance(call, ast.Call)
        and isinstance(call.func, ast.Attribute)
        and call.func.attr == _REPAIR
        and isinstance(call.func.value, ast.Name)
        and call.func.value.id == "self"
        and not call.args
        and not call.keywords
    )


def _touches_identity(node: ast.AST) -> bool:
    """Any mention of the identity attribute in this subtree, load or store."""
    return any(
        isinstance(child, ast.Attribute) and child.attr == _IDENTITY_ATTRIBUTE
        for child in ast.walk(node)
    )


@lru_cache(maxsize=1)
def _scan_package() -> _PackageScan:
    """Walk every module of the installed package exactly once."""
    root = Path(baldur.__file__).parent
    loads: list[_Load] = []
    allocators: list[_Allocator] = []
    name_constants: list[tuple[str, int]] = []
    modules = 0

    for path in sorted(root.rglob("*.py")):
        module = path.relative_to(root).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        modules += 1

        collector = _IdentityLoadCollector(module)
        collector.visit(tree)
        loads.extend(collector.loads)

        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == _ALLOCATOR:
                allocators.append(
                    _Allocator(
                        module=module,
                        binds_the_repair=_binds_the_repair(_first_statement(node)),
                        reads_the_identity=_touches_identity(node),
                    )
                )
            elif isinstance(node, ast.Constant) and node.value == _IDENTITY_ATTRIBUTE:
                name_constants.append((module, node.lineno))

    return _PackageScan(
        modules=modules,
        loads=tuple(loads),
        allocators=tuple(allocators),
        name_constants=tuple(name_constants),
    )


class TestEntryIdentitySingleReaderContract:
    """The identity has exactly one reader on the allocation path.

    Asserted on the source rather than on behavior: a second reader is a defect
    only when a concurrent swap lands between the two reads, which is neither
    deterministic to reproduce nor exhaustive to enumerate. The walk is both.
    It carries no exclusion list — the attribute name is unique to this class,
    so any read it finds is a read of this identity.
    """

    def test_the_walk_reaches_the_package_and_finds_the_known_readers(self):
        """A scan that reached nothing would pass every assertion below. The
        readers it finds are spelled out rather than compared to the allowed
        set alone, so admitting a new reader by widening that set is a failure
        here as well as a diff — otherwise exempting one would silently retire
        the rule."""
        scan = _scan_package()

        assert scan.modules > 1
        found = {load.member for load in scan.loads}
        assert found == {"_repair_if_forked", "_pod_id", "_pid", "_run_nonce"}
        assert found == _ALLOWED_READERS

    def test_only_the_repair_and_its_views_read_the_identity(self):
        """Any other reader is a second load that can straddle a concurrent
        swap and allocate from an identity nobody installed."""
        offenders = [
            load
            for load in _scan_package().loads
            if load.member not in _ALLOWED_READERS
        ]

        assert offenders == [], (
            "these read the entry identity outside the repair and its "
            f"diagnostic views: {offenders}"
        )

    def test_the_allocator_binds_the_repair_and_reads_nothing_else(self):
        """The allocation must use the object the repair returned. Re-reading
        the attribute is exactly what that return value exists to prevent."""
        allocators = _scan_package().allocators

        assert len(allocators) == 1
        assert allocators[0].binds_the_repair
        assert not allocators[0].reads_the_identity

    def test_no_module_names_the_identity_as_a_string(self):
        """Closes the ``getattr(self, "…")`` / ``vars(self)[…]`` forms, which
        an attribute-node scan cannot see."""
        assert _scan_package().name_constants == ()
