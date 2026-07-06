"""G12 — Env reads forbidden in module-level `_create_*` factory bodies.

Per D8.c (related to OOS #453): module-level factories named ``_create_*``
should receive their configuration via typed ``BaldurRuntime`` env accessors,
NOT raw ``os.environ.get`` / ``os.getenv`` reads.

A function is in scope when ALL hold:
1. The function name matches ``^_create_*``.
2. The function is module-level (not a class method, not nested).
3. The function body contains at least one ``os.environ.get`` / ``os.getenv``
   call AND does NOT reference ``BaldurRuntime`` or any ``runtime.<...>``
   attribute lookup (per OOS #453 sweep filter D1).

The 4 named offenders from OOS #453 populate this rule's baseline at landing
and are removed as their migration commits land:
- ``audit/checkpoint/__init__.py:357``
- ``adapters/audit/singleton.py:41``
- ``adapters/airgap/factory.py:28,46``
- ``adapters/metrics/factory.py:52``

Rule registry: ``ARCHITECTURE.md#g12-env-factory-bodies``
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

_RULE_KEY = "env_factory_bodies"
_RULE_ANCHOR = "#g12-env-factory-bodies"


def _is_env_read(call: ast.Call) -> bool:
    func = call.func
    if not isinstance(func, ast.Attribute):
        return False
    receiver = func.value
    if func.attr == "get" and isinstance(receiver, ast.Attribute):
        if (
            receiver.attr == "environ"
            and isinstance(receiver.value, ast.Name)
            and receiver.value.id == "os"
        ):
            return True
    if func.attr == "getenv" and isinstance(receiver, ast.Name) and receiver.id == "os":
        return True
    return False


def _references_runtime(function_body: list[ast.stmt]) -> bool:
    """True when the body references BaldurRuntime / runtime accessors."""
    for stmt in function_body:
        for node in ast.walk(stmt):
            if isinstance(node, ast.Name) and node.id == "BaldurRuntime":
                return True
            if isinstance(node, ast.Attribute):
                if isinstance(node.value, ast.Name) and node.value.id == "runtime":
                    return True
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id == "get_runtime":
                    return True
    return False


def _scan(path: Path) -> list[tuple[Path, int, str]]:
    tree = parse_ast(path)
    if tree is None:
        return []
    violations: list[tuple[Path, int, str]] = []
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not node.name.startswith("_create_"):
            continue
        env_calls = [
            child
            for child in ast.walk(node)
            if isinstance(child, ast.Call) and _is_env_read(child)
        ]
        if not env_calls:
            continue
        if _references_runtime(node.body):
            continue
        for call in env_calls:
            violations.append(
                (
                    path,
                    call.lineno,
                    f"{node.name}() reads os.environ without BaldurRuntime accessor",
                )
            )
    return violations


class TestEnvFactoryBodiesContract:
    """G12 — module-level `_create_*` factories MUST use BaldurRuntime."""

    def test_no_unbaselined_violations(self):
        raw: list[tuple[Path, int | None, str | None, str | None]] = []
        for path in walk_src(DEFAULT_SRC_ROOTS):
            for offender_path, line, extra in _scan(path):
                raw.append((offender_path, line, None, extra))

        violations = collect_violations(_RULE_KEY, raw, _RULE_ANCHOR)
        assert not violations, (
            f"G12: env-factory regressions ({len(violations)}). "
            "Migrate the factory to `BaldurRuntime` env accessors or add a "
            "baseline entry under `env_factory_bodies:` with reason+ticket.\n"
            + "\n".join(violations)
        )
