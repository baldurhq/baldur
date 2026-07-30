"""Architectural fitness function — storing a freshly armed timer over a slot
that may already hold one MUST cancel the old one first.

A ``threading.Timer`` is live from ``start()``. Overwriting the attribute that
holds it does not stop it: the old timer keeps its own reference through the
threading machinery and fires on schedule, while the only handle the owner had
is gone. Two consequences, both silent:

- **The work doubles.** A self-rescheduling chain whose slot was overwritten
  once now has two chains ticking, each re-arming itself. Output stays correct,
  so nothing observable says anything is wrong -- the process simply does twice
  the work forever.
- **``stop()`` stops nothing.** Cancellation can only reach the slot, so the
  orphaned chain outlives every attempt to end it and dies with the process.

``SystemMetricsCache`` shipped exactly this: ``start()`` ran the cold-start
refresh, which armed the next tick on its way out, and then armed again. A
single ``start()`` left two live daemon timers, sampled psutil at twice the
configured interval, and left one chain permanently beyond ``stop()``.

The rule fires on a store into ``self.<attr>`` or ``self.<attr>[key]`` whose
value is a timer/thread construction -- directly, or through a local name bound
to one in the same function. It is satisfied by a cancellation of that same
slot earlier in the function: a ``.cancel()``/``.join()`` on it, or a call to a
same-object helper whose name says it cancels or stops.

Not in scope: the first assignment in ``__init__`` (the slot starts empty, and
``None`` is not a construction), and any slot whose value the AST cannot
resolve to a timer construction.

Rule registry:
``ARCHITECTURE.md#g81-timer-rearm-cancels``
"""

from __future__ import annotations

import ast
from pathlib import Path

from tests.architecture._helpers import PROJECT_ROOT, oss_src_root, repo_relative
from tests.architecture.conftest import (
    collect_violations,
    parse_ast,
    symbol_of,
    walk_src,
)

_RULE_KEY = "timer_rearm_cancels"
_RULE_ANCHOR = "#g81-timer-rearm-cancels"

# Timer only, and the exclusion is the rule. A pending Timer is cancellable
# and invisible once its slot is overwritten. A Thread has no cancel(), is
# already running, and its slot is normally re-entrancy-guarded by a
# `_running` flag -- storing over a finished worker is ordinary.
_SCHEDULING_CALLABLES = frozenset({"Timer"})
_RELEASING_CALLS = frozenset({"cancel", "join"})


def _src_roots() -> tuple[Path, ...]:
    """OSS (wherever it is installed from) plus the private PRO tier."""
    return (oss_src_root(), PROJECT_ROOT / "src" / "baldur_pro")


def _callee_name(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _is_construction(node: ast.expr) -> bool:
    """``threading.Timer(...)`` — bare or dotted."""
    return isinstance(node, ast.Call) and _callee_name(node) in _SCHEDULING_CALLABLES


def _locally_armed_names(func: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    """Local names bound to a timer/thread construction inside ``func``."""
    names: set[str] = set()
    for node in ast.walk(func):
        if not isinstance(node, ast.Assign):
            continue
        if not _is_construction(node.value):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name):
                names.add(target.id)
    return names


def _slot_of(target: ast.expr) -> str | None:
    """Name the storage slot a store writes to, if it is owned by ``self``.

    ``self._timer`` -> ``_timer``; ``self._phase_timers[key]`` ->
    ``_phase_timers``. A store to anything else is out of scope.
    """
    if isinstance(target, ast.Subscript):
        target = target.value
    if not isinstance(target, ast.Attribute):
        return None
    if not isinstance(target.value, ast.Name) or target.value.id != "self":
        return None
    return target.attr


def _names_read_from_slot(
    func: ast.FunctionDef | ast.AsyncFunctionDef, slot: str
) -> set[str]:
    """Locals bound from the slot, so a cancel on them counts as a release.

    The common idiom fetches the incumbent before replacing it --
    ``existing = self._timers.get(key)`` then ``existing.cancel()`` -- so a rule
    that only recognises ``self._timers[key].cancel()`` would call correct code
    a violation.
    """
    names: set[str] = set()
    for node in ast.walk(func):
        if not isinstance(node, ast.Assign):
            continue
        source = node.value
        if isinstance(source, ast.Call):
            source = source.func
        if isinstance(source, ast.Attribute) and source.attr in ("get", "pop"):
            source = source.value
        if isinstance(source, ast.Subscript):
            source = source.value
        if not isinstance(source, ast.Attribute):
            continue
        if not isinstance(source.value, ast.Name) or source.value.id != "self":
            continue
        if source.attr != slot:
            continue
        for target in node.targets:
            if isinstance(target, ast.Name):
                names.add(target.id)
    return names


def _releases_slot(
    node: ast.AST, slot: str, aliases: frozenset[str] = frozenset()
) -> bool:
    """Whether ``node`` cancels/stops the given slot, or delegates that."""
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if not isinstance(func, ast.Attribute):
        return False

    if func.attr in _RELEASING_CALLS:
        # self._timer.cancel() / self._timers[key].cancel()
        owner = func.value
        if isinstance(owner, ast.Subscript):
            owner = owner.value
        if (
            isinstance(owner, ast.Attribute)
            and isinstance(owner.value, ast.Name)
            and owner.value.id == "self"
            and owner.attr == slot
        ):
            return True

    # A cancel on a local fetched from the slot.
    if func.attr in _RELEASING_CALLS:
        owner = func.value
        if isinstance(owner, ast.Name) and owner.id in aliases:
            return True

    # A delegated release: self._cancel_timer(...), self._stop_worker(...).
    if isinstance(func.value, ast.Name) and func.value.id == "self":
        lowered = func.attr.lower()
        if "cancel" in lowered or "stop" in lowered:
            return True
    return False


def _scan(path: Path) -> list[tuple[Path, int, str, str]]:
    tree = parse_ast(path)
    if tree is None:
        return []

    violations: list[tuple[Path, int, str, str]] = []
    for func in ast.walk(tree):
        if not isinstance(func, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        armed_locals = _locally_armed_names(func)

        for node in ast.walk(func):
            if not isinstance(node, ast.Assign):
                continue
            value = node.value
            stores_timer = _is_construction(value) or (
                isinstance(value, ast.Name) and value.id in armed_locals
            )
            if not stores_timer:
                continue
            for target in node.targets:
                slot = _slot_of(target)
                if slot is None:
                    continue
                aliases = frozenset(_names_read_from_slot(func, slot))
                released = any(
                    _releases_slot(other, slot, aliases)
                    for other in ast.walk(func)
                    if getattr(other, "lineno", 0) < node.lineno
                )
                if released:
                    continue
                violations.append(
                    (
                        path,
                        node.lineno,
                        symbol_of(tree, node),
                        (
                            f"`self.{slot}` is re-armed without cancelling what "
                            "it already held"
                        ),
                    )
                )
    return violations


class TestTimerRearmCancelsArchitecture:
    """A timer slot may hold at most one live timer — overwriting is not
    stopping."""

    def test_no_rearm_without_cancel(self):
        raw: list[tuple[Path, int | None, str | None, str | None]] = []
        for path in walk_src(_src_roots()):
            for offender_path, line, symbol, extra in _scan(path):
                # Normalize to the repo-relative ``src/<pkg>/...`` form BEFORE
                # baselining: OSS is an installed sibling here, so its absolute
                # path lies outside PROJECT_ROOT and would key the baseline on
                # this machine's checkout layout.
                raw.append(
                    (PROJECT_ROOT / repo_relative(offender_path), line, symbol, extra)
                )

        violations = collect_violations(_RULE_KEY, raw, _RULE_ANCHOR)
        assert not violations, (
            f"Timer re-armed without cancelling ({len(violations)}). Cancel the "
            "slot before storing the new timer, or route the arming through a "
            "helper that cancels first. Baseline entries go under "
            f"`{_RULE_KEY}:` with reason+ticket.\n" + "\n".join(violations)
        )

    def test_the_known_arming_sites_are_actually_scanned(self):
        """Belt-and-suspenders: the rule must keep reaching a real arming site,
        so a move or rename cannot leave it scanning nothing."""
        path = oss_src_root() / "services" / "system_metrics_cache.py"
        assert path.is_file(), f"arming site moved or renamed: {path}"
        tree = parse_ast(path)
        assert tree is not None
        armed = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Assign)
            and _is_construction(node.value)
            and any(_slot_of(target) is not None for target in node.targets)
        ]
        assert armed, (
            f"{repo_relative(path)} no longer stores a constructed timer on "
            "self. If the arming moved, update this fitness function's anchor."
        )
