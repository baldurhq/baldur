"""G40 - the first-party import-time graph MUST be acyclic.

CLAUDE.md Pattern Compliance carries a "Dependency safety: no circular imports"
claim that, until this gate, had zero mechanized enforcement - the only Pattern
Compliance bullet without a fitness function. A structural cycle is invisible
until an import-order change turns it into a runtime ``ImportError`` /
``AttributeError``, and the project's PEP 562 lazy-import convention masks the
problem further (a lazy import inside a ``def`` body breaks the cycle at runtime
but the structural dependency is still there).

**What "import-time" means here.** Only imports that execute when a module is
imported become graph edges:

- Module-body imports, and imports inside module-level ``if`` / ``try`` /
  ``with`` / ``for`` / ``while`` blocks and class bodies, ARE edges (they run at
  import time).
- Imports inside a ``def`` / ``async def`` body are NOT edges (lazy - they run
  only when the function is called; the PEP 562 convention exists precisely to
  break cycles this way).
- Imports inside an ``if TYPE_CHECKING:`` block are NOT edges (never executed at
  runtime).

**Dual-edge target resolution.** For ``from pkg import name`` two edges are
added: one to the FROM-module ``pkg`` (its ``__init__`` executes) and, when
``pkg.name`` is itself a module on disk, one to ``pkg.name``. The second edge is
load-bearing: the real CLI hub is ``app.py`` doing
``from baldur.cli.commands import admin as _admin_cmd`` against
``commands/admin.py`` doing ``from baldur.cli.app import ...`` back - a
FROM-module-only resolver points the first edge at the ``commands`` package
(not the ``admin`` submodule), the back-edge never forms, and the gate is
vacuous. The submodule edge resolves through the imported ``name`` (the on-disk
module), never the local ``asname`` binding.

This collection pass is distinct from G38's: it reuses ``parse_ast`` /
``walk_src`` / ``PROJECT_ROOT`` and replicates G38's relative-import ``level``
math, but NOT its ``ast.walk`` node collection (which would count lazy edges)
nor its FROM-module-only resolution (which would drop the submodule edge).

**Known limitations** (out of the import-time-static threat model by design):

- Dynamic imports (``importlib.import_module`` / ``__import__``) are not
  statically resolvable, so a module-level dynamic import forming a real cycle
  is a residual false-negative (rare; most dynamic imports are function-body and
  already excluded as non-import-time).
- ``if TYPE_CHECKING:`` imports cannot cause an import-time cycle by construction
  (not executed at runtime), so they are never edges.

**Enforced-empty, no baseline** (precedent G20 / G21 / G38). Every cycle is a
structural defect to dissolve (relocate the shared symbol to a leaf module),
never baselined.

Rule registry: ``ARCHITECTURE.md#g40-import-cycles``
"""

from __future__ import annotations

import ast
from collections.abc import Iterable
from pathlib import Path

from tests.architecture.conftest import PROJECT_ROOT, parse_ast, walk_src

_SRC = PROJECT_ROOT / "src"
_FIRST_PARTY_ROOTS = ("baldur", "baldur_pro", "baldur_dormant")
_SCAN_ROOTS = tuple(_SRC / name for name in _FIRST_PARTY_ROOTS)


def _module_parts(path: Path, src_root: Path) -> list[str]:
    """Dotted parts of the module ``path`` defines (``__init__`` stripped)."""
    rel = path.relative_to(src_root).with_suffix("")
    parts = list(rel.parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return parts


def _package_parts(path: Path, src_root: Path) -> list[str]:
    """Dotted parts of the package that *contains* the relative-import anchor."""
    parts = _module_parts(path, src_root)
    if path.name == "__init__.py":
        return parts
    return parts[:-1]


def _resolve_from_module(
    path: Path, node: ast.ImportFrom, src_root: Path
) -> list[str] | None:
    """Resolve an ``ImportFrom`` to its absolute FROM-module dotted parts.

    Replicates G38's ``level`` math: absolute imports as-is; relative imports
    anchored at the file's package, climbing ``level - 1`` parents. Returns
    ``None`` for over-deep relative imports (which Python itself rejects).
    """
    if node.level == 0:
        return node.module.split(".") if node.module else None
    pkg = _package_parts(path, src_root)
    up = node.level - 1
    if up > len(pkg):
        return None
    base = pkg[: len(pkg) - up]
    if node.module:
        base = base + node.module.split(".")
    return base or None


def _module_exists(dotted: list[str], src_root: Path) -> bool:
    """True when the dotted module resolves to a ``.py`` or package on disk."""
    base = src_root.joinpath(*dotted)
    return base.with_suffix(".py").exists() or (base / "__init__.py").exists()


def _is_type_checking(node: ast.If) -> bool:
    """True for ``if TYPE_CHECKING:`` / ``if typing.TYPE_CHECKING:`` guards."""
    test = node.test
    if isinstance(test, ast.Name):
        return test.id == "TYPE_CHECKING"
    if isinstance(test, ast.Attribute):
        return test.attr == "TYPE_CHECKING"
    return False


def _iter_import_time_imports(
    tree: ast.Module,
) -> Iterable[ast.Import | ast.ImportFrom]:
    """Yield only the import statements that execute at module import time.

    Descends the module body, class bodies, and module-level compound
    statements (``if`` / ``try`` / ``with`` / ``for`` / ``while``), but NOT
    ``def`` / ``async def`` bodies (lazy imports) nor ``if TYPE_CHECKING:``
    blocks (never executed at runtime).
    """

    def visit(node: ast.AST) -> Iterable[ast.Import | ast.ImportFrom]:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.Import, ast.ImportFrom)):
                yield child
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue  # lazy import - never an import-time edge
            elif isinstance(child, ast.If) and _is_type_checking(child):
                continue  # TYPE_CHECKING block - not executed at runtime
            else:
                yield from visit(child)

    yield from visit(tree)


def _resolve_import_targets(
    path: Path,
    node: ast.Import | ast.ImportFrom,
    src_root: Path,
    first_party_roots: tuple[str, ...],
) -> list[str]:
    """Resolve one import statement to its first-party target module(s).

    ``import a.b.c`` -> the dotted target ``a.b.c`` (always a module). ``from pkg
    import name`` -> the FROM-module ``pkg`` PLUS ``pkg.name`` when that resolves
    to a module on disk (dual-edge). The submodule edge resolves through the
    imported ``name``, never the local ``asname``.
    """
    targets: list[str] = []
    if isinstance(node, ast.Import):
        for alias in node.names:
            parts = alias.name.split(".")
            if parts and parts[0] in first_party_roots:
                targets.append(".".join(parts))
        return targets

    from_module = _resolve_from_module(path, node, src_root)
    if from_module is None:
        return targets
    if from_module[0] in first_party_roots:
        targets.append(".".join(from_module))
        for alias in node.names:
            if alias.name == "*":
                continue
            candidate = from_module + [alias.name]
            if _module_exists(candidate, src_root):
                targets.append(".".join(candidate))
    return targets


def build_import_graph(
    roots: Iterable[Path],
    src_root: Path = _SRC,
    first_party_roots: tuple[str, ...] = _FIRST_PARTY_ROOTS,
) -> dict[str, set[str]]:
    """Build the first-party import-time dependency graph over ``roots``.

    Nodes are dotted module names; an edge ``A -> B`` means importing ``A``
    executes an import of first-party module ``B`` at import time. Self-edges are
    dropped (they cannot form an SCC of size > 1).
    """
    graph: dict[str, set[str]] = {}
    for path in walk_src(roots):
        tree = parse_ast(path)
        if tree is None:
            continue
        source = ".".join(_module_parts(path, src_root))
        edges = graph.setdefault(source, set())
        for node in _iter_import_time_imports(tree):
            for target in _resolve_import_targets(
                path, node, src_root, first_party_roots
            ):
                if target != source:
                    edges.add(target)
    return graph


def strongly_connected_components(graph: dict[str, set[str]]) -> list[list[str]]:
    """Tarjan's SCC algorithm (iterative, to stay clear of recursion limits).

    Returns one list of node names per strongly connected component. A component
    of size > 1 is an import cycle.
    """
    counter = 0
    index: dict[str, int] = {}
    lowlink: dict[str, int] = {}
    on_stack: dict[str, bool] = {}
    stack: list[str] = []
    result: list[list[str]] = []

    for root in list(graph.keys()):
        if root in index:
            continue
        work: list[tuple[str, object]] = [(root, iter(sorted(graph.get(root, ()))))]
        index[root] = lowlink[root] = counter
        counter += 1
        stack.append(root)
        on_stack[root] = True
        while work:
            node, iterator = work[-1]
            advanced = False
            for successor in iterator:  # type: ignore[assignment]
                if successor not in index:
                    index[successor] = lowlink[successor] = counter
                    counter += 1
                    stack.append(successor)
                    on_stack[successor] = True
                    work.append((successor, iter(sorted(graph.get(successor, ())))))
                    advanced = True
                    break
                if on_stack.get(successor):
                    lowlink[node] = min(lowlink[node], index[successor])
            if advanced:
                continue
            if lowlink[node] == index[node]:
                component: list[str] = []
                while True:
                    member = stack.pop()
                    on_stack[member] = False
                    component.append(member)
                    if member == node:
                        break
                result.append(component)
            work.pop()
            if work:
                parent = work[-1][0]
                lowlink[parent] = min(lowlink[parent], lowlink[node])
    return result


def find_import_cycles(
    roots: Iterable[Path],
    src_root: Path = _SRC,
    first_party_roots: tuple[str, ...] = _FIRST_PARTY_ROOTS,
) -> list[list[str]]:
    """Return every import cycle (SCC of size > 1) in the import-time graph."""
    graph = build_import_graph(roots, src_root, first_party_roots)
    return [
        sorted(component)
        for component in strongly_connected_components(graph)
        if len(component) > 1
    ]


class TestImportCycles:
    """G40 - the first-party import-time graph MUST be acyclic."""

    def test_no_import_cycles(self):
        cycles = find_import_cycles(_SCAN_ROOTS)
        assert not cycles, (
            f"G40: {len(cycles)} import-time cycle(s) detected. Each is a "
            "structural dependency that breaks at runtime on any import-order "
            "change; dissolve it by relocating the shared symbol to a leaf "
            "module (see the CLI `_bootstrap` precedent). Registry: "
            "ARCHITECTURE.md#g40-import-cycles\n"
            + "\n".join(" <-> ".join(cycle) for cycle in cycles)
        )

    def test_detects_synthetic_cycle(self, tmp_path):
        """Non-vacuity: a real module-level 2-cycle IS flagged."""
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("", encoding="utf-8")
        (pkg / "a.py").write_text("from pkg import b\n", encoding="utf-8")
        (pkg / "b.py").write_text("from pkg import a\n", encoding="utf-8")
        cycles = find_import_cycles(
            (pkg,), src_root=tmp_path, first_party_roots=("pkg",)
        )
        assert ["pkg.a", "pkg.b"] in cycles

    def test_lazy_import_only_cycle_not_flagged(self, tmp_path):
        """A back-edge living inside a ``def`` body is NOT an import-time edge.

        Pins the guarantee the whole gate rests on: ``a`` imports ``b`` at module
        level, ``b`` imports ``a`` only inside a function body, so no import-time
        cycle exists and the detector must stay silent.
        """
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("", encoding="utf-8")
        (pkg / "a.py").write_text("import pkg.b\n", encoding="utf-8")
        (pkg / "b.py").write_text(
            "def use():\n    import pkg.a\n    return pkg.a\n", encoding="utf-8"
        )
        cycles = find_import_cycles(
            (pkg,), src_root=tmp_path, first_party_roots=("pkg",)
        )
        assert cycles == []

    def test_aliased_from_import_resolves_through_name_not_asname(self):
        """Dual-edge resolves the submodule via the imported ``name``, not ``asname``.

        The exact shape of the real CLI hub:
        ``from baldur.cli.commands import admin as _admin_cmd``. The submodule
        edge MUST point at ``baldur.cli.commands.admin`` (a real module) via the
        imported ``name``, never at ``baldur.cli.commands._admin_cmd`` (the local
        binding) - a resolver reading ``asname`` would target a non-existent
        module, drop the CLI-hub back-edge, and make the gate silently vacuous.
        """
        anchor = _SRC / "baldur" / "cli" / "app.py"
        node = ast.ImportFrom(
            module="baldur.cli.commands",
            names=[ast.alias(name="admin", asname="_admin_cmd")],
            level=0,
        )
        targets = _resolve_import_targets(anchor, node, _SRC, _FIRST_PARTY_ROOTS)
        assert "baldur.cli.commands.admin" in targets
        assert "baldur.cli.commands._admin_cmd" not in targets
        assert "baldur.cli.commands" in targets  # FROM-module edge

    def test_relative_from_import_resolves_to_package_absolute(self):
        """``from . import submodule`` yields the package-absolute ``pkg.submodule`` edge.

        Layering dual-edge resolution on the relative-``level`` math is new in
        this gate, so the package-absolute resolution is pinned directly.
        """
        anchor = _SRC / "baldur" / "cli" / "app.py"
        node = ast.ImportFrom(
            module=None,
            names=[ast.alias(name="commands", asname=None)],
            level=1,
        )
        targets = _resolve_import_targets(anchor, node, _SRC, _FIRST_PARTY_ROOTS)
        assert "baldur.cli.commands" in targets
