"""G6 — Health check naming contract (`is_healthy` MUST be a property).

CLAUDE.md § Pattern Compliance distinguishes:
- ``health_check()`` — adapter interface contract (I/O), normal method.
- ``is_healthy`` — cached state query (no I/O), MUST be a property.
- ``check_health()`` — active monitor probe (expensive I/O), normal method.

Per D8 this test enforces the *naming layer* only; semantic checks (does the
body actually perform I/O?) are explicitly OOS — they would require runtime
tracing.

Detection: ``ast.FunctionDef`` named ``is_healthy`` inside any ``ClassDef``
that does NOT carry a ``@property`` decorator. Module-level ``is_healthy``
functions are not flagged (rare, intentional shape).

Rule registry: ``ARCHITECTURE.md#g6-health-check-naming``
"""

from __future__ import annotations

import ast
from pathlib import Path

from tests.architecture.conftest import (
    DEFAULT_SRC_ROOTS,
    collect_violations,
    parse_ast,
    symbol_of,
    walk_src,
)

_RULE_KEY = "health_check_naming"
_RULE_ANCHOR = "#g6-health-check-naming"


def _is_property_decorator(decorator: ast.expr) -> bool:
    if isinstance(decorator, ast.Name) and decorator.id == "property":
        return True
    if isinstance(decorator, ast.Attribute) and decorator.attr == "property":
        return True
    return False


def _scan(path: Path) -> list[tuple[Path, int, str, str]]:
    tree = parse_ast(path)
    if tree is None:
        return []
    violations: list[tuple[Path, int, str, str]] = []
    for class_node in ast.walk(tree):
        if not isinstance(class_node, ast.ClassDef):
            continue
        for stmt in class_node.body:
            if not isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if stmt.name != "is_healthy":
                continue
            has_property = any(
                _is_property_decorator(deco) for deco in stmt.decorator_list
            )
            if not has_property:
                # `is_healthy` recurs across classes — class-qualify via the
                # method node's own qualname (Class.is_healthy, or
                # Outer.Inner.is_healthy for nested classes) (D6).
                violations.append(
                    (
                        path,
                        stmt.lineno,
                        symbol_of(tree, stmt),
                        f"{class_node.name}.is_healthy must be @property "
                        "(cached state query, no I/O)",
                    )
                )
    return violations


class TestHealthCheckNamingContract:
    """G6 — `is_healthy` MUST be declared as `@property`."""

    def test_no_unbaselined_violations(self):
        raw: list[tuple[Path, int | None, str | None, str | None]] = []
        for path in walk_src(DEFAULT_SRC_ROOTS):
            for offender_path, line, symbol, extra in _scan(path):
                raw.append((offender_path, line, symbol, extra))

        violations = collect_violations(_RULE_KEY, raw, _RULE_ANCHOR)
        assert not violations, (
            f"G6: health-check naming regressions ({len(violations)}). "
            "Decorate `is_healthy` with @property (cached state query, no I/O) "
            "per CLAUDE.md, or rename the method to `health_check` / "
            "`check_health`, or add a baseline entry under `health_check_naming:` "
            "with reason+ticket.\n" + "\n".join(violations)
        )
