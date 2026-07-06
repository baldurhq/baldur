"""G19 — tests/ may not module-level-import baldur_pro / baldur_dormant
without a gating marker.

CLAUDE.md § Test Location Rules + impl doc 528 D7/D11. After the
``tests/`` + ``tests/pro/`` + ``tests/dormant/`` publication split, the
public-repo test root MUST stay collectable on a clean
``pip install baldur-framework``
environment where ``baldur_pro`` and ``baldur_dormant`` are absent. A test
under ``tests/`` that module-level-imports either sibling package without
a gating mechanism crashes ``pytest --collect-only`` with
``ModuleNotFoundError`` on every external contributor's machine.

Scope direction: ``tests/`` -> (``baldur_pro`` | ``baldur_dormant``).
Tests under ``tests/pro/`` and ``tests/dormant/`` are exempt — they live in
the private repo where both packages are installed and the importorskip
gate would be dead code (528 D13 Phase 3 Stage 4 Step 7 strips it on move).

A file with a gated import passes G19 only if it carries either:

  1. ``pytest.importorskip("baldur_pro")`` (or ``"baldur_dormant"``) at
     module top — the collection-time hard gate. Skips the entire file
     before any AST below the call executes.
  2. ``pytestmark = pytest.mark.requires_pro`` at module top — the CI
     filter selector. The 528 D5 marker registered in ``pytest.ini``.

Either gate is sufficient. Both are typically added in pairs by
``scripts/add_requires_pro_marker.py`` (528 Stage 4 Step 1) but a stripped
``tests/pro/``-resident copy keeps only the marker (Step 7), so G19 must
accept either independently.

Architectural fitness function rule registry:
``ARCHITECTURE.md#g19-oss-test-pro-marker``

Note on numbering: 528 originally drafted this rule as "G18" but the
v1.0-default-enable rule (#g18-v1-default-enable, doc 527 D5) took that
slot first. 528 took the next free number (G19).
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path

from tests.architecture.conftest import (
    OSS_TESTS_ROOT,
    collect_violations,
    parse_ast,
)

_RULE_KEY = "oss_test_pro_marker"
_RULE_ANCHOR = "#g19-oss-test-pro-marker"
_GATED_TOP_LEVELS: tuple[str, ...] = ("baldur_pro", "baldur_dormant")


def _is_gated_top_level(name: str | None) -> bool:
    if not name:
        return False
    return name.split(".", 1)[0] in _GATED_TOP_LEVELS


def _is_gated_import(node: ast.Import | ast.ImportFrom) -> bool:
    if isinstance(node, ast.ImportFrom):
        return _is_gated_top_level(node.module)
    return any(_is_gated_top_level(alias.name) for alias in node.names)


def _has_importorskip(tree: ast.Module) -> bool:
    """True when module top calls ``pytest.importorskip("baldur_pro" | "baldur_dormant")``."""
    for node in tree.body:
        if not isinstance(node, ast.Expr):
            continue
        call = node.value
        if not isinstance(call, ast.Call):
            continue
        func = call.func
        if not (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id == "pytest"
            and func.attr == "importorskip"
        ):
            continue
        if call.args and isinstance(call.args[0], ast.Constant):
            arg = call.args[0].value
            if isinstance(arg, str) and _is_gated_top_level(arg):
                return True
    return False


def _has_requires_pro_pytestmark(tree: ast.Module) -> bool:
    """True when module top declares ``pytestmark = pytest.mark.requires_pro`` (single or list)."""
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(
            isinstance(t, ast.Name) and t.id == "pytestmark" for t in node.targets
        ):
            continue
        marks: list[ast.expr] = []
        if isinstance(node.value, (ast.List, ast.Tuple)):
            marks.extend(node.value.elts)
        else:
            marks.append(node.value)
        for mark in marks:
            if (
                isinstance(mark, ast.Attribute)
                and mark.attr == "requires_pro"
                and isinstance(mark.value, ast.Attribute)
                and mark.value.attr == "mark"
                and isinstance(mark.value.value, ast.Name)
                and mark.value.value.id == "pytest"
            ):
                return True
    return False


def _scan_module(path: Path) -> list[tuple[Path, int, str]]:
    """Return ``(path, lineno, extra)`` tuples for each unmarked gated import."""
    tree = parse_ast(path)
    if tree is None:
        return []
    if _has_importorskip(tree) or _has_requires_pro_pytestmark(tree):
        return []
    offenders: list[tuple[Path, int, str]] = []
    for node in tree.body:
        if not isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        if not _is_gated_import(node):
            continue
        if isinstance(node, ast.ImportFrom):
            target = node.module or _GATED_TOP_LEVELS[0]
        else:
            target = node.names[0].name
        offenders.append(
            (path, node.lineno, f"unmarked module-level import of '{target}'")
        )
    return offenders


def _walk_tests_oss() -> Iterator[Path]:
    """Walk the OSS test root's .py files, skipping ``__pycache__``."""
    root = OSS_TESTS_ROOT
    if not root.exists():
        return
    for path in root.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        yield path


class TestOssTestProMarker:
    """G19 — tests/ may not import baldur_pro / baldur_dormant unmarked."""

    def test_no_unmarked_gated_imports_in_oss_tests(self):
        raw: list[tuple[Path, int | None, str | None, str | None]] = []
        for path in _walk_tests_oss():
            for offender_path, line, extra in _scan_module(path):
                raw.append((offender_path, line, None, extra))

        violations = collect_violations(_RULE_KEY, raw, _RULE_ANCHOR)
        assert not violations, (
            f"G19: tests/ contains {len(violations)} unmarked import(s) "
            "of baldur_pro / baldur_dormant. Add either "
            '`pytest.importorskip("baldur_pro")` (or "baldur_dormant") or '
            "`pytestmark = pytest.mark.requires_pro` at module top, OR move "
            "the test under tests/pro/ / tests/dormant/.\n" + "\n".join(violations)
        )
