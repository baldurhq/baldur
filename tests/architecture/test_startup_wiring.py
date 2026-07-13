"""G7 — startup wiring (`setup_*` / `start_*` MUST be invoked).

Per CLAUDE.md § Pattern Compliance — Startup wiring: every public
``setup_*()`` / ``start_*()`` function MUST have a call site in a framework
adapter's startup path. Defined-but-uncalled setup functions are bugs.

Detection (per D5):
1. Walk ``src/baldur/`` + ``src/baldur_pro/`` and collect every module-level
   ``def setup_*(...) | def start_*(...)`` definition. Class methods named
   ``start_*`` are NOT in scope (only module-level ``def``s).
2. Walk the entry-point paths — ``src/baldur/adapters/{django,flask,fastapi}/``
   and ``src/baldur/cli/`` — and resolve ``ast.Call`` references through
   ``from ... import name`` aliases.
3. Any defined name NOT invoked from an entry point is a violation.

Known limitation (documented per D5): dynamic dispatch via
``getattr(module, name)()`` / runtime registry walks / list-of-functions
iteration is NOT detected and produces false negatives; such setup functions
MUST be listed in the rule registry as an exception.

Rule registry: ``ARCHITECTURE.md#g7-startup-wiring``
"""

from __future__ import annotations

import ast
from pathlib import Path

from tests.architecture.conftest import (
    DEFAULT_SRC_ROOTS,
    PROJECT_ROOT,
    collect_violations,
    parse_ast,
    resolve_callsites,
    walk_src,
)

_RULE_KEY = "startup_wiring"
_RULE_ANCHOR = "#g7-startup-wiring"

_ENTRY_POINT_PATHS = (
    "src/baldur/adapters/django",
    "src/baldur/adapters/flask",
    "src/baldur/adapters/fastapi",
    "src/baldur/adapters/celery",
    "src/baldur/cli",
    "src/baldur/bootstrap.py",
    # 615 D2 — the single PRO startup surface. Its static calls to
    # start_metrics_updater() / setup_crisis_multiplier_invalidation() are the
    # gate-visible production call sites. _entry_point_roots() drops this path
    # on an OSS-only checkout where src/baldur_pro/ is absent.
    "src/baldur_pro/startup.py",
)


def _entry_point_roots() -> list[Path]:
    roots: list[Path] = []
    for rel in _ENTRY_POINT_PATHS:
        candidate = PROJECT_ROOT / rel
        if candidate.exists():
            roots.append(candidate)
    return roots


def _collect_module_level_setups(
    tree: ast.Module,
) -> list[tuple[str, int]]:
    """Return module-level ``def setup_*`` / ``def start_*`` definitions."""
    found: list[tuple[str, int]] = []
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        name = node.name
        if name.startswith("setup_") or name.startswith("start_"):
            found.append((name, node.lineno))
    return found


def _scan_definitions() -> dict[str, list[tuple[Path, int]]]:
    """Return ``{function_name: [(file, lineno), ...]}`` for every setup/start def."""
    definitions: dict[str, list[tuple[Path, int]]] = {}
    for path in walk_src(DEFAULT_SRC_ROOTS):
        tree = parse_ast(path)
        if tree is None:
            continue
        for name, lineno in _collect_module_level_setups(tree):
            definitions.setdefault(name, []).append((path, lineno))
    return definitions


class TestStartupWiringContract:
    """G7 — every `setup_*()` / `start_*()` must be invoked from an entry point."""

    def test_no_unbaselined_violations(self):
        definitions = _scan_definitions()
        if not definitions:
            return
        invoked = resolve_callsites(_entry_point_roots(), set(definitions))
        raw: list[tuple[Path, int | None, str | None, str | None]] = []
        for name, sites in definitions.items():
            if name in invoked:
                continue
            # `name` is a module-level def name == qualname — emit as symbol (D5).
            for path, lineno in sites:
                raw.append(
                    (path, lineno, name, f"{name}() never invoked from any entry point")
                )

        violations = collect_violations(_RULE_KEY, raw, _RULE_ANCHOR)
        assert not violations, (
            f"G7: startup wiring regressions ({len(violations)}). "
            "Add a call site in a framework adapter (django/flask/fastapi/cli) "
            "or in baldur.bootstrap; document dynamic dispatch in the rule "
            "registry; or add a baseline entry under `startup_wiring:` "
            "with reason+ticket.\n" + "\n".join(violations)
        )
