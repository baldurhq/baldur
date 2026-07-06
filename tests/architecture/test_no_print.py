"""G14 — No `print()` in business-logic directories.

Per D11, raw ``print()`` bypasses structured fields, the event-name
convention, and log-level enforcement; CLAUDE.md § Logging & Observability
Standards mandates ``structlog.get_logger()``.

Scope (business-logic only):
    ``src/baldur/{services,adapters,core,resilience,coordination,
    multiregion,scaling,metrics,audit}/**`` and the ``baldur_pro`` mirrors.

Exempt:
- ``src/baldur/cli/**`` — terminal output is the contract there.
- ``scripts/**``, ``tests/**`` — operator tooling and test code.
- ``if __name__ == "__main__":`` script blocks — invoked as a CLI surface.

Hardcoded secret / token detection is intentionally NOT bundled here — Bandit
already covers that responsibility via ``.github/workflows/security.yml``.

Rule registry: ``ARCHITECTURE.md#g14-no-print``
"""

from __future__ import annotations

import ast
from pathlib import Path

from tests.architecture.conftest import (
    PROJECT_ROOT,
    collect_violations,
    parse_ast,
    symbol_of,
    walk_src,
)

_RULE_KEY = "no_print"
_RULE_ANCHOR = "#g14-no-print"

_BUSINESS_LOGIC_SUBDIRS = (
    "services",
    "adapters",
    "core",
    "resilience",
    "coordination",
    "multiregion",
    "scaling",
    "metrics",
    "audit",
)


def _business_logic_roots() -> list[Path]:
    roots: list[Path] = []
    for parent in (
        PROJECT_ROOT / "src" / "baldur",
        PROJECT_ROOT / "src" / "baldur_pro",
    ):
        if not parent.exists():
            continue
        for subdir in _BUSINESS_LOGIC_SUBDIRS:
            candidate = parent / subdir
            if candidate.exists():
                roots.append(candidate)
    return roots


def _is_dunder_main_guard(node: ast.If) -> bool:
    test = node.test
    if not isinstance(test, ast.Compare):
        return False
    if not (isinstance(test.left, ast.Name) and test.left.id == "__name__"):
        return False
    for comparator in test.comparators:
        if isinstance(comparator, ast.Constant) and comparator.value == "__main__":
            return True
    return False


def _collect_main_guard_lines(tree: ast.Module) -> set[int]:
    """Return the set of source lines inside ``if __name__ == "__main__":`` blocks."""
    exempt_lines: set[int] = set()
    for node in tree.body:
        if isinstance(node, ast.If) and _is_dunder_main_guard(node):
            for child in ast.walk(node):
                if hasattr(child, "lineno"):
                    exempt_lines.add(child.lineno)
    return exempt_lines


def _scan(path: Path) -> list[tuple[Path, int, str, str]]:
    tree = parse_ast(path)
    if tree is None:
        return []
    exempt_lines = _collect_main_guard_lines(tree)
    violations: list[tuple[Path, int, str, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Name) and func.id == "print"):
            continue
        if node.lineno in exempt_lines:
            continue
        violations.append(
            (path, node.lineno, symbol_of(tree, node), "raw print() call")
        )
    return violations


class TestNoPrintContract:
    """G14 — business-logic dirs MUST use `structlog`, not `print()`."""

    def test_no_unbaselined_violations(self):
        raw: list[tuple[Path, int | None, str | None, str | None]] = []
        for path in walk_src(_business_logic_roots()):
            for offender_path, line, symbol, extra in _scan(path):
                raw.append((offender_path, line, symbol, extra))

        violations = collect_violations(_RULE_KEY, raw, _RULE_ANCHOR)
        assert not violations, (
            f"G14: print() regressions ({len(violations)}). "
            'Replace `print(...)` with `logger.info("...", ...)` or add a '
            "baseline entry under `no_print:` with reason+ticket.\n"
            + "\n".join(violations)
        )
