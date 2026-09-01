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

import pytest

from tests.architecture.conftest import (
    PROJECT_ROOT,
    collect_violations,
    parse_ast,
    resolve_callsites,
    src_root_params,
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


def _installed_baldur_root() -> Path | None:
    """Directory of the installed ``baldur`` package, or None.

    The private repo consumes the OSS core as a sibling editable install, so
    ``PROJECT_ROOT/src/baldur`` does not exist there. Without this fallback the
    OSS entry points (``bootstrap.py``, the framework adapters, the CLI) are
    all absent from the scan while ``src/baldur_pro`` is still walked for
    definitions — so every PRO setup function invoked from ``bootstrap.py`` was
    reported as never-invoked, a verdict the gate had no basis for. Two such
    false violations sat baselined as "wiring gap" while both were correctly
    wired all along.
    """
    import importlib.util

    spec = importlib.util.find_spec("baldur")
    if spec is None or not spec.origin:
        return None
    return Path(spec.origin).resolve().parent


def _entry_point_roots() -> list[Path]:
    roots: list[Path] = []
    installed = _installed_baldur_root()
    for rel in _ENTRY_POINT_PATHS:
        candidate = PROJECT_ROOT / rel
        if candidate.exists():
            roots.append(candidate)
            continue
        # In-tree path absent: resolve it inside the installed package instead.
        if installed is not None and rel.startswith("src/baldur/"):
            fallback = installed / rel[len("src/baldur/") :]
            if fallback.exists():
                roots.append(fallback)
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


def _scan_definitions(roots) -> dict[str, list[tuple[Path, int]]]:
    """Return ``{function_name: [(file, lineno), ...]}`` for every setup/start def."""
    definitions: dict[str, list[tuple[Path, int]]] = {}
    for path in walk_src(roots):
        tree = parse_ast(path)
        if tree is None:
            continue
        for name, lineno in _collect_module_level_setups(tree):
            definitions.setdefault(name, []).append((path, lineno))
    return definitions


class TestStartupWiringContract:
    """G7 — every `setup_*()` / `start_*()` must be invoked from an entry point."""

    @pytest.mark.parametrize("root", src_root_params())
    def test_no_unbaselined_violations(self, root):
        definitions = _scan_definitions([root])
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

    def test_the_oss_entry_points_are_reachable(self):
        """Guard-of-the-guard: an unreachable entry-point set turns every
        definition into a false violation, which is how two correctly-wired PRO
        setup functions ended up baselined as wiring gaps."""
        roots = _entry_point_roots()
        assert roots, "no entry-point root resolved — every definition would be flagged"

        names = {r.name for r in roots}
        assert "bootstrap.py" in names, (
            "baldur.bootstrap is not in the resolved entry-point set; PRO setup "
            "functions invoked from it would be reported as never-invoked"
        )
