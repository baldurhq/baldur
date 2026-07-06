"""G36 — a `Parked` catalog entry MUST NOT have a foreign SOLD importer.

Impl doc 589 D2/D5. The catalog's Product Status `Parked` asserts "not marketed
as a headline product." For a `Parked` entry whose Code Role is `product-feature`
that also implies "no SOLD feature depends on it" — otherwise the label
contradicts the wiring (the inverse of the claim-wiring class: a
not-a-product label must have NO production consumer, symmetric to G31/G32's "an
advertised guarantee MUST have one"). G36 enforces that direction.

**The predicate (= the D2 triage predicate — single source of truth).** For each
catalog entry with Product Status `Parked` AND Code Role ≠ `internal-support`,
scan `src/baldur(+_pro)` for a **functional** importer of its `Module` path(s) —
a module-level OR in-function `import` (NOT an `if TYPE_CHECKING:`-guarded import,
which couples types not runtime). An importer file counts as a SOLD-feature
importer **iff it physically lives under some OSS/PRO `product-feature` entry's
`Module` subtree**. A surviving SOLD-feature importer is a violation.

`internal-support` is the **escape hatch** (the D2 triage assigns it to exactly
the entries that DO have a SOLD functional importer — Security, FinOps, Learning,
ML Models, the forecasting primitives — so they are scanned OUT, not in). The
`settings_recommendation` ← `auto_tuning` case is the load-bearing TYPE_CHECKING
exclusion: its sole foreign importer is type-only, so it stays a clean `Parked` +
`product-feature` entry.

Making the triage predicate identical to the gate predicate (same shared catalog
parser, same import-classification) is what guarantees G36 is **enforced-empty by
construction**.

**Known limitation (dynamic seams).** The AST scan reads only static `import`
statements, so a Parked module consumed *only* via a dynamic seam
(`importlib.import_module(path)`, ProviderRegistry string-dispatch, `getattr`)
would be invisible → a false PASS. Mitigation (the #587 indirection remedy): a
companion **string-literal** scan flags any Parked module's dotted path appearing
as a string constant inside a SOLD-subtree file. Variable-name dynamic dispatch
(`importlib.import_module(var)`) stays out of reach by construction — the same
documented residual as G7/G32.

**Baseline granularity** — ENFORCED-EMPTY (`no_false_dormant: []`). A surviving
importer is FIXED by reclassifying the entry to `internal-support` (it IS a live
shared module) or by removing the coupling; `baseline.yaml` is a documented
fallback only.

Rule registry: ``ARCHITECTURE.md#g36-no-false-dormant``
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from tests.architecture._helpers import (
    CATALOG_PATH,
    PROJECT_ROOT,
    parse_catalog_entries,
    resolve_module_locations,
    walk_src,
)
from tests.architecture.conftest import collect_violations, parse_ast

_RULE_KEY = "no_false_dormant"
_RULE_ANCHOR = "#g36-no-false-dormant"

_SRC_DIR = PROJECT_ROOT / "src"


# ---------------------------------------------------------------------------
# Import-target resolution (pure helpers — unit-tested below).
# ---------------------------------------------------------------------------
def file_package(path: Path) -> str:
    """Dotted package of the file's directory, root-anchored at ``src/``.

    ``src/baldur/services/foo/bar.py`` → ``baldur.services.foo``; an
    ``__init__.py`` maps to its own directory's dotted path. The first component
    is the root package (``baldur`` / ``baldur_pro`` / ``baldur_dormant``).
    """
    rel = path.resolve().relative_to(_SRC_DIR)
    return ".".join(rel.parent.parts)


def _is_type_checking_test(test: ast.expr) -> bool:
    """True for ``if TYPE_CHECKING:`` / ``if typing.TYPE_CHECKING:``."""
    if isinstance(test, ast.Name):
        return test.id == "TYPE_CHECKING"
    if isinstance(test, ast.Attribute):
        return test.attr == "TYPE_CHECKING"
    return False


def _type_checking_import_ids(tree: ast.Module) -> set[int]:
    """Ids of every import node inside an ``if TYPE_CHECKING:`` body (type-only)."""
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and _is_type_checking_test(node.test):
            for stmt in node.body:
                for inner in ast.walk(stmt):
                    if isinstance(inner, (ast.Import, ast.ImportFrom)):
                        ids.add(id(inner))
    return ids


def _relative_base(pkg: str, level: int) -> str:
    """Resolve a relative-import base: drop ``level - 1`` trailing pkg segments."""
    if level <= 1:
        return pkg
    parts = pkg.split(".")
    drop = level - 1
    return ".".join(parts[:-drop]) if drop < len(parts) else ""


def functional_import_targets(tree: ast.Module, pkg: str) -> set[str]:
    """Return the set of dotted modules ``tree`` functionally imports (D5).

    Excludes ``if TYPE_CHECKING:``-guarded imports. Absolute and relative imports
    are both resolved to absolute dotted paths anchored on ``pkg`` (the importer
    file's package). For ``from X import name`` both ``X`` and ``X.name`` are
    emitted, so ``from baldur.services import compliance`` resolves to
    ``baldur.services.compliance``.
    """
    skip = _type_checking_import_ids(tree)
    targets: set[str] = set()
    for node in ast.walk(tree):
        if id(node) in skip:
            continue
        if isinstance(node, ast.Import):
            for alias in node.names:
                targets.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level and node.level > 0:
                base = _relative_base(pkg, node.level)
                if node.module:
                    base = f"{base}.{node.module}" if base else node.module
            else:
                base = node.module or ""
            if not base:
                continue
            targets.add(base)
            for alias in node.names:
                targets.add(f"{base}.{alias.name}")
    return targets


def imports_match(targets: set[str], prefix: str) -> bool:
    """True iff any import target is ``prefix`` or a submodule of it."""
    return any(t == prefix or t.startswith(prefix + ".") for t in targets)


# ---------------------------------------------------------------------------
# Catalog-derived sets.
# ---------------------------------------------------------------------------
def _entries():
    # FEATURE_CATALOG.md is monorepo-only (publish FORBIDDEN_PATHS), so it is
    # absent on the public OSS mirror. Every live G36 test routes through this
    # chokepoint, so the in-body skip here covers them all; the synthetic
    # anti-vacuous fixtures keep running on the mirror. G36 is fully covered by
    # the monorepo run.
    if not CATALOG_PATH.exists():
        pytest.skip("FEATURE_CATALOG.md is monorepo-only (FORBIDDEN_PATHS)")
    text = CATALOG_PATH.read_text(encoding="utf-8")
    entries = parse_catalog_entries(text)
    assert entries, "G36: FEATURE_CATALOG.md yielded no entries — parser broken"
    return entries


def _sold_subtree_paths(entries) -> set[Path]:
    """Filesystem dir/file paths of every OSS/PRO `product-feature` entry's Module."""
    paths: set[Path] = set()
    for entry in entries:
        if not entry.is_sold_product_feature:
            continue
        for module in entry.modules:
            for fs_path, _dotted in resolve_module_locations(module):
                paths.add(fs_path.resolve())
    return paths


def _parked_scanned(entries):
    """The `Parked` + non-`internal-support` entries G36 scans (enforced-empty)."""
    return [
        e
        for e in entries
        if e.product_status == "Parked" and e.code_role != "internal-support"
    ]


def _parked_prefixes(entry) -> set[str]:
    """The dotted module prefixes of a Parked entry (one per resolved location)."""
    prefixes: set[str] = set()
    for module in entry.modules:
        for _fs_path, dotted in resolve_module_locations(module):
            prefixes.add(dotted)
    return prefixes


def _is_under(path: Path, roots: set[Path]) -> bool:
    """True iff ``path`` is one of ``roots`` or nested under a root dir."""
    resolved = path.resolve()
    return any(resolved == r or r in resolved.parents for r in roots)


class TestNoFalseDormant:
    """G36 — no `Parked` product-feature entry has a foreign SOLD importer."""

    def test_no_parked_entry_has_a_sold_importer(self):
        """The static-import direction: a SOLD-subtree file functionally importing a Parked module."""
        entries = _entries()
        sold_paths = _sold_subtree_paths(entries)
        assert sold_paths, "G36: no SOLD product-feature subtrees parsed — broken"
        parked = _parked_scanned(entries)
        # Anti-vacuous: the 589 triage leaves exactly six Parked product-feature
        # entries; a zero here means the parser stopped recognizing them.
        assert parked, "G36: no Parked product-feature entries parsed — broken"
        prefixes = {e.title: _parked_prefixes(e) for e in parked}

        raw: list[tuple[Path, int | None, str | None, str | None]] = []
        scanned = 0
        for path in walk_src():  # baldur + baldur_pro
            if not _is_under(path, sold_paths):
                continue
            tree = parse_ast(path)
            if tree is None:
                continue
            scanned += 1
            targets = functional_import_targets(tree, file_package(path))
            if not targets:
                continue
            for entry in parked:
                if any(imports_match(targets, p) for p in prefixes[entry.title]):
                    raw.append(
                        (
                            path,
                            None,
                            entry.title,
                            f"SOLD-feature file functionally imports the Parked "
                            f"entry {entry.title!r} — relabel it `internal-support` "
                            f"(it IS a live shared module) or remove the coupling",
                        )
                    )
        # Anti-vacuous: the SOLD subtrees span many service files — a zero means
        # the sold-subtree resolution silently matched no files (a path bug), in
        # which case the scan would pass without examining anything.
        assert scanned > 50, (
            f"G36: only {scanned} SOLD-subtree files scanned — sold-subtree "
            "resolution is broken (the gate would pass vacuously)"
        )
        violations = collect_violations(_RULE_KEY, raw, _RULE_ANCHOR)
        assert not violations, (
            f"G36: {len(violations)} false-Parked coupling(s) — a `Parked` "
            "product-feature entry is functionally imported by a SOLD feature.\n"
            + "\n".join(violations)
        )

    def test_no_parked_entry_referenced_by_dynamic_seam(self):
        """The string-literal companion (dynamic-seam mitigation, #587 remedy).

        A Parked module consumed only via `importlib.import_module("dotted.path")`
        / ProviderRegistry string-dispatch is invisible to the static-import scan.
        This flags any Parked dotted path appearing as a string CONSTANT inside a
        SOLD-subtree file.
        """
        entries = _entries()
        sold_paths = _sold_subtree_paths(entries)
        parked = _parked_scanned(entries)
        prefixes = {e.title: _parked_prefixes(e) for e in parked}

        raw: list[tuple[Path, int | None, str | None, str | None]] = []
        for path in walk_src():
            if not _is_under(path, sold_paths):
                continue
            tree = parse_ast(path)
            if tree is None:
                continue
            constants = {
                node.value
                for node in ast.walk(tree)
                if isinstance(node, ast.Constant) and isinstance(node.value, str)
            }
            for entry in parked:
                for prefix in prefixes[entry.title]:
                    if any(
                        c == prefix or c.startswith(prefix + ".") for c in constants
                    ):
                        raw.append(
                            (
                                path,
                                None,
                                entry.title,
                                f"SOLD-feature file references Parked entry "
                                f"{entry.title!r} via a string-literal dotted path "
                                f"(dynamic seam) — relabel `internal-support` or "
                                f"remove the coupling",
                            )
                        )
                        break
        violations = collect_violations(_RULE_KEY, raw, _RULE_ANCHOR)
        assert not violations, (
            f"G36: {len(violations)} dynamic-seam reference(s) to a Parked "
            "product-feature entry from a SOLD feature.\n" + "\n".join(violations)
        )


# ---------------------------------------------------------------------------
# Anti-silent-pass inline fixtures (G24/G32 precedent). Synthetic source proves
# the import classifier flags the live coupling shapes and clears the exempt ones
# — so the gate cannot pass vacuously even while the live population is clean.
# `P` = the Parked module under test (`baldur.services.parked`). Type: Behavior.
# ---------------------------------------------------------------------------
_PARKED = "baldur.services.parked"


def _targets(source: str, pkg: str = "baldur.services.sold") -> set[str]:
    return functional_import_targets(ast.parse(source), pkg)


class TestFunctionalImportClassifier:
    """`functional_import_targets` / `imports_match` — the predicate core."""

    def test_module_level_absolute_import_flagged(self):
        targets = _targets("from baldur.services.parked import X\n")
        assert imports_match(targets, _PARKED)

    def test_submodule_absolute_import_flagged(self):
        targets = _targets("from baldur.services.parked.tasks import Y\n")
        assert imports_match(targets, _PARKED)

    def test_from_parent_import_name_flagged(self):
        targets = _targets("from baldur.services import parked\n")
        assert imports_match(targets, _PARKED)

    def test_plain_import_dotted_flagged(self):
        targets = _targets("import baldur.services.parked.svc\n")
        assert imports_match(targets, _PARKED)

    def test_in_function_import_flagged(self):
        source = "def f():\n    from baldur.services.parked import X\n    return X\n"
        assert imports_match(_targets(source), _PARKED)

    def test_relative_import_resolved_and_flagged(self):
        # A file in `baldur.services.sold` doing `from ..parked import X`.
        source = "from ..parked import X\n"
        targets = functional_import_targets(ast.parse(source), "baldur.services.sold")
        assert imports_match(targets, _PARKED)

    def test_type_checking_import_not_flagged(self):
        # The load-bearing settings_recommendation ← auto_tuning shape.
        source = (
            "from typing import TYPE_CHECKING\n"
            "if TYPE_CHECKING:\n"
            "    from baldur.services.parked import X\n"
        )
        assert not imports_match(_targets(source), _PARKED)

    def test_unrelated_import_not_flagged(self):
        targets = _targets("from baldur.services.other import X\n")
        assert not imports_match(targets, _PARKED)

    def test_sibling_path_prefix_not_false_matched(self):
        # `parked_extra` must NOT match the `parked` prefix (boundary-aware).
        targets = _targets("from baldur.services.parked_extra import X\n")
        assert not imports_match(targets, _PARKED)

    def test_file_level_product_not_matched_by_sibling_module(self):
        # File-level Parked product `...predictive_forecaster.service`: importing
        # the SIBLING `...time_series` primitive must NOT match the product.
        product = "baldur.services.predictive_forecaster.service"
        targets = _targets("from baldur.core.time_series import H\n")
        assert not imports_match(targets, product)


class TestCatalogDerivedSets:
    """The live catalog yields the expected SOLD / Parked partitions."""

    def test_six_parked_product_features_scanned(self):
        # 589 D2/D3: exactly six Parked + product-feature entries are the
        # enforced-empty scan target.
        parked = _parked_scanned(_entries())
        assert len(parked) == 6

    def test_internal_support_entries_are_exempt(self):
        # The five internal-support entries are NOT in the scanned set.
        entries = _entries()
        internal = [
            e
            for e in entries
            if e.product_status == "Parked" and e.code_role == "internal-support"
        ]
        assert len(internal) == 5
        scanned_titles = {e.title for e in _parked_scanned(entries)}
        assert not (scanned_titles & {e.title for e in internal})

    def test_sold_subtrees_nonempty(self):
        assert _sold_subtree_paths(_entries())


class TestG36CatalogAbsentSkip:
    """663 D4 — the module-level _entries() chokepoint skips when the catalog is absent.

    Every live G36 test routes through ``_entries()``, so the in-body skip there
    covers them all; the synthetic import-classifier fixtures keep running on the
    mirror. G36 is fully covered by the monorepo run.
    """

    def test_entries_skips_when_catalog_absent(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            "tests.architecture.test_no_false_dormant.CATALOG_PATH",
            tmp_path / "no_catalog.md",
        )
        with pytest.raises(pytest.skip.Exception):
            _entries()
