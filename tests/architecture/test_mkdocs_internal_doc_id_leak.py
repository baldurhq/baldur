"""G24 — no internal document identifiers on the published mkdocs surface.

The published docs site (mkdocs Material + mkdocstrings) renders two surfaces
that can leak internal project bookkeeping — impl-doc numbers, ``NNN DN``
decision IDs (qualified and bare ``DN``), qualified ``NNN GN`` / ``RN`` /
``CN`` gap/risk/choice item refs, ``ADR-NN`` / ``OOS`` / ``Wave NN`` /
``DEC-NN`` refs, ``#NNNN`` issue refs, ``file.py:NN`` citations,
``baldur_dormant.*`` private-distribution paths, and
``docs/{impl,laws,self_healing}/...`` paths — to external readers. Those
identifiers point at internal documents the publish allowlist (``mkdocs.yml``
``exclude_docs``) deliberately excludes, so a reader sees unresolvable
dangling references.

The two rendered surfaces this rule gates:

* **(B) Rendered docstrings** — public-symbol docstrings pulled by ``:::``
  directives across ``docs/reference/**``. The resolver follows each ``:::``
  target to its defining source file and AST-extracts every docstring
  (conventional + attribute) — a deliberate superset of what mkdocstrings'
  ``!^_`` filter actually renders, so a doc-ID added to a non-rendered
  docstring on a public-surface file is also flagged (and remediated by
  moving it to a ``#`` comment).
* **(A) Authored markdown + nav/site metadata** — the published ``.md`` text
  (``docs/index.md``, ``docs/getting-started/**``, ``docs/reference/**``) plus
  ``mkdocs.yml``'s rendered string values (``site_name`` / ``site_description``
  / ``nav`` leaf labels), parsed via ``mkdocs_safe_load`` so YAML comments — the
  bulk of ``mkdocs.yml``'s own doc-IDs — are discarded by construction.

Inline ``#`` source comments are intentionally retained for maintainer
traceability (CLAUDE.md § Code Language Rules); mkdocstrings renders
docstrings, not comments, and AST docstring extraction excludes comments by
construction, so the comment carve-out holds automatically.

Baseline is enforced-empty (no ``baseline.yaml`` key) — same discipline as
G20/G21/G22/G23.

Rule registry:
``ARCHITECTURE.md#g24-mkdocs-internal-doc-id-leak``
"""

from __future__ import annotations

import importlib
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.architecture.conftest import (
    DOC_ID_ALLOWLIST as _DOC_ID_ALLOWLIST,
)
from tests.architecture.conftest import (
    PROJECT_ROOT,
    REFERENCE_DIR,
    directive_targets,
    mkdocs_safe_load,
)
from tests.architecture.conftest import (
    find_doc_ids as _find_doc_ids,
)
from tests.architecture.conftest import (
    iter_docstrings as _iter_docstrings,
)

_SRC_ROOT = (PROJECT_ROOT / "src" / "baldur").resolve()
_REFERENCE_DIR = REFERENCE_DIR
_DOCS_DIR = PROJECT_ROOT / "docs"
_MKDOCS_YML = PROJECT_ROOT / "mkdocs.yml"

# ``_DOC_ID_PATTERNS`` / ``_DOC_ID_ALLOWLIST`` / ``_find_doc_ids`` /
# ``_iter_docstrings`` are imported from ``_helpers`` (the canonical home,
# shared with G23/G26/G27 so the doc-ID matcher + docstring extraction never
# drift across gates). Aliased to the original private names so the rest of
# this module and its anti-silent-pass unit tests stay unchanged.


def _iter_mkdocs_strings(yaml_text: str) -> Iterator[str]:
    """Yield ``mkdocs.yml`` rendered string values (pure function).

    Parses with ``mkdocs_safe_load`` (discarding comments by construction) and
    yields the values that render to readers: ``site_name``,
    ``site_description``, and every ``nav`` leaf label (recursively).
    """
    data = mkdocs_safe_load(yaml_text)
    if not isinstance(data, dict):
        return

    for key in ("site_name", "site_description"):
        value = data.get(key)
        if isinstance(value, str):
            yield value

    def walk_nav(item: object) -> Iterator[str]:
        if isinstance(item, str):
            yield item
        elif isinstance(item, list):
            for element in item:
                yield from walk_nav(element)
        elif isinstance(item, dict):
            for nav_key, nav_value in item.items():
                if isinstance(nav_key, str):
                    yield nav_key
                yield from walk_nav(nav_value)

    yield from walk_nav(data.get("nav", []))


def _file_in_src(obj_module: str | None, src_root: Path) -> Path | None:
    """Resolve a module name to its in-tree source file, or None."""
    if not obj_module:
        return None
    try:
        module = importlib.import_module(obj_module)
    except Exception:
        return None
    source = getattr(module, "__file__", None)
    if not source:
        return None
    path = Path(source).resolve()
    return path if src_root in path.parents else None


def _resolve_reference_source_files(
    reference_dir: Path = _REFERENCE_DIR,
    src_root: Path = _SRC_ROOT,
) -> set[Path]:
    """Resolve every ``:::`` target to the in-tree source file(s) it renders.

    Handles both directive kinds:

    * **module/package target** (``::: baldur.interfaces``): importable as a
      module → scan its own ``__file__`` AND walk its ``__all__``, mapping
      each member via ``obj.__module__`` to its defining file.
    * **symbol target** (``::: baldur.get_circuit_breaker_service``,
      ``::: baldur.decorators.dlq_protect.dlq_protect``): not importable as a
      module → import the parent package, ``getattr`` the leaf symbol, and
      resolve ``obj.__module__``. Without this fallback, symbol-only pages
      would never be scanned.

    Files outside ``src_root`` (re-exported third-party / private symbols) are
    dropped.
    """
    files: set[Path] = set()
    for target in directive_targets(reference_dir):
        try:
            module = importlib.import_module(target)
        except ModuleNotFoundError:
            # Symbol target — import the parent package and getattr the leaf.
            if "." not in target:
                continue
            parent_name, leaf = target.rsplit(".", 1)
            try:
                parent = importlib.import_module(parent_name)
            except Exception:
                continue
            obj = getattr(parent, leaf, None)
            if obj is None:
                continue
            resolved = _file_in_src(getattr(obj, "__module__", None), src_root)
            if resolved is not None:
                files.add(resolved)
            continue
        except Exception:
            continue

        # Module/package target — scan its own file plus every __all__ member.
        own = getattr(module, "__file__", None)
        if own:
            own_path = Path(own).resolve()
            if src_root in own_path.parents:
                files.add(own_path)
        for symbol_name in getattr(module, "__all__", []):
            obj = getattr(module, symbol_name, None)
            if obj is None:
                continue
            resolved = _file_in_src(getattr(obj, "__module__", None), src_root)
            if resolved is not None:
                files.add(resolved)
    return files


def _published_markdown_files(docs_dir: Path = _DOCS_DIR) -> list[Path]:
    """Return the published ``.md`` set per the ``mkdocs.yml`` allowlist."""
    files: list[Path] = []
    index = docs_dir / "index.md"
    if index.exists():
        files.append(index)
    getting_started = docs_dir / "getting-started"
    if getting_started.exists():
        files.extend(sorted(getting_started.rglob("*.md")))
    reference = docs_dir / "reference"
    if reference.exists():
        files.extend(sorted(reference.rglob("*.md")))
    return files


def _rel(path: Path) -> str:
    try:
        return path.resolve().relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return path.as_posix()


class TestMkdocsInternalDocIdLeak:
    """G24 — the published mkdocs surface carries no internal doc-IDs."""

    def test_reference_source_set_is_resolvable(self):
        """Anti-vacuous-pass guard for the ``:::`` resolver.

        Asserts the resolver yields a non-empty file set covering BOTH a
        package-target page and a symbol-only-target page
        (``decorators/dlq_protect.py``, reachable solely via the symbol
        fallback). A resolver that silently drops symbol targets — or breaks
        entirely — fails loudly here instead of letting the leak gate pass
        vacuously.
        """
        files = _resolve_reference_source_files()
        assert files, (
            "G24: resolved zero source files from the reference ::: directives "
            "— the resolver is broken, so the doc-ID gate would vacuously pass."
        )
        rels = {_rel(path) for path in files}

        symbol_page = "src/baldur/decorators/dlq_protect.py"
        assert symbol_page in rels, (
            f"G24: {symbol_page} missing from the resolved set — the symbol-"
            "target resolution fallback (::: baldur.decorators.dlq_protect."
            "dlq_protect) is broken, so symbol-only pages escape the gate."
        )

        has_package_page = any(rel.startswith("src/baldur/interfaces/") for rel in rels)
        assert has_package_page, (
            "G24: no baldur.interfaces source file resolved — the module/"
            "package-target __all__ walk (::: baldur.interfaces) is broken."
        )

    def test_no_doc_ids_in_rendered_docstrings(self):
        offenders: list[str] = []
        for path in sorted(_resolve_reference_source_files()):
            source = path.read_text(encoding="utf-8")
            ids: list[str] = []
            for docstring in _iter_docstrings(source):
                ids.extend(_find_doc_ids(docstring))
            if ids:
                unique = ", ".join(sorted(set(ids)))
                offenders.append(f"  {_rel(path)} — {unique}")

        assert not offenders, (
            f"G24: internal doc-IDs found in rendered docstrings "
            f"({len(offenders)} file(s)). Move the reference to an adjacent "
            "`#` comment or rephrase it out — these docstrings ship to "
            "baldur.sh/reference/.\n" + "\n".join(offenders)
        )

    def test_no_doc_ids_in_published_markdown(self):
        offenders: list[str] = []
        for path in _published_markdown_files():
            text = path.read_text(encoding="utf-8")
            for lineno, line in enumerate(text.splitlines(), start=1):
                ids = _find_doc_ids(line)
                if ids:
                    unique = ", ".join(sorted(set(ids)))
                    offenders.append(f"  {_rel(path)}:{lineno} — {unique}")

        assert not offenders, (
            f"G24: internal doc-IDs found in published markdown "
            f"({len(offenders)} line(s)). Rewrite for an external-reader voice "
            "— these pages ship to baldur.sh.\n" + "\n".join(offenders)
        )

    def test_no_doc_ids_in_mkdocs_nav_metadata(self):
        offenders: list[str] = []
        yaml_text = _MKDOCS_YML.read_text(encoding="utf-8")
        for value in _iter_mkdocs_strings(yaml_text):
            ids = _find_doc_ids(value)
            if ids:
                unique = ", ".join(sorted(set(ids)))
                offenders.append(f"  {value!r} — {unique}")

        assert not offenders, (
            f"G24: internal doc-IDs found in mkdocs.yml rendered metadata "
            f"({len(offenders)} value(s)). nav labels and site_description "
            "render on every page.\n" + "\n".join(offenders)
        )


# --------------------------------------------------------------------------
# Anti-silent-pass unit tests — each extraction path must flag a deliberately
# injected doc-ID (``508 D6`` and the residual forms ``.py:NN`` / bare ``D\d``
# / ``NNN GN`` / ``baldur_dormant.*``), and a clean fixture must not. The
# negative list locks the FP exclusions (``L``-class, ``baldur_pro.*``, ``2D``).
# --------------------------------------------------------------------------

_POSITIVE_DOC_IDS = [
    "508 D6",
    "impl doc 521",
    "doc 450 D2",
    "#497",
    "Wave 6A",
    "ADR-" + "005",  # split via ``+`` so the fixture carries no contiguous ADR ref
    "OOS_INDEX",
    "DEC-12",
    # split via ``+`` (the formatter does not fold it) so this fixture exercises
    # the matcher at runtime without carrying a contiguous private-doc path token.
    "see docs/" + "laws/x.md#g1",
    "../laws/x.md#g16",
    # 553 D2/D3/D4/D5 — the residual forms G24 newly gates.
    "protect.py:99",  # G1 file:line citation
    "factory/base.py:38-41",  # G1 file:line (range)
    "D6",  # bare decision ID
    "per D8",  # bare decision ID, preposition form
    "476 G8",  # qualified Gap item ref
    "463 R1",  # qualified Risk item ref
    "429 C15",  # qualified Choice item ref
    "1023 G1",  # 4-digit doc number (SB-019 999 → 1000 boundary)
    "baldur_dormant.adapters.kafka.consumer.ConsumedEvent",  # private path
]

_NEGATIVE_DOC_IDS = [
    "429",  # HTTP status
    "500 RPS",  # throughput figure
    "2.0.0",  # SemVer
    "PEP 567",  # PEP reference
    "#354150",  # 6-digit CSS hex color (4-digit cap on the # pattern)
    "### Heading 508",  # markdown heading (no anchor)
    "port 6379",  # 4-digit, no anchor
    "the flaws/x problem",  # 'laws/' inside a word — no boundary
    # 553 — FP exclusions the new patterns must NOT flag.
    "post-476 L2-authoritative",  # 'L' excluded from [DGRC] (L1/L2 cache layer)
    "baldur_pro.services.bulkhead",  # baldur_pro.* retained, only dormant gated
    "2D",  # digit-then-D, no leading word boundary
    "MD5",  # hash name, D not at a word boundary
    "0xD4",  # hex literal, D not at a word boundary
    "examples/quickstart_django/settings.py",  # public getting-started path
]


class TestDocIdMatcher:
    """The pure ``_find_doc_ids`` matcher is anchored and FP-free."""

    @pytest.mark.parametrize("text", _POSITIVE_DOC_IDS)
    def test_positive_doc_ids_flagged(self, text: str):
        assert _find_doc_ids(text), f"expected a doc-ID match in {text!r}"

    @pytest.mark.parametrize("text", _NEGATIVE_DOC_IDS)
    def test_negative_doc_ids_clean(self, text: str):
        assert not _find_doc_ids(text), f"unexpected doc-ID match in {text!r}"

    def test_module_allowlist_is_enforced_empty(self):
        """The shipped allowlist starts empty — a leak is cleaned, not baselined."""
        assert _DOC_ID_ALLOWLIST == frozenset()

    def test_allowlisted_token_suppressed_but_others_still_flag(self):
        """An allowlisted hit is dropped; a co-located non-allowlisted leak survives."""
        text = "bare D6 next to D7"
        assert _find_doc_ids(text) == ["D6", "D7"]
        suppressed = _find_doc_ids(text, allowlist=frozenset({"D6"}))
        assert suppressed == ["D7"], (
            "allowlisting 'D6' must drop only that hit, never the D7 leak"
        )


_FIXTURE_WITH_CONVENTIONAL = '''
"""Module summary (508 D6)."""


class Sample:
    def method(self):
        """A method docstring referencing impl doc 521."""
'''

_FIXTURE_WITH_ATTRIBUTE = '''
"""Clean module summary."""

FOO = 1
"""Attribute docstring referencing 490 D4."""
'''

_FIXTURE_CLEAN = '''
"""Clean module summary."""


class Sample:
    BAR = 2
    """A clean attribute docstring."""

    def method(self):
        """A clean method docstring."""
'''

_MKDOCS_FIXTURE_WITH_LEAK = """
site_name: Baldur (508 D6)
site_description: Self-healing layer.
# This comment mentions impl doc 521 and must NOT be yielded.
nav:
  - Home: index.md
  - Audit (Wave 6A): reference/audit.md
"""

_MKDOCS_FIXTURE_CLEAN = """
site_name: Baldur
site_description: Self-healing layer.
# Internal comment: 508 D6 / impl doc 521 — discarded by yaml.safe_load.
nav:
  - Home: index.md
  - Reference: reference/index.md
"""


class TestExtractionPathsAntiSilentPass:
    """Each extraction path flags an injected doc-ID; clean fixtures do not."""

    def test_conventional_docstring_pass_flags(self):
        ids: list[str] = []
        for docstring in _iter_docstrings(_FIXTURE_WITH_CONVENTIONAL):
            ids.extend(_find_doc_ids(docstring))
        assert "508 D6" in ids
        assert "impl doc 521" in ids

    def test_attribute_docstring_pass_flags(self):
        ids: list[str] = []
        for docstring in _iter_docstrings(_FIXTURE_WITH_ATTRIBUTE):
            ids.extend(_find_doc_ids(docstring))
        assert "490 D4" in ids, (
            "the attribute-docstring extraction pass (string Expr after an "
            "assignment) must reach this docstring"
        )

    def test_clean_docstrings_not_flagged(self):
        ids: list[str] = []
        for docstring in _iter_docstrings(_FIXTURE_CLEAN):
            ids.extend(_find_doc_ids(docstring))
        assert not ids

    def test_mkdocs_nav_and_metadata_flags(self):
        ids: list[str] = []
        for value in _iter_mkdocs_strings(_MKDOCS_FIXTURE_WITH_LEAK):
            ids.extend(_find_doc_ids(value))
        assert "508 D6" in ids  # site_name
        assert "Wave 6A" in ids  # nav leaf label

    def test_mkdocs_comments_not_yielded(self):
        # The doc-IDs live only in YAML comments here — safe_load drops them.
        ids: list[str] = []
        for value in _iter_mkdocs_strings(_MKDOCS_FIXTURE_CLEAN):
            ids.extend(_find_doc_ids(value))
        assert not ids, (
            "mkdocs.yml comment doc-IDs must not be yielded — only rendered "
            "string values (site_name / site_description / nav labels)"
        )
