"""G13 — Unguarded imports of optional extras dependencies (related to #460).

CLAUDE.md § Pattern Compliance — optional-import guarding: any third-party
module that ships only through ``pyproject.toml [project.optional-dependencies]``
MUST be imported inside a ``try / except ImportError`` block (or behind
``if TYPE_CHECKING:``) so that clean-venv installs without the extra do not
break at import time.

Optional modules are discovered dynamically via ``optional_extras_modules()``
(D6) — adding a new extras group in pyproject.toml automatically grows the
enforced set, no rule edit needed.

Detection:
- Module-level ``ast.Import`` / ``ast.ImportFrom`` whose top-level module is
  in the optional set.
- Imports under ``ast.Try`` blocks that catch ``ImportError`` are accepted.
- Imports under ``if TYPE_CHECKING:`` blocks are accepted (no runtime cost).

Scope: ``src/baldur/`` only — ``src/baldur_pro/`` extras live in a different
distribution boundary and are gated separately.

Rule registry: ``ARCHITECTURE.md#g13-optional-imports``
"""

from __future__ import annotations

import ast
from pathlib import Path

from tests.architecture.conftest import (
    PROJECT_ROOT,
    collect_violations,
    optional_extras_modules,
    parse_ast,
    walk_src,
)

_RULE_KEY = "optional_imports"
_RULE_ANCHOR = "#g13-optional-imports"

_ALWAYS_ALLOWED = frozenset(
    {
        "baldur",
        "baldur_pro",
    }
)


def _all_optional_top_level_modules() -> frozenset[str]:
    modules: set[str] = set()
    for extra_modules in optional_extras_modules().values():
        modules.update(extra_modules)
    modules.discard("baldur")
    return frozenset(modules)


def _is_importerror_handler(handler: ast.ExceptHandler) -> bool:
    if handler.type is None:
        return False
    if isinstance(handler.type, ast.Name) and handler.type.id == "ImportError":
        return True
    if isinstance(handler.type, ast.Tuple):
        return any(
            isinstance(elt, ast.Name)
            and elt.id in ("ImportError", "ModuleNotFoundError")
            for elt in handler.type.elts
        )
    return False


def _is_type_checking_guard(node: ast.If) -> bool:
    test = node.test
    if isinstance(test, ast.Name) and test.id == "TYPE_CHECKING":
        return True
    if isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING":
        return True
    return False


def _collect_guarded_import_linenos(tree: ast.Module) -> set[int]:
    """Return source line numbers covered by either an ImportError try-block
    or a ``TYPE_CHECKING`` guard.
    """
    guarded: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Try):
            covers_importerror = any(_is_importerror_handler(h) for h in node.handlers)
            if not covers_importerror:
                continue
            for child in ast.walk(node):
                if isinstance(child, (ast.Import, ast.ImportFrom)):
                    guarded.add(child.lineno)
        elif isinstance(node, ast.If) and _is_type_checking_guard(node):
            for child in ast.walk(node):
                if isinstance(child, (ast.Import, ast.ImportFrom)):
                    guarded.add(child.lineno)
    return guarded


def _module_top_level(module_name: str) -> str:
    return module_name.split(".", 1)[0]


def _scan(path: Path, optional_modules: frozenset[str]) -> list[tuple[Path, int, str]]:
    tree = parse_ast(path)
    if tree is None:
        return []
    guarded = _collect_guarded_import_linenos(tree)
    violations: list[tuple[Path, int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module
            if module is None:
                continue
            top = _module_top_level(module)
        elif isinstance(node, ast.Import):
            if not node.names:
                continue
            top = _module_top_level(node.names[0].name)
        else:
            continue
        if top in _ALWAYS_ALLOWED:
            continue
        if top not in optional_modules:
            continue
        if node.lineno in guarded:
            continue
        violations.append(
            (path, node.lineno, f"unguarded import of optional module '{top}'")
        )
    return violations


class TestOptionalImportsContract:
    """G13 — optional extras MUST be imported under try/except ImportError."""

    def test_no_unbaselined_violations(self):
        optional_modules = _all_optional_top_level_modules()
        assert optional_modules, (
            "optional_extras_modules() returned no modules — "
            "pyproject.toml [project.optional-dependencies] is missing or unreadable."
        )

        roots = [PROJECT_ROOT / "src" / "baldur"]
        raw: list[tuple[Path, int | None, str | None, str | None]] = []
        for path in walk_src(roots):
            for offender_path, line, extra in _scan(path, optional_modules):
                raw.append((offender_path, line, None, extra))

        violations = collect_violations(_RULE_KEY, raw, _RULE_ANCHOR)
        assert not violations, (
            f"G13: optional import regressions ({len(violations)}). "
            "Wrap the import in `try/except ImportError` or move it under "
            "`if TYPE_CHECKING:`, or add a baseline entry under "
            "`optional_imports:` with reason+ticket.\n" + "\n".join(violations)
        )
