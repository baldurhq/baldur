"""G17 + G17b + G17c — OSS->PRO / OSS->Dormant module-level import boundary.

CLAUDE.md § Pattern Compliance — the OSS source tree (``src/baldur/``) must
not depend on ``baldur_pro`` or ``baldur_dormant`` at module-load time.
Both are shipped as private-distribution sibling packages and are absent
from a clean ``pip install baldur-framework`` wheel — module-level
``from baldur_pro ... import ...`` / ``from baldur_dormant ... import ...``
raises ``ModuleNotFoundError`` on every user machine that omits the
private extras.

Three complementary rules ratchet the boundary in opposite directions:

G17  — per-site allowlist (``baseline.yaml[pro_import_boundary]``).
    Flags any module-level ``from baldur_pro`` / ``import baldur_pro`` in
    ``src/baldur/`` that is not in the baseline. Module-level includes:
    bare top-level imports, imports inside ``try / except ImportError``
    blocks, and imports under ``if TYPE_CHECKING:`` guards. In-function
    lazy imports are out of G17 scope but are covered by G17b.

G17b — global-count ratchet
    (``baseline.yaml[pro_import_boundary_count]``).
    Counts every ``baldur_pro`` occurrence under ``src/baldur/``
    (module-level + in-function combined) and fails if the count is
    strictly greater than the recorded snapshot. The number must
    decrease over time as 518 batches retire callsites; raising the
    snapshot requires a deliberate baseline bump in the same PR.

G17c — global-count ratchet for the OSS->Dormant direction
    (``baseline.yaml[dormant_import_boundary_count]``).
    Counts every ``baldur_dormant`` occurrence under ``src/baldur/``
    and fails if the count exceeds the recorded snapshot. Same shape as
    G17b but for the 528 D10-v2 Dormant-tier boundary. The target is 0:
    OSS callers should resolve concrete adapters via ``ProviderRegistry``
    rather than importing ``baldur_dormant`` directly. Initial snapshot
    captures the Stage 2b residual sites that still in-function-import
    ``baldur_dormant`` adapters (server.py kafka reset, redis_bus.py
    fallback, distributed_channel.py kafka eventbus, etc.).

Scope: OSS->PRO and OSS->Dormant directions only. ``baldur_pro/`` /
``baldur_dormant/`` importing OSS is DIP-correct and not a violation
(PRO depends on OSS, never the reverse).

Rule registry:
``ARCHITECTURE.md#g17-pro-import-boundary``
``ARCHITECTURE.md#g17b-pro-import-count``
``ARCHITECTURE.md#g17c-dormant-import-count``

Note on naming: 516 Implementation Decision D1 referenced this rule as
"G15". Because ``#g15-env-prefix-naming`` was already in use, the
implementation took the next free numeric slot (G17) — the rule body
is unchanged.
"""

from __future__ import annotations

import ast
from pathlib import Path

from tests.architecture._helpers import _load_baseline_document
from tests.architecture.conftest import (
    PROJECT_ROOT,
    collect_violations,
    parse_ast,
    symbol_of,
    walk_src,
)

_PER_SITE_RULE_KEY = "pro_import_boundary"
_PER_SITE_RULE_ANCHOR = "#g17-pro-import-boundary"
_COUNT_RULE_KEY = "pro_import_boundary_count"
_COUNT_RULE_ANCHOR = "#g17b-pro-import-count"
_DORMANT_COUNT_RULE_KEY = "dormant_import_boundary_count"
_DORMANT_COUNT_RULE_ANCHOR = "#g17c-dormant-import-count"

_PRO_TOP_LEVEL = "baldur_pro"
_DORMANT_TOP_LEVEL = "baldur_dormant"


def _is_pro_module(name: str | None) -> bool:
    if not name:
        return False
    return name == _PRO_TOP_LEVEL or name.startswith(_PRO_TOP_LEVEL + ".")


def _is_dormant_module(name: str | None) -> bool:
    if not name:
        return False
    return name == _DORMANT_TOP_LEVEL or name.startswith(_DORMANT_TOP_LEVEL + ".")


def _is_dormant_import(node: ast.Import | ast.ImportFrom) -> bool:
    if isinstance(node, ast.ImportFrom):
        return _is_dormant_module(node.module)
    return any(_is_dormant_module(alias.name) for alias in node.names)


def _iter_import_nodes(
    node: ast.AST,
) -> list[ast.Import | ast.ImportFrom]:
    """Return Import/ImportFrom nodes anywhere in the subtree of ``node``."""
    return [
        child
        for child in ast.walk(node)
        if isinstance(child, (ast.Import, ast.ImportFrom))
    ]


def _is_pro_import(node: ast.Import | ast.ImportFrom) -> bool:
    if isinstance(node, ast.ImportFrom):
        return _is_pro_module(node.module)
    return any(_is_pro_module(alias.name) for alias in node.names)


def _scan_module_level(path: Path) -> list[tuple[Path, int, str, str]]:
    """Per-site G17 scan — module-level ``baldur_pro`` import sites.

    All sites are module-level (bare, or inside a module-level ``try`` /
    ``if TYPE_CHECKING:`` block), so ``symbol_of`` resolves every one to
    ``MODULE_SYMBOL`` (``"<module>"``) — the per-symbol key for D7. The 5
    hedging entries (1 ``if TYPE_CHECKING:`` + 4 bottom-of-module
    ``try / except ImportError``) therefore all fold to one ``<module>`` key.
    """
    tree = parse_ast(path)
    if tree is None:
        return []
    violations: list[tuple[Path, int, str, str]] = []
    for node in tree.body:
        # Direct top-level Import / ImportFrom
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            if _is_pro_import(node):
                target = _format_target(node)
                violations.append(
                    (
                        path,
                        node.lineno,
                        symbol_of(tree, node),
                        f"module-level import of '{target}'",
                    )
                )
            continue
        # try / except ImportError block (wrap pattern)
        if isinstance(node, ast.Try):
            for child in _iter_import_nodes(node):
                if _is_pro_import(child):
                    target = _format_target(child)
                    violations.append(
                        (
                            path,
                            child.lineno,
                            symbol_of(tree, child),
                            f"module-level try-import of '{target}'",
                        )
                    )
            continue
        # if TYPE_CHECKING: block
        if isinstance(node, ast.If) and _is_type_checking_guard(node):
            for child in _iter_import_nodes(node):
                if _is_pro_import(child):
                    target = _format_target(child)
                    violations.append(
                        (
                            path,
                            child.lineno,
                            symbol_of(tree, child),
                            f"TYPE_CHECKING import of '{target}'",
                        )
                    )
            continue
    return violations


def _format_target(node: ast.Import | ast.ImportFrom) -> str:
    if isinstance(node, ast.ImportFrom):
        return node.module or _PRO_TOP_LEVEL
    return node.names[0].name


def _is_type_checking_guard(node: ast.If) -> bool:
    test = node.test
    if isinstance(test, ast.Name) and test.id == "TYPE_CHECKING":
        return True
    if isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING":
        return True
    return False


def _count_all_pro_imports(path: Path) -> int:
    """G17b helper — count every ``baldur_pro`` import statement in ``path``.

    Includes module-level + in-function + try/except + TYPE_CHECKING.
    A single statement contributes 1 regardless of nested aliases.
    """
    tree = parse_ast(path)
    if tree is None:
        return 0
    count = 0
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)) and _is_pro_import(node):
            count += 1
    return count


def _count_all_dormant_imports(path: Path) -> int:
    """G17c helper — count every ``baldur_dormant`` import statement in ``path``.

    Includes module-level + in-function + try/except + TYPE_CHECKING.
    A single statement contributes 1 regardless of nested aliases.
    """
    tree = parse_ast(path)
    if tree is None:
        return 0
    count = 0
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)) and _is_dormant_import(node):
            count += 1
    return count


class TestProImportBoundary:
    """G17 — OSS code must not module-level-import baldur_pro."""

    def test_no_unbaselined_module_level_pro_imports(self):
        roots = [PROJECT_ROOT / "src" / "baldur"]
        raw: list[tuple[Path, int | None, str | None, str | None]] = []
        for path in walk_src(roots):
            for offender_path, line, symbol, extra in _scan_module_level(path):
                raw.append((offender_path, line, symbol, extra))

        violations = collect_violations(_PER_SITE_RULE_KEY, raw, _PER_SITE_RULE_ANCHOR)
        assert not violations, (
            f"G17: OSS->PRO module-level import regressions "
            f"({len(violations)}). Migrate the callsite to "
            "ProviderRegistry-based access (516 D2), or add a baseline "
            f"entry under `{_PER_SITE_RULE_KEY}:` with "
            "reason+ticket+category.\n" + "\n".join(violations)
        )


class TestProImportCount:
    """G17b — global count of baldur_pro imports must not increase."""

    def test_pro_import_count_does_not_exceed_snapshot(self):
        roots = [PROJECT_ROOT / "src" / "baldur"]
        observed = 0
        for path in walk_src(roots):
            observed += _count_all_pro_imports(path)

        document = _load_baseline_document()
        snapshot = document.get(_COUNT_RULE_KEY)
        assert isinstance(snapshot, int), (
            f"G17b: baseline.yaml must define an integer "
            f"`{_COUNT_RULE_KEY}` snapshot. "
            f"Add `{_COUNT_RULE_KEY}: {observed}`."
        )

        assert observed <= snapshot, (
            f"G17b: baldur_pro import count grew from snapshot {snapshot} "
            f"to {observed}. Either migrate the new callsite(s) to "
            "ProviderRegistry-based access (516 D2 / 518) so the count "
            f"decreases, or bump the snapshot `{_COUNT_RULE_KEY}: "
            f"{observed}` in the same PR with rationale."
        )


class TestDormantImportCount:
    """G17c — global count of baldur_dormant imports must not increase.

    Targets the 528 D10-v2 publication boundary. OSS callers should route
    through ``ProviderRegistry.<slot>`` rather than importing
    ``baldur_dormant`` directly. Stage 2b lands the registry slots + NoOp
    defaults; this ratchet ensures the residual in-function imports
    (server.py kafka reset, redis_bus.py fallback, etc.) shrink over time.
    Target = 0.
    """

    def test_dormant_import_count_does_not_exceed_snapshot(self):
        roots = [PROJECT_ROOT / "src" / "baldur"]
        observed = 0
        for path in walk_src(roots):
            observed += _count_all_dormant_imports(path)

        document = _load_baseline_document()
        snapshot = document.get(_DORMANT_COUNT_RULE_KEY)
        assert isinstance(snapshot, int), (
            f"G17c: baseline.yaml must define an integer "
            f"`{_DORMANT_COUNT_RULE_KEY}` snapshot. "
            f"Add `{_DORMANT_COUNT_RULE_KEY}: {observed}`."
        )

        assert observed <= snapshot, (
            f"G17c: baldur_dormant import count grew from snapshot "
            f"{snapshot} to {observed}. Either migrate the new callsite(s) "
            "to ProviderRegistry-based access (528 D10-v2) so the count "
            f"decreases, or bump the snapshot `{_DORMANT_COUNT_RULE_KEY}: "
            f"{observed}` in the same PR with rationale."
        )
