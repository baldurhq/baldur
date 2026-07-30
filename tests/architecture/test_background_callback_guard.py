"""Architectural fitness function — a function handed to a background thread
MUST NOT let an exception escape its body.

``threading`` gives a callback no error channel. An exception that leaves the
target propagates into the thread bootstrap, which kills that thread and prints
a traceback. For a one-shot thread that loses one action. For a
**self-rescheduling** callback -- one whose last act is to arm the next tick --
it ends the chain for the life of the process: nothing re-arms it, the work
silently stops, and no log line or metric records that it did. Recovery needs a
restart.

This is not hypothetical. ``PrecomputedCacheWorker._do_refresh`` opened with an
unguarded circuit-breaker admission check; on a Redis-backed breaker repository
that check reaches the resilient backend, which writes to the audit WAL while
Redis is degraded, and a WAL that refuses the write raised straight through.
The refresh chain died during exactly the outage the worker exists to ride out,
while the published guide promised it would back off and try again.

The rule is stated on the body rather than on the call site because the escape
is a property of the target: the same function is often reachable both from the
timer and from a synchronous cold-start call.

Accepted shapes for a guarded body:

- the whole body is one ``try`` statement carrying a bare ``except Exception``
  (or ``except BaseException`` / a bare ``except``);
- the body is a docstring, or assignments, followed by such a ``try``;
- a single ``return``/``pass`` body -- nothing can raise from it.

A target defined outside the scanned tree (an imported helper, a bound method
resolved at runtime, a lambda) is not resolvable here and is skipped rather
than guessed at; the sites this rule guards are all module- or class-level
functions in the same file as their scheduling call.

Rule registry:
``ARCHITECTURE.md#g80-background-callback-guard``
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

_RULE_KEY = "background_callback_guard"
_RULE_ANCHOR = "#g80-background-callback-guard"

# Constructors whose callable argument runs on a thread of its own.
_TIMER_CALLABLES = frozenset({"Timer"})
_THREAD_CALLABLES = frozenset({"Thread"})


def _src_roots() -> tuple[Path, ...]:
    """OSS (wherever it is installed from) plus the private PRO tier."""
    return (oss_src_root(), PROJECT_ROOT / "src" / "baldur_pro")


def _callee_name(node: ast.Call) -> str | None:
    """Last path segment of the called name: ``threading.Timer`` -> ``Timer``."""
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _target_expr(node: ast.Call) -> ast.expr | None:
    """The callable a thread/timer construction will run, if it is given.

    ``Timer(interval, function)`` takes it positionally as the second argument;
    ``Thread(target=...)`` takes it by keyword. Both also accept ``target=``.
    """
    name = _callee_name(node)
    if name is None:
        return None
    for keyword in node.keywords:
        if keyword.arg in ("target", "function"):
            return keyword.value
    if name in _TIMER_CALLABLES and len(node.args) >= 2:
        return node.args[1]
    if name in _THREAD_CALLABLES and len(node.args) >= 2:
        return node.args[1]
    return None


def _target_attr_name(expr: ast.expr) -> str | None:
    """``self._do_refresh`` -> ``_do_refresh``; ``_seed`` -> ``_seed``.

    Anything else (a lambda, a subscript, a call result) is unresolvable from
    the AST and is reported as such by returning None.
    """
    if isinstance(expr, ast.Name):
        return expr.id
    if isinstance(expr, ast.Attribute):
        return expr.attr
    return None


def _scheduled_target_names(tree: ast.Module) -> set[str]:
    """Names this module hands to a thread or timer constructor."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        callee = _callee_name(node)
        if callee not in _TIMER_CALLABLES | _THREAD_CALLABLES:
            continue
        target = _target_expr(node)
        if target is None:
            continue
        attr = _target_attr_name(target)
        if attr is not None:
            names.add(attr)
    return names


def _catches_broadly(handler: ast.ExceptHandler) -> bool:
    """True for ``except:``, ``except Exception:`` or ``except BaseException:``."""
    if handler.type is None:
        return True
    candidates = (
        handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    )
    for candidate in candidates:
        name = None
        if isinstance(candidate, ast.Name):
            name = candidate.id
        elif isinstance(candidate, ast.Attribute):
            name = candidate.attr
        if name in ("Exception", "BaseException"):
            return True
    return False


def _body_is_guarded(func: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Whether no exception can leave ``func``'s body.

    Leading docstrings and assignments are allowed before the ``try`` -- the
    real callbacks bind a couple of locals first -- but every statement that
    can raise must sit inside it, so anything after the ``try`` disqualifies
    the body.
    """
    statements = list(func.body)
    if statements and isinstance(statements[0], ast.Expr):
        value = statements[0].value
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            statements = statements[1:]

    if not statements:
        return True
    if len(statements) == 1 and isinstance(statements[0], ast.Pass | ast.Return):
        return True

    prefix, tail = statements[:-1], statements[-1]
    if not isinstance(tail, ast.Try):
        return False
    if not any(_catches_broadly(handler) for handler in tail.handlers):
        return False
    # A prefix statement that can raise defeats the guard just as surely as a
    # suffix one; only inert binding is tolerated ahead of the try.
    return all(
        isinstance(statement, ast.Assign | ast.AnnAssign) for statement in prefix
    )


def _scheduling_functions(tree: ast.Module) -> dict[str, set[str]]:
    """Map each function that constructs a thread/timer to the targets it arms."""
    out: dict[str, set[str]] = {}
    for func in ast.walk(tree):
        if not isinstance(func, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        armed: set[str] = set()
        for node in ast.walk(func):
            if not isinstance(node, ast.Call):
                continue
            if _callee_name(node) not in _TIMER_CALLABLES | _THREAD_CALLABLES:
                continue
            target = _target_expr(node)
            if target is None:
                continue
            attr = _target_attr_name(target)
            if attr is not None:
                armed.add(attr)
        if armed:
            out[func.name] = armed
    return out


def _called_names(func: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    """Names this function calls, by last path segment."""
    names: set[str] = set()
    for node in ast.walk(func):
        if isinstance(node, ast.Call):
            name = _callee_name(node)
            if name is not None:
                names.add(name)
    return names


def _self_rescheduling(tree: ast.Module) -> set[str]:
    """Callbacks that arm their own next tick — directly or via a helper.

    This is the class where an escaping exception is unrecoverable rather than
    merely lossy: the chain has no other source of re-arming, so the work stops
    for the life of the process. A one-shot thread that raises loses one action
    and leaves a traceback, which is ordinary severity and not this rule's
    business.
    """
    schedulers = _scheduling_functions(tree)
    bodies = {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    }

    chained: set[str] = set()
    for name, node in bodies.items():
        # Armed by some function in this module?
        armers = {scheduler for scheduler, armed in schedulers.items() if name in armed}
        if not armers:
            continue
        # Does the callback itself lead back to one of those armers?
        reachable = _called_names(node)
        if name in armers or reachable & armers:
            chained.add(name)
    return chained


def _scan(path: Path) -> list[tuple[Path, int, str, str]]:
    tree = parse_ast(path)
    if tree is None:
        return []

    chained = _self_rescheduling(tree)
    if not chained:
        return []

    violations: list[tuple[Path, int, str, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        if node.name not in chained:
            continue
        if _body_is_guarded(node):
            continue
        violations.append(
            (
                path,
                node.lineno,
                symbol_of(tree, node),
                (
                    f"`{node.name}` re-arms its own timer but can raise out of "
                    "its body, which ends the chain permanently"
                ),
            )
        )
    return violations


class TestBackgroundCallbackGuardArchitecture:
    """A thread callback that raises takes its thread down; a self-rescheduling
    one takes the whole chain down with it."""

    def test_no_unguarded_background_callback(self):
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
            f"Unguarded background callback ({len(violations)}). Wrap the body "
            "in `try` / `except Exception` and log there; if the callback "
            "re-arms itself, re-arm on the failure path too so the chain "
            f"survives. Baseline entries go under `{_RULE_KEY}:` with "
            "reason+ticket.\n" + "\n".join(violations)
        )

    def test_the_known_callbacks_are_actually_scanned(self):
        """Belt-and-suspenders: the rule must keep reaching real callbacks.

        Without this, renaming ``threading.Timer`` usage or moving these
        workers would leave the rule scanning nothing and reporting green.
        """
        guarded = (
            oss_src_root() / "services" / "precomputed_cache" / "worker.py",
            oss_src_root() / "services" / "system_metrics_cache.py",
        )
        for path in guarded:
            assert path.is_file(), f"guarded site moved or renamed: {path}"
            tree = parse_ast(path)
            assert tree is not None
            names = _scheduled_target_names(tree)
            assert names, (
                f"{repo_relative(path)} no longer hands a callable to a thread "
                "or timer. If the scheduling moved, update this fitness "
                "function's anchor."
            )
            bodies = {
                node.name
                for node in ast.walk(tree)
                if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
                and node.name in names
            }
            assert bodies, (
                f"{repo_relative(path)} schedules {sorted(names)} but none of "
                "those bodies resolve in this module — the scan would skip it."
            )
