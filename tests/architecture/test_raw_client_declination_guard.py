"""Architectural fitness function — a raw Redis dial MUST be declined, not just
attempted, when the backend says the dial is not worth making.

The resilient storage backend answers every operation it owns from memory + WAL
when Redis is unreachable: no exception, no log line, and a probe cooldown that
bounds the connect rate. A repository that reaches past it for the raw client
gets none of that. Each such call opens its own connection, fails on its own,
and reports on its own — which is how a zero-config first run came to log a
Redis WARNING every background refresh, and how three more write lanes were
found doing the same thing under failing traffic.

The rule is therefore about the **bypass**, not about a module or a log level:
a path that reaches the client directly must first ask the backend whether to
dial at all. Three admitted forms, deliberately not ranked and not
interchangeable:

- an ``is_degraded`` pre-check — for a path that owns a local fallback;
- an ``ensure_redis()`` admission — the blocking form, for a path that would
  rather pay one bounded probe than skip;
- the unreached-unconfigured-default predicate — for a path whose only fallback
  lives in its caller, which declines only when the backend has never reached a
  Redis and nobody named one.

The third is why this is NOT stated as "an availability check". With a Redis
someone named down, availability is False and the dial still goes through on
purpose: a configured store's outage keeps its loud, counted failure. What the
rule forbids is a dial issued with no guard of any kind.

**Population — the dial, not the fetch.** A command issued on a value obtained
from ``raw_redis_client`` (directly, or through a local bound to it in the same
function). Fetching the client and returning it issues nothing, so an accessor
property is out of scope by construction. Derived by AST walk over
``adapters/redis/**``, never authored, so a Lua method added tomorrow inherits
the obligation instead of re-litigating it.

Adapters under ``adapters/redis/`` that hold their own client and issue commands
on it are outside the population by construction — they have no resilient
backend to ask. Whether those lanes want a guard of their own is a separate
question this rule does not answer.

**Where the guard may live.** In the dialing function itself, naming the same
receiver as the client access — or, for a helper, in every one of its
module-local callers. The second form is what the query lanes actually use: a
public entry point gates on ``is_degraded`` once and then delegates through two
or three private helpers, each holding the raw client. Requiring the guard in
the leaf would have called correct code a violation; ignoring callers entirely
would have made the rule unenforceable there. An inherited guard is
receiver-agnostic (a collaborator has a different name one hop up); a
same-function guard is not.

Baseline is enforced-empty — no ``baseline.yaml`` key. An unguarded dial is
fixed at the offending commit.

Rule registry:
``ARCHITECTURE.md#g85-raw-client-declination-guard``
"""

from __future__ import annotations

import ast
from pathlib import Path

from tests.architecture._helpers import format_violation, oss_src_root, repo_relative
from tests.architecture.conftest import parse_ast, walk_src

_RULE_ANCHOR = "#g85-raw-client-declination-guard"

# The ``ResilientStorageBackend`` seam, plus the private property one repository
# wraps it in. Both name the same object; a scan that knew only the public
# spelling would miss every lane that fetches through the wrapper.
_RAW_CLIENT_ATTRS = frozenset({"raw_redis_client", "_raw_redis_client"})

# The two readings that decide whether a dial is worth making. ``is_degraded``
# is "reachable right now"; ``has_reached_redis`` is "has this address ever
# answered" — a helper consulting either one is deciding, not describing.
_GUARD_ATTRS = frozenset({"is_degraded", "has_reached_redis"})
_GUARD_CALLS = frozenset({"ensure_redis"})

_FuncDef = ast.FunctionDef | ast.AsyncFunctionDef


def _dotted(node: ast.expr) -> str | None:
    """Render a ``self._backend``-shaped receiver as a dotted string.

    Anything that is not a chain of plain attribute loads rooted at a name is
    unrepresentable here, and an unrepresentable receiver never matches a
    guard — the failing direction is reporting a violation, not hiding one.
    """
    parts: list[str] = []
    current: ast.expr = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if not isinstance(current, ast.Name):
        return None
    parts.append(current.id)
    return ".".join(reversed(parts))


def _raw_client_receiver(node: ast.expr) -> str | None:
    """The backend a ``<recv>.raw_redis_client`` access reads from."""
    if isinstance(node, ast.Attribute) and node.attr in _RAW_CLIENT_ATTRS:
        return _dotted(node.value)
    return None


def _getattr_target(node: ast.expr) -> tuple[str, str] | None:
    """Decompose ``getattr(obj, "name", default)`` into ``(receiver, name)``."""
    if not isinstance(node, ast.Call):
        return None
    if not (isinstance(node.func, ast.Name) and node.func.id == "getattr"):
        return None
    if len(node.args) < 2 or not isinstance(node.args[1], ast.Constant):
        return None
    attr = node.args[1].value
    if not isinstance(attr, str):
        return None
    receiver = _dotted(node.args[0])
    return (receiver, attr) if receiver else None


def _guarded_receivers(func: _FuncDef) -> set[str]:
    """Receivers this function reads a declination verdict from."""
    receivers: set[str] = set()
    for node in ast.walk(func):
        if isinstance(node, ast.Attribute) and node.attr in _GUARD_ATTRS:
            receiver = _dotted(node.value)
            if receiver:
                receivers.add(receiver)
        elif isinstance(node, ast.Call):
            probed = _getattr_target(node)
            if probed and probed[1] in _GUARD_ATTRS:
                receivers.add(probed[0])
            elif (
                isinstance(node.func, ast.Attribute) and node.func.attr in _GUARD_CALLS
            ):
                receiver = _dotted(node.func.value)
                if receiver:
                    receivers.add(receiver)
    return receivers


def collect_declination_helpers(tree: ast.Module) -> dict[str, frozenset[str]]:
    """Methods that themselves consult a declination verdict, by name.

    Derived, not authored: a method qualifies only because its body reads an
    availability or reach verdict off some receiver, so a helper that merely
    reports the *posture* (is this address the shipped default?) does not
    become a guard by being named like one. The mapped receivers are what a
    ``self.<helper>()`` call stands in for at a call site.
    """
    helpers: dict[str, frozenset[str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, _FuncDef):
            continue
        receivers = _guarded_receivers(node)
        if receivers:
            helpers[node.name] = frozenset(receivers)
    return helpers


def _guard_lines(func: _FuncDef, helpers: dict[str, frozenset[str]]) -> dict[str, int]:
    """Earliest line at which each receiver is guarded inside ``func``.

    A delegated guard — ``self._declining_unreached_default()`` — is credited
    to the receivers the delegate itself reads, which is how a guard on
    ``self`` covers a client fetched from ``self._backend`` without the rule
    having to accept "any call on self" as a guard.
    """
    earliest: dict[str, int] = {}

    def _mark(receiver: str, line: int) -> None:
        if receiver not in earliest or line < earliest[receiver]:
            earliest[receiver] = line

    for node in ast.walk(func):
        if isinstance(node, ast.Attribute) and node.attr in _GUARD_ATTRS:
            receiver = _dotted(node.value)
            if receiver:
                _mark(receiver, node.lineno)
            continue
        if not isinstance(node, ast.Call):
            continue
        probed = _getattr_target(node)
        if probed and probed[1] in _GUARD_ATTRS:
            _mark(probed[0], node.lineno)
            continue
        if not isinstance(node.func, ast.Attribute):
            continue
        receiver = _dotted(node.func.value)
        if receiver is None:
            continue
        if node.func.attr in _GUARD_CALLS:
            _mark(receiver, node.lineno)
        elif node.func.attr in helpers:
            # ``<recv>._ensure_redis_available()`` guards ``<recv>`` itself;
            # ``self._declining_unreached_default()`` guards whatever the
            # delegate reads.
            _mark(receiver, node.lineno)
            for delegated in helpers[node.func.attr]:
                _mark(delegated, node.lineno)
    return earliest


def _dials(func: _FuncDef) -> list[tuple[int, str]]:
    """Commands issued on a raw client inside ``func``, as ``(line, receiver)``.

    Two shapes: the chained form
    (``self._backend.raw_redis_client.scan(...)``) and the far more common
    fetch-then-call, where the client is bound to a local first. A bare fetch
    with no command issued on it is not a dial — that is the accessor shape,
    and it opens no connection.
    """
    bound: dict[str, str] = {}
    for node in ast.walk(func):
        if not isinstance(node, ast.Assign):
            continue
        receiver = _raw_client_receiver(node.value)
        if receiver is None:
            continue
        for target in node.targets:
            if isinstance(target, ast.Name):
                bound[target.id] = receiver

    # A conditional fetch (``None if declined else backend.raw_redis_client``)
    # binds the same local, and the client half is the only branch that dials.
    for node in ast.walk(func):
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.IfExp):
            continue
        for branch in (node.value.body, node.value.orelse):
            receiver = _raw_client_receiver(branch)
            if receiver is None:
                continue
            for target in node.targets:
                if isinstance(target, ast.Name):
                    bound[target.id] = receiver

    dials: list[tuple[int, str]] = []
    for node in ast.walk(func):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        owner = node.func.value
        chained = _raw_client_receiver(owner)
        if chained is not None:
            dials.append((node.lineno, chained))
        elif isinstance(owner, ast.Name) and owner.id in bound:
            dials.append((node.lineno, bound[owner.id]))
    return dials


def _self_calls(func: _FuncDef) -> set[str]:
    """Names of same-object methods this function invokes."""
    called: set[str] = set()
    for node in ast.walk(func):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if isinstance(node.func.value, ast.Name) and node.func.value.id == "self":
            called.add(node.func.attr)
    return called


def _admitted_functions(
    functions: dict[str, _FuncDef], helpers: dict[str, frozenset[str]]
) -> set[str]:
    """Functions reached only through a guard, by module-local call graph.

    A function is admitted when it guards for itself, or when it has at least
    one module-local caller and *every* one of them is admitted. Monotone, so
    the fixpoint is order-independent; a call cycle with no guarded entry stays
    unadmitted, which is the conservative direction.
    """
    callers: dict[str, set[str]] = {name: set() for name in functions}
    for name, func in functions.items():
        for callee in _self_calls(func):
            if callee in callers:
                callers[callee].add(name)

    admitted = {name for name, func in functions.items() if _guarded_receivers(func)}
    changed = True
    while changed:
        changed = False
        for name in functions:
            if name in admitted:
                continue
            reached_by = callers[name]
            if reached_by and reached_by <= admitted:
                admitted.add(name)
                changed = True
    return admitted


def scan_module(
    tree: ast.Module, helpers: dict[str, frozenset[str]]
) -> list[tuple[int, str, str]]:
    """Unguarded raw-client dials in one module, as ``(line, symbol, detail)``."""
    functions: dict[str, _FuncDef] = {
        node.name: node for node in ast.walk(tree) if isinstance(node, _FuncDef)
    }
    admitted = _admitted_functions(functions, helpers)

    violations: list[tuple[int, str, str]] = []
    for name, func in functions.items():
        dials = _dials(func)
        if not dials:
            continue
        guards = _guard_lines(func, helpers)
        inherited = name in admitted and not _guarded_receivers(func)
        for line, receiver in dials:
            if receiver in guards and guards[receiver] < line:
                continue
            if inherited:
                continue
            violations.append(
                (
                    line,
                    name,
                    f"`{receiver}` is dialed with no declination guard on it",
                )
            )
    return violations


def _redis_adapter_root() -> Path:
    return oss_src_root() / "adapters" / "redis"


def _scan_tree() -> tuple[list[str], int]:
    """Violations plus the derived population size, over the redis adapters."""
    paths = sorted(walk_src((_redis_adapter_root(),)))
    trees: dict[Path, ast.Module] = {}
    helpers: dict[str, frozenset[str]] = {}
    for path in paths:
        tree = parse_ast(path)
        if tree is None:
            continue
        trees[path] = tree
        helpers.update(collect_declination_helpers(tree))

    violations: list[str] = []
    population = 0
    for path, tree in trees.items():
        for node in ast.walk(tree):
            if isinstance(node, _FuncDef):
                population += len(_dials(node))
        for line, _symbol, detail in scan_module(tree, helpers):
            violations.append(
                format_violation(_RULE_ANCHOR, repo_relative(path), line, detail)
            )
    violations.sort()
    return violations, population


class TestRawClientDeclinationGuardArchitecture:
    """Every raw-client dial is admitted by a guard — or it is a violation."""

    def test_no_unguarded_raw_client_dial(self):
        violations, _population = _scan_tree()
        assert not violations, (
            f"Raw Redis dial with no declination guard ({len(violations)}). Ask the "
            "backend before reaching past it: an `is_degraded` pre-check when the "
            "path owns a local fallback, an `ensure_redis()` admission when it "
            "would rather probe, or the unreached-unconfigured-default predicate "
            "when the fallback lives in the caller. Enforced-empty — there is no "
            "baseline key.\n" + "\n".join(violations)
        )

    def test_the_population_is_not_empty(self):
        """Anti-vacuous guard: a rule that reaches nothing reports nothing."""
        _violations, population = _scan_tree()
        assert population >= 8, (
            "the raw-client dial population collapsed to "
            f"{population}. If the seam moved or was renamed, follow it here — "
            "this rule scanning nothing looks exactly like this rule passing."
        )

    def test_the_circuit_breaker_write_lanes_are_reached(self):
        """The lanes this rule exists for must stay inside its population."""
        path = _redis_adapter_root() / "circuit_breaker.py"
        assert path.is_file(), f"the CB state repository moved or was renamed: {path}"
        tree = parse_ast(path)
        assert tree is not None
        dialing = {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, _FuncDef) and _dials(node)
        }
        for lane in (
            "_write_unless_pinned",
            "try_acquire_half_open_slot",
            "record_success_with_close_check",
            "record_failure_with_open_check",
            "trip_to_open",
        ):
            assert lane in dialing, (
                f"`{lane}` no longer reads as a raw-client dial. Either it stopped "
                "reaching past the backend (delete this name) or the rule stopped "
                "recognising the shape (fix the scan)."
            )


class TestRawClientDeclinationGuardModel:
    """Fixtures for each drift class — the anti-silent-pass guard.

    Every one is a source the real tree does not contain, so the scan's verdict
    on it is the only evidence that the rule can still tell these shapes apart.
    """

    @staticmethod
    def _scan(source: str) -> list[tuple[int, str, str]]:
        tree = ast.parse(source)
        return scan_module(tree, collect_declination_helpers(tree))

    def test_an_unguarded_dial_is_flagged(self):
        found = self._scan(
            "class R:\n"
            "    def write(self):\n"
            "        client = self._backend.raw_redis_client\n"
            "        return client.eval('x')\n"
        )
        assert len(found) == 1
        assert "self._backend" in found[0][2]

    def test_a_matching_guard_admits_the_dial(self):
        assert not self._scan(
            "class R:\n"
            "    def write(self):\n"
            "        if self._backend.is_degraded:\n"
            "            return None\n"
            "        client = self._backend.raw_redis_client\n"
            "        return client.eval('x')\n"
        )

    def test_a_guard_on_a_different_backend_does_not_admit_the_dial(self):
        """The receiver is load-bearing: guarding one collaborator says nothing
        about another."""
        found = self._scan(
            "class R:\n"
            "    def write(self):\n"
            "        if self._other.is_degraded:\n"
            "            return None\n"
            "        client = self._backend.raw_redis_client\n"
            "        return client.eval('x')\n"
        )
        assert len(found) == 1

    def test_a_guard_below_the_dial_does_not_admit_it(self):
        found = self._scan(
            "class R:\n"
            "    def write(self):\n"
            "        client = self._backend.raw_redis_client\n"
            "        client.eval('x')\n"
            "        return self._backend.is_degraded\n"
        )
        assert len(found) == 1

    def test_a_delegated_guard_admits_the_dial_it_covers(self):
        assert not self._scan(
            "class R:\n"
            "    def _declining(self):\n"
            "        return getattr(self._backend, 'has_reached_redis', True) is False\n"
            "    def write(self):\n"
            "        if self._declining():\n"
            "            raise RuntimeError\n"
            "        client = self._backend.raw_redis_client\n"
            "        return client.eval('x')\n"
        )

    def test_a_posture_only_helper_is_not_a_guard(self):
        """A helper that only asks *whose* address this is decides nothing about
        whether to dial — the distinction the rule turns on."""
        found = self._scan(
            "class R:\n"
            "    def _unconfigured(self):\n"
            "        return probing_unconfigured_default(self._backend)\n"
            "    def write(self):\n"
            "        if self._unconfigured():\n"
            "            raise RuntimeError\n"
            "        client = self._backend.raw_redis_client\n"
            "        return client.eval('x')\n"
        )
        assert len(found) == 1

    def test_a_helper_reached_only_through_a_guarded_caller_is_admitted(self):
        assert not self._scan(
            "class R:\n"
            "    def read(self):\n"
            "        if self._backend.is_degraded:\n"
            "            return []\n"
            "        return self._scan_keys()\n"
            "    def _scan_keys(self):\n"
            "        return self._backend.raw_redis_client.scan(0)\n"
        )

    def test_one_unguarded_caller_reddens_the_whole_helper(self):
        found = self._scan(
            "class R:\n"
            "    def read(self):\n"
            "        if self._backend.is_degraded:\n"
            "            return []\n"
            "        return self._scan_keys()\n"
            "    def sweep(self):\n"
            "        return self._scan_keys()\n"
            "    def _scan_keys(self):\n"
            "        return self._backend.raw_redis_client.scan(0)\n"
        )
        assert len(found) == 1

    def test_a_fetch_without_a_command_is_not_a_dial(self):
        """The accessor shape — it opens no connection, so it carries no
        obligation."""
        assert not self._scan(
            "class R:\n"
            "    @property\n"
            "    def _raw_redis_client(self):\n"
            "        return getattr(self._backend, 'raw_redis_client', None)\n"
        )

    def test_a_none_check_on_the_fetched_client_is_not_a_guard(self):
        """A stale client is not None — checking for None declines nothing in
        the posture this rule exists for."""
        found = self._scan(
            "class R:\n"
            "    def write(self):\n"
            "        client = self._backend.raw_redis_client\n"
            "        if client is None:\n"
            "            return None\n"
            "        return client.eval('x')\n"
        )
        assert len(found) == 1
