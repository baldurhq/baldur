"""G9 — `__all__` declared on every module the published reference reads.

Scope is the set of modules a ``:::`` directive renders WHOLE, resolved by the
shared reference-directive classifier: a whole-package directive (mkdocstrings
reads the ``__all__`` in that package's ``__init__.py``), a plain-module
directive (it reads that file's ``__all__``), and any package the reference
covers leaf-by-leaf (the completeness rule compares its ``__all__`` against the
rendered leaves). A per-symbol directive renders one object and never consults
``__all__``, so the symbol's own module is out of scope.

Two contracts are validated:
    (a) An ``__all__`` assignment exists at module level. Missing it produces
        a violation; a non-literal value (e.g., ``__all__ = [...] + extras``)
        is silently accepted (declaration-only, content unchecked).
    (b) When ``__all__`` is a ``list[str]`` / ``tuple[str, ...]`` literal AND
        the module does not define a PEP 562 ``__getattr__``, every string
        element MUST resolve to a top-level ``ClassDef``, ``FunctionDef``,
        ``AsyncFunctionDef``, top-level ``Assign`` target, or re-imported
        ``ImportFrom`` name. Modules with module-level ``__getattr__`` skip
        (b) because names are resolved at attribute access time.

Why this scope and not every module: ``__all__`` has exactly one mechanical
reader here — mkdocstrings. Nothing else consults it (star-imports have no call
site in the source tree, and no lint rule keys on it), so enforcing it on
modules the reference never renders bought a permanently-baselined majority and
no guarantee. On the rendered set the rule closes a real hole: a package that
loses its ``__all__`` degrades the reference-completeness check to a vacuous
pass, silently dropping symbols from the published API reference.

Baseline is enforced-empty — a rendered module's ``__all__`` is fixed, never
baselined.

Rule registry: ``ARCHITECTURE.md#g9-all-declaration``
"""

from __future__ import annotations

import ast
from pathlib import Path

from tests.architecture.conftest import (
    collect_violations,
    parse_ast,
    reference_read_modules,
)

_RULE_KEY = "all_declaration"
_RULE_ANCHOR = "#g9-all-declaration"

# Non-vacuity anchors. The scope is small and import-resolved, so a broken
# resolver (or a reference page losing its directives) would empty it and turn
# the rule green for the wrong reason. These are OSS-only on purpose: a
# PRO-absent checkout legitimately resolves no ``baldur_pro`` target.
_SANITY_ANCHOR_MODULES: tuple[str, ...] = (
    "baldur",
    "baldur.interfaces",
    "baldur.adapters.django",
)


def _has_module_getattr(tree: ast.Module) -> bool:
    """Detect PEP 562 lazy-loader `__getattr__` at module level."""
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == "__getattr__":
                return True
    return False


def _collect_names(stmts: list[ast.stmt], names: set[str]) -> None:
    for node in stmts:
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
                elif isinstance(target, (ast.Tuple, ast.List)):
                    for elt in target.elts:
                        if isinstance(elt, ast.Name):
                            names.add(elt.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                names.add(alias.asname or alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                names.add((alias.asname or alias.name).split(".")[0])
        elif isinstance(node, ast.Try):
            _collect_names(node.body, names)
            for handler in node.handlers:
                _collect_names(handler.body, names)
            _collect_names(node.orelse, names)
            _collect_names(node.finalbody, names)
        elif isinstance(node, ast.If):
            _collect_names(node.body, names)
            _collect_names(node.orelse, names)


def _module_top_level_names(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    _collect_names(list(tree.body), names)
    return names


def _find_all_assignment(tree: ast.Module) -> ast.Assign | ast.AnnAssign | None:
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "__all__":
                    return node
        elif isinstance(node, ast.AnnAssign):
            target = node.target
            if isinstance(target, ast.Name) and target.id == "__all__":
                return node
    return None


def _audit_module(path: Path) -> tuple[int | None, str] | None:
    tree = parse_ast(path)
    if tree is None:
        return None
    assignment = _find_all_assignment(tree)
    if assignment is None:
        return (None, "missing __all__ declaration")

    value = assignment.value
    if value is None:
        return (assignment.lineno, "__all__ has no value")

    if not isinstance(value, (ast.List, ast.Tuple)):
        return None

    declared: list[str] = []
    for element in value.elts:
        if isinstance(element, ast.Constant) and isinstance(element.value, str):
            declared.append(element.value)

    if _has_module_getattr(tree):
        return None

    top_level = _module_top_level_names(tree)
    missing = [name for name in declared if name not in top_level]
    if missing:
        return (
            assignment.lineno,
            f"__all__ references undefined names: {sorted(missing)}",
        )
    return None


class TestAllDeclarationContract:
    """G9 — reference-rendered modules MUST declare and populate `__all__`."""

    def test_scope_is_not_vacuous(self):
        rendered = reference_read_modules()
        assert rendered, (
            "G9: the reference-rendered module set is empty — the directive "
            "resolver or the reference pages regressed, and the rule below "
            "would pass vacuously."
        )
        resolved = set(rendered.values())
        missing = [name for name in _SANITY_ANCHOR_MODULES if name not in resolved]
        assert not missing, (
            f"G9: reference-rendered set lost known anchors {missing}. "
            "Either the reference dropped their directives or the resolver "
            "stopped classifying them as rendered modules."
        )

    def test_no_unbaselined_violations(self):
        raw: list[tuple[Path, int | None, str | None, str | None]] = []
        for path in sorted(reference_read_modules()):
            result = _audit_module(path)
            if result is None:
                continue
            line, extra = result
            raw.append((path, line, None, extra))

        violations = collect_violations(_RULE_KEY, raw, _RULE_ANCHOR)
        assert not violations, (
            f"G9: __all__ declaration regressions ({len(violations)}). "
            "The published reference renders these modules whole and reads "
            "their __all__ — declare it in the offending module.\n"
            + "\n".join(violations)
        )
