"""G9 — `__all__` explicitly declared on every public module.

Per D7, the scope is every `_`-prefix-free `.py` module under `src/baldur/` +
`src/baldur_pro/`, excluding `__init__.py` (re-export hubs have a separate
convention).

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

Rule registry: ``ARCHITECTURE.md#g9-all-declaration``
"""

from __future__ import annotations

import ast
from pathlib import Path

from tests.architecture.conftest import (
    DEFAULT_SRC_ROOTS,
    collect_violations,
    parse_ast,
    walk_src,
)

_RULE_KEY = "all_declaration"
_RULE_ANCHOR = "#g9-all-declaration"


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

    value = assignment.value if isinstance(assignment, ast.Assign) else assignment.value
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
    """G9 — public modules MUST declare and correctly populate `__all__`."""

    def test_no_unbaselined_violations(self):
        raw: list[tuple[Path, int | None, str | None, str | None]] = []
        modules = walk_src(
            DEFAULT_SRC_ROOTS,
            exclude_underscore=True,
            exclude_init=True,
        )
        for path in modules:
            result = _audit_module(path)
            if result is None:
                continue
            line, extra = result
            raw.append((path, line, None, extra))

        violations = collect_violations(_RULE_KEY, raw, _RULE_ANCHOR)
        assert not violations, (
            f"G9: __all__ declaration regressions ({len(violations)}). "
            "Either declare __all__ in the offending module or add a baseline entry "
            "under `all_declaration:` with reason+ticket.\n" + "\n".join(violations)
        )
