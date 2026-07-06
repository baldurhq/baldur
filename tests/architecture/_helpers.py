"""Shared helpers for architectural fitness function tests.

All helpers are pure (path/string inputs, return values) and trivially unit
testable. Per impl doc 506 D2 they reuse stdlib `ast` +
`pathlib.Path.rglob('*.py')` rather than adding third-party dependencies
(`import-linter` / `tach` rejected).

Per D4 the YAML baseline lives at `tests/architecture/baseline.yaml` and
holds all rules' allowlists under top-level keys. Per impl doc 534 line-level
entries are `{file, symbol, count?, reason, ticket, target_remove_by?}` — the
match key is the drift-stable enclosing `symbol` (CPython `__qualname__`
semantics) and `count` (default 1) is the allowed-occurrence threshold; an
entry with no `symbol:` allowlists the whole file for that rule (infinite
allowance).

Per D10 every fitness test that emits a failure SHOULD format it with
`format_violation()` so CI logs surface a one-click link to the rule rationale
in `ARCHITECTURE.md`.

This module is imported by `conftest.py` (which re-exports the public API).
Test files MAY import from either location; `_helpers` is the canonical home
for monkeypatching targets (e.g., `_helpers.BASELINE_PATH`,
`_helpers._load_baseline_document`).
"""

from __future__ import annotations

import ast
import importlib
import importlib.util
import inspect
import re
import tomllib
from collections import defaultdict
from collections.abc import Iterable, Iterator
from functools import lru_cache
from pathlib import Path
from typing import Any, NamedTuple

import yaml


def _locate_project_root() -> Path:
    """Climb from this file to the nearest ancestor holding ``pyproject.toml``.

    Layout-agnostic: ``tests/architecture/`` in the monorepo (root four
    levels up) and ``tests/architecture/`` in the published mirror (root three
    levels up, after the ``tests/`` -> ``tests/`` rename) both resolve
    correctly. Replaces a fixed ``parents[3]``, which in the renamed mirror
    climbed one level too high — making ``PROJECT_ROOT/"src"/"baldur"``
    misresolve and silently vacuous-passing every ``architecture/`` gate.
    ``pyproject.toml`` ships to the mirror (it is in the publish allowlist), so
    the marker is present in both layouts.
    """
    here = Path(__file__).resolve()
    for candidate in here.parents:
        if (candidate / "pyproject.toml").is_file():
            return candidate
    # Defensive fallback to the monorepo depth when no marker is found.
    return here.parents[3]


PROJECT_ROOT = _locate_project_root()
# The OSS test root, resolved relative to this module (which lives in
# ``architecture/``): ``tests/oss`` in the monorepo, ``tests`` in the mirror
# after ``--path-rename tests/:tests/``. The three PRO-leak gates
# (G19/G20/G21) walk this instead of the hardcoded ``PROJECT_ROOT/"tests"/"oss"``
# so they scan a non-empty file set in BOTH layouts (no vacuous pass).
OSS_TESTS_ROOT = Path(__file__).resolve().parents[1]
BASELINE_PATH = Path(__file__).resolve().parent / "baseline.yaml"
DEFAULT_SRC_ROOTS: tuple[Path, ...] = (
    PROJECT_ROOT / "src" / "baldur",
    PROJECT_ROOT / "src" / "baldur_pro",
)
REFERENCE_DIR = PROJECT_ROOT / "docs" / "reference"
RULE_REGISTRY_DOC = "https://github.com/gotoUSA/baldur-python/blob/main/ARCHITECTURE.md"


class _MkdocsSafeLoader(yaml.SafeLoader):
    """SafeLoader that tolerates mkdocs ``!!python/name:`` tags.

    ``mkdocs.yml`` carries a ``pymdownx.superfences`` ``custom_fences`` entry
    whose ``format:`` is a ``!!python/name:...`` tag (the Mermaid fence
    renderer). Plain ``yaml.safe_load`` raises ``ConstructorError`` on that
    unknown tag. The architecture gates that parse ``mkdocs.yml`` only read
    ``nav`` / ``llmstxt`` text and never touch the fence config, so the tag's
    target object is irrelevant — resolve it to its dotted-path string instead
    of importing the (possibly absent) callable. Subclassing keeps the global
    ``yaml.SafeLoader`` untouched.
    """


def _construct_python_name(
    loader: yaml.SafeLoader, suffix: str, node: yaml.Node
) -> str:
    """Resolve a ``!!python/name:dotted.path`` tag to its dotted-path string."""
    return suffix


_MkdocsSafeLoader.add_multi_constructor(
    "tag:yaml.org,2002:python/name:", _construct_python_name
)


def mkdocs_safe_load(yaml_text: str) -> Any:
    """``yaml.safe_load`` for ``mkdocs.yml``, tolerating ``!!python/name:`` tags.

    A drop-in for ``yaml.safe_load`` that does not choke on the Mermaid
    ``custom_fences`` ``format:`` tag. Use this for any gate that parses
    ``mkdocs.yml``.
    """
    return yaml.load(yaml_text, Loader=_MkdocsSafeLoader)


def _to_posix(path: Path) -> str:
    """Convert a path to a POSIX-style string relative to PROJECT_ROOT."""
    try:
        rel = path.resolve().relative_to(PROJECT_ROOT)
    except ValueError:
        rel = path
    return rel.as_posix()


def walk_src(
    roots: Iterable[Path] = DEFAULT_SRC_ROOTS,
    *,
    exclude_underscore: bool = False,
    exclude_init: bool = False,
) -> Iterator[Path]:
    """Iterate `.py` files under the given source roots.

    Args:
        roots: Iterable of directory or single-file roots to walk. A root that
            points directly at a ``.py`` file is yielded as-is (subject to the
            filters below) — ``rglob`` returns nothing for a file path, so file
            roots must be handled explicitly.
        exclude_underscore: When True, skip modules whose filename starts with
            ``_`` (private helpers). ``__init__.py`` is controlled separately.
        exclude_init: When True, skip ``__init__.py`` files.
    """

    def _passes_filters(name: str) -> bool:
        if exclude_init and name == "__init__.py":
            return False
        if exclude_underscore and name.startswith("_") and name != "__init__.py":
            return False
        return True

    for root in roots:
        if not root.exists():
            continue
        if root.is_file():
            if root.suffix == ".py" and _passes_filters(root.name):
                yield root
            continue
        for path in root.rglob("*.py"):
            if _passes_filters(path.name):
                yield path


@lru_cache(maxsize=4096)
def parse_ast(path: Path) -> ast.Module | None:
    """Parse a Python source file into an AST module, or return None on failure.

    Cached so multiple rule tests parsing the same file pay the cost once.
    """
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    try:
        return ast.parse(source, filename=str(path))
    except SyntaxError:
        return None


MODULE_SYMBOL = "<module>"

# A qualname scope is opened by exactly these three AST node types (plus the
# module root). AsyncFunctionDef is a DISTINCT node type from FunctionDef —
# omitting it would give async methods/coroutines the wrong enclosing symbol.
_SCOPE_OPENER_TYPES: tuple[type[ast.AST], ...] = (
    ast.FunctionDef,
    ast.AsyncFunctionDef,
    ast.ClassDef,
)


@lru_cache(maxsize=4096)
def _symbol_index(tree: ast.Module) -> dict[int, str]:
    """Map ``id(node)`` -> qualified-scope name for every node in ``tree``.

    Built by a single recursive scope-stack descent following CPython
    ``__qualname__`` semantics:

    - module-level statement -> ``"<module>"`` (``MODULE_SYMBOL``);
    - module-level def -> bare name; method -> ``Class.method``;
    - nested class -> ``Outer.Inner``; function-nested def ->
      ``outer.<locals>.inner``.

    For a node that IS a scope opener (def / async def / class) the index
    stores its OWN qualname; for every other node it stores its ENCLOSING
    scope qualname. ``symbol_of`` relies on this so Category A (pass the def)
    and Category B (pass the Call / Import) are both correct with one lookup.

    Scope transparency (load-bearing): everything NOT in ``_SCOPE_OPENER_TYPES``
    is transparent — compound statements (``try`` / ``if`` incl.
    ``if TYPE_CHECKING:`` / ``for`` / ``while`` / ``with``) AND the
    implicit-scope expressions (``lambda``, comprehensions). A violation nested
    inside any of them converges to the nearest named ``def`` / ``class`` (a
    stable key) rather than to an unnamed ``<lambda>`` / ``<listcomp>``. The
    descent visits every node (recursive ``ast.iter_child_nodes``) because
    Category B scanners pass arbitrary nodes found via ``ast.walk``; a node
    missing from the index is a bug — ``symbol_of`` defensively defaults to
    ``MODULE_SYMBOL``.

    Cached on the tree object (the same object returned by the already-cached
    ``parse_ast``) so multiple rules scanning one file share the index.
    """
    index: dict[int, str] = {}

    def descend(node: ast.AST, enclosing: str, def_prefix: str) -> None:
        if isinstance(node, _SCOPE_OPENER_TYPES):
            own = def_prefix + node.name  # type: ignore[attr-defined]
            index[id(node)] = own
            if isinstance(node, ast.ClassDef):
                child_enclosing, child_prefix = own, own + "."
            else:
                child_enclosing, child_prefix = own, own + ".<locals>."
            for child in ast.iter_child_nodes(node):
                descend(child, child_enclosing, child_prefix)
        else:
            index[id(node)] = enclosing
            for child in ast.iter_child_nodes(node):
                descend(child, enclosing, def_prefix)

    index[id(tree)] = MODULE_SYMBOL
    for child in ast.iter_child_nodes(tree):
        descend(child, MODULE_SYMBOL, "")
    return index


def symbol_of(tree: ast.Module, node: ast.AST) -> str:
    """Return the qualified-scope name for ``node`` within ``tree``.

    When ``node`` IS a def / class scope node, returns its own qualname;
    otherwise returns its enclosing scope qualname (``MODULE_SYMBOL`` at module
    level). Defends against an unindexed node by defaulting to ``MODULE_SYMBOL``.
    """
    return _symbol_index(tree).get(id(node), MODULE_SYMBOL)


@lru_cache(maxsize=1)
def _load_baseline_document() -> dict[str, Any]:
    if not BASELINE_PATH.exists():
        return {}
    with BASELINE_PATH.open(encoding="utf-8") as handle:
        loaded = yaml.safe_load(handle) or {}
    if not isinstance(loaded, dict):
        return {}
    return loaded


def load_baseline(rule_key: str) -> dict[tuple[str, str | None], int]:
    """Return the baseline allowlist for a rule as ``{(file, symbol|None): count}``.

    A symbol entry ``{file, symbol, count?}`` maps ``(file_posix, symbol)`` to
    its allowed-occurrence ``count`` (default 1 when ``count:`` is omitted, so
    every singleton symbol keeps blind-spot protection for free). A whole-file
    entry ``{file}`` (no ``symbol:``) maps ``(file_posix, None)`` and means an
    *infinite* allowance, exactly as the pre-534 ``line=None`` / file-level
    entry did; the rule visitor short-circuits on membership of
    ``(file, None)`` and never reads its count.
    """
    document = _load_baseline_document()
    entries = document.get(rule_key) or []
    result: dict[tuple[str, str | None], int] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        file_value = entry.get("file")
        if not isinstance(file_value, str):
            continue
        file_posix = file_value.replace("\\", "/")
        symbol_value = entry.get("symbol")
        if not isinstance(symbol_value, str):
            # Whole-file waiver (infinite allowance; the count is never read).
            result[(file_posix, None)] = 1
            continue
        count_value = entry.get("count")
        count = count_value if isinstance(count_value, int) and count_value > 0 else 1
        result[(file_posix, symbol_value)] = count
    return result


def baselined_count(
    file_posix: str,
    symbol: str | None,
    baseline: dict[tuple[str, str | None], int],
) -> int:
    """Return the allowed-occurrence count for a ``(file, symbol)`` pair.

    Zero when the pair is absent (a brand-new symbol regresses on its first
    occurrence). Whole-file waivers are not represented here — callers check
    ``(file_posix, None) in baseline`` for the infinite-allowance short-circuit.
    """
    return baseline.get((file_posix, symbol), 0)


def resolve_callsites(
    roots: Iterable[Path],
    target_names: Iterable[str],
) -> set[str]:
    """Return the subset of ``target_names`` invoked anywhere under ``roots``.

    Walks `.py` files under the roots, follows ``from ... import name as alias``
    bindings, and collects ``ast.Call`` references that resolve back to a
    target name (directly or through a local alias).

    Per D5 this catches aliased imports that grep misses, but only static
    ``ast.Call`` invocations — dynamic dispatch (`getattr(...)()`, registry
    walks, list-of-functions iteration) is NOT detected and is documented as
    a known limitation in the rule registry.
    """
    targets = set(target_names)
    invoked: set[str] = set()
    for path in walk_src(roots):
        tree = parse_ast(path)
        if tree is None:
            continue
        alias_to_target: dict[str, str] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    if alias.name in targets:
                        local_name = alias.asname or alias.name
                        alias_to_target[local_name] = alias.name
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Name):
                if func.id in alias_to_target:
                    invoked.add(alias_to_target[func.id])
                elif func.id in targets:
                    invoked.add(func.id)
            elif isinstance(func, ast.Attribute):
                if func.attr in targets:
                    invoked.add(func.attr)
    return invoked


def _normalize_distribution_name(spec: str) -> str:
    """Extract the distribution name from a `[project.optional-dependencies]` spec."""
    name = spec.strip().split(";", 1)[0].strip()
    for separator in ("[", "<", ">", "=", "!", "~", " "):
        cut = name.find(separator)
        if cut >= 0:
            name = name[:cut]
    return name.strip().lower()


_DIST_TO_MODULE_OVERRIDES: dict[str, str] = {
    "djangorestframework": "rest_framework",
    "djangorestframework-simplejwt": "rest_framework_simplejwt",
    "django-db-connection-pool": "dj_db_conn_pool",
    "django-redis": "django_redis",
    "psycopg2-binary": "psycopg2",
    "pyyaml": "yaml",
    "confluent-kafka": "confluent_kafka",
    "scikit-learn": "sklearn",
    "factory-boy": "factory",
    "prometheus-client": "prometheus_client",
    "pymemcache": "pymemcache",
    "rq-scheduler": "rq_scheduler",
}


# Self-distribution names of the OSS package. The PyPI distribution was
# renamed ``baldur`` -> ``baldur-framework`` in 531 D8 (importable package name
# stays ``baldur``); both are matched so recursive extras keep flattening if
# ``baldur`` is later published as a forward-compat alias.
_SELF_DISTRIBUTION_NAMES: frozenset[str] = frozenset({"baldur", "baldur-framework"})


def _distribution_to_module(distribution: str) -> str | None:
    """Map a PyPI distribution name to its top-level import module.

    Returns None for self-references such as ``"baldur-framework"`` (the package
    itself referenced by recursive extras like
    ``all = ["baldur-framework[...]"]``).
    """
    canonical = distribution.lower()
    if not canonical or canonical in _SELF_DISTRIBUTION_NAMES:
        return None
    if canonical in _DIST_TO_MODULE_OVERRIDES:
        return _DIST_TO_MODULE_OVERRIDES[canonical]
    return canonical.replace("-", "_")


@lru_cache(maxsize=1)
def _pyproject_data() -> dict[str, Any]:
    pyproject = PROJECT_ROOT / "pyproject.toml"
    if not pyproject.exists():
        return {}
    with pyproject.open("rb") as handle:
        return tomllib.load(handle)


@lru_cache(maxsize=1)
def core_dependency_modules() -> frozenset[str]:
    """Return the top-level modules from ``[project.dependencies]``.

    These are always installed (not optional); the optional-imports fitness
    rule subtracts this set to avoid false positives when a package is listed
    in both ``[project.dependencies]`` and ``[project.optional-dependencies]``
    (e.g., ``redis`` is core but also re-listed under ``[celery]``).
    """
    data = _pyproject_data()
    deps = (data.get("project") or {}).get("dependencies") or []
    modules: set[str] = set()
    for spec in deps:
        if not isinstance(spec, str):
            continue
        distribution = _normalize_distribution_name(spec)
        module = _distribution_to_module(distribution)
        if module:
            modules.add(module)
    return frozenset(modules)


@lru_cache(maxsize=1)
def optional_extras_modules() -> dict[str, frozenset[str]]:
    """Return ``{extra_name: {top_level_module, ...}}`` from pyproject.toml.

    Reads ``[project.optional-dependencies]`` via stdlib ``tomllib`` (Python
    3.11+ per pyproject ``requires-python``). Recursive extras such as
    ``baldur-framework[ml]`` are flattened across the chain. Modules also present in
    ``[project.dependencies]`` are excluded so each entry truly represents an
    optional-only dependency.
    """
    raw = (_pyproject_data().get("project") or {}).get("optional-dependencies") or {}
    raw_specs: dict[str, list[str]] = {
        name: list(specs) for name, specs in raw.items() if isinstance(specs, list)
    }
    core_modules = core_dependency_modules()

    resolved: dict[str, frozenset[str]] = {}

    def _resolve(extra: str, seen: frozenset[str]) -> set[str]:
        if extra in resolved:
            return set(resolved[extra])
        if extra in seen:
            return set()
        seen = seen | {extra}
        modules: set[str] = set()
        for spec in raw_specs.get(extra, []):
            distribution = _normalize_distribution_name(spec)
            if distribution in _SELF_DISTRIBUTION_NAMES:
                bracket_start = spec.find("[")
                bracket_end = spec.find("]", bracket_start + 1)
                if bracket_start >= 0 and bracket_end > bracket_start:
                    inner = spec[bracket_start + 1 : bracket_end]
                    for nested in inner.split(","):
                        nested = nested.strip()
                        if nested:
                            modules.update(_resolve(nested, seen))
                continue
            module = _distribution_to_module(distribution)
            if module:
                modules.add(module)
        return modules

    for extra in raw_specs:
        resolved_modules = _resolve(extra, frozenset())
        resolved[extra] = frozenset(resolved_modules - core_modules)
    return resolved


def format_violation(
    rule_anchor: str,
    file: Path | str,
    line: int | None,
    extra: str | None = None,
) -> str:
    """Format a single violation line for inclusion in a test failure message.

    The resulting string includes a link to the rule heading in
    ``ARCHITECTURE.md`` so CI log readers can
    jump straight to the rationale.
    """
    if isinstance(file, Path):
        file_str = _to_posix(file)
    else:
        file_str = file.replace("\\", "/")
    location = f"{file_str}:{line}" if line is not None else file_str
    anchor = rule_anchor if rule_anchor.startswith("#") else f"#{rule_anchor}"
    rule_link = f"{RULE_REGISTRY_DOC}{anchor}"
    suffix = f" — {extra}" if extra else ""
    return f"  {location}{suffix}  [{rule_link}]"


def collect_violations(
    rule_key: str,
    raw_violations: Iterable[tuple[Path, int | None, str | None, str | None]],
    rule_anchor: str,
) -> list[str]:
    """Helper: filter raw `(path, line, symbol, extra)` violations by the baseline.

    Groups live violations by ``(file, symbol)`` and emits a regression only
    when a group's observed occurrence count exceeds the baselined ``count``
    threshold (impl doc 534 D1/D4). A whole-file waiver (``(file, None)``
    present) short-circuits to "skip all", exactly as the pre-534 file-level
    entry did. The 8 file-level / meta rules pass ``symbol=None`` for every
    violation, so each file collapses to the single key ``(file, None)`` and
    behaves identically to the pre-534 whole-file check.

    Returns the sorted list of pre-formatted violation strings that regress.
    The displayed ``file:line`` comes from the *live* scan, so it can never go
    stale even though matching no longer keys on ``line``.
    """
    baseline = load_baseline(rule_key)
    observed: dict[tuple[str, str | None], list[tuple[int | None, str | None]]] = (
        defaultdict(list)
    )
    for path, line, symbol, extra in raw_violations:
        file_posix = _to_posix(path)
        if (file_posix, None) in baseline:  # whole-file waiver (infinite)
            continue
        observed[(file_posix, symbol)].append((line, extra))

    formatted: list[str] = []
    for (file_posix, symbol), occurrences in observed.items():
        if len(occurrences) <= baseline.get((file_posix, symbol), 0):
            continue
        for line, extra in occurrences:
            formatted.append(format_violation(rule_anchor, file_posix, line, extra))
    formatted.sort()
    return formatted


# ``:::`` mkdocstrings autodoc directive, e.g. ``::: baldur.interfaces`` or
# ``::: baldur.decorators.dlq_protect.dlq_protect``. Anchored at line start so
# fenced-code or prose mentions of ``:::`` are not matched.
_DIRECTIVE_RE = re.compile(r"^:::\s+(\S+)")


def directive_targets(reference_dir: Path = REFERENCE_DIR) -> Iterator[str]:
    """Yield every ``:::`` autodoc target across the reference ``.md`` set.

    Recursive (``rglob``) so themed sub-directory pages (e.g.
    ``reference/interfaces/repositories.md``) are covered without per-page
    registration. Shared by the doc-ID-leak resolver (G24) and the
    reference-completeness rule (G25) so the two never drift on directive
    parsing — the single canonical parser for the published reference surface.
    """
    for path in sorted(reference_dir.rglob("*.md")):
        for line in path.read_text(encoding="utf-8").splitlines():
            match = _DIRECTIVE_RE.match(line)
            if match:
                yield match.group(1)


# ---------------------------------------------------------------------------
# Published-reference source-surface primitives (shared by G23/G24/G26/G27).
#
# mkdocstrings renders the docstrings of every symbol re-exported by a
# rendered package's ``__all__`` PLUS the package's own ``__init__`` module
# docstring. These primitives resolve that source set and scan it for the two
# things that must never reach the published site: Korean prose (CLAUDE.md
# § Code Language Rules) and internal doc-IDs (the G24 leak class).
#
# They are kept GENERIC here — ``resolve_all_chain_files`` takes the package
# tuple + src root as PARAMETERS and this module never names or statically
# imports a private (``baldur_pro`` / ``baldur_dormant``) package — so the
# file stays mirror-safe and OSS-collectable while a private gate under
# ``tests/pro/architecture/`` injects its own ``baldur_pro.services.*`` tuple.
# ---------------------------------------------------------------------------

# Hangul syllables + Jamo. ``[가-힣]`` plus the compatibility/conjoining Jamo
# blocks for defense in depth.
KOREAN_RE = re.compile(r"[가-힣ᄀ-ᇿ㄰-㆏]")

# All patterns but one carry a DISTINCTIVE anchor (a doc-number prefix, an
# ``ADR-`` / ``DEC-`` literal, a ``.py:`` / ``baldur_dormant.`` token, etc.),
# so HTTP codes (``429`` / ``500``), RPS figures (``500 RPS``), SemVer
# (``2.0.0``), ``PEP 567`` and 6-digit CSS hex (``#354150``, excluded by the
# ``#\d{3,4}`` 4-digit cap) cannot match. The one exception is bare ``D\d``
# (``D6`` / ``per D8``): it has no anchor, so a future legitimate
# ``D\d``-like token is suppressed via the reviewed, enforced-empty
# ``DOC_ID_ALLOWLIST`` rather than a pattern carve-out. The doc-number width
# is ``\d{3,}`` (not ``\d{3}``) so the gate survives the 999 → 1000 impl-doc
# boundary.
DOC_ID_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b\d{3,}\s+[DGRC]\d+\b"),  # 508 D6 / 476 G8 / 463 R1 / 429 C15
    re.compile(r"impl doc \d{3,}"),  # impl doc 521
    re.compile(r"\bdoc \d{3,}\b"),  # doc 450
    re.compile(r"(?:docs/|\.\./)?\b(?:impl|laws|self_healing)/\S+"),  # path refs
    re.compile(r"\bADR-\d+\b"),  # architecture-decision ref
    re.compile(r"\bOOS(?:_INDEX)?\b"),  # OOS / OOS_INDEX
    re.compile(r"\bWave \d+[A-Z]?\b"),  # Wave 6A
    re.compile(r"\bDEC-\d+\b"),  # DEC-123
    re.compile(r"#\d{3,4}\b"),  # #497 issue refs (4-cap excludes 6-digit hex)
    re.compile(r"\.py:\d+"),  # protect.py:99 file:line citations
    re.compile(r"\bbaldur_dormant\.[\w.]+"),  # private-distribution dotted paths
    re.compile(r"\bD\d{1,2}\b"),  # bare D6 / per D8 (allowlist-gated)
)

# Reviewed escape hatch for the one non-anchored pattern (bare ``D\d``):
# enforced-empty, so a real leak is cleaned, never allowlisted. Only a
# confirmed false positive is ever added here, as a reviewed constant.
DOC_ID_ALLOWLIST: frozenset[str] = frozenset()


def find_doc_ids(text: str, allowlist: frozenset[str] = DOC_ID_ALLOWLIST) -> list[str]:
    """Return every internal-doc-ID substring in ``text`` (pure function).

    Hits whose exact text is in ``allowlist`` are dropped — the reviewed
    escape hatch for the single non-anchored pattern (bare ``D\\d``). The
    parameter is injectable so a test can confirm suppression without
    mutating the module-level enforced-empty constant.
    """
    hits: list[str] = []
    for pattern in DOC_ID_PATTERNS:
        hits.extend(match.group(0) for match in pattern.finditer(text))
    return [hit for hit in hits if hit not in allowlist]


# ---------------------------------------------------------------------------
# Concept-guide published-prose leak matcher (G29).
#
# Composes find_doc_ids() (which already covers internal doc-IDs, ``.py:NN``
# file:line citations, and ``baldur_dormant.*`` paths) and ADDS the three
# concept-guide-prose leak classes find_doc_ids does not flag:
#
#   ③-b  ``baldur_pro.*`` private-distribution dotted paths;
#   ④-a-dotted  a ``baldur.``-rooted dotted path with a private ``._segment``;
#   ④-a-bare  a bare, unqualified private ``_name`` inside an INLINE code-span
#             (dunder-excluded — ``__x__`` is public protocol).
#
# DOC_ID_PATTERNS is deliberately NEVER mutated — the shared matcher's negative
# suite pins ``baldur_pro`` non-retention there (G24 asserts
# ``baldur_pro.services.bulkhead`` is NOT a doc-ID), so ``baldur_pro`` detection
# is additive and local to the prose surface here. Like the existing
# ``baldur_dormant`` literal above, every additive pattern is a regex STRING
# (not an import), so this file stays mirror-safe and OSS-collectable.
#
# The ④-b class (a public-looking internal symbol — no underscore, not in
# ``__all__``) is intentionally NOT mechanized: a zero-false-positive gate
# cannot distinguish it from public symbols / env vars / literals without an
# FP-heavy ``__all__`` cross-reference, so the verification skill owns it as a
# quote-and-match judgment. find_prose_leaks must not attempt ④-b.
# ---------------------------------------------------------------------------

# ③-b — ``baldur_pro.*`` dotted paths. Mirrors the ``baldur_dormant.*`` literal
# in DOC_ID_PATTERNS; ``pip install baldur-pro`` (a hyphen, no trailing ``.``)
# does not match.
_BALDUR_PRO_PATH_RE = re.compile(r"\bbaldur_pro\.[\w.]+")

# ④-a-dotted — a ``baldur.``-rooted dotted path carrying a private ``._segment``
# (e.g. ``baldur.adaptive._helpers``). The ``baldur.`` root keeps it
# false-positive-free: a bare ``x._y`` in prose or math never matches, and a
# public path (``baldur.core.exceptions``, ``baldur.sh``, ``@baldur.protected``)
# carries no ``._`` segment. The lazy ``*?`` finds the first private segment
# with minimal backtracking. ``baldur_pro.*`` is NOT matched here (it has no
# ``.`` immediately after ``baldur``) — ③-b owns it.
_BALDUR_PRIVATE_SEGMENT_RE = re.compile(r"\bbaldur(?:\.\w+)*?\._\w+")

# ④-a-bare — a bare, unqualified private symbol: a single leading underscore
# NOT preceded by a word char or a dot (so an attribute access ``self._state``
# and an env var ``BALDUR_X`` are excluded) and NOT a dunder (the ``(?!_)``
# rejects ``__x__`` / ``__init__``, which are public protocol). Run ONLY inside
# inline code-spans (see find_prose_leaks) — a bare unqualified private symbol
# in a fenced OSS example is the ④-b residual the verification skill owns, not
# a mechanically-decidable leak.
_PRIVATE_BARE_SYMBOL_RE = re.compile(r"(?<![\w.])_(?!_)\w+")

# Inline code-span content. Newline-excluded so a fenced ``` block — whose
# fences sit on their own lines — is never consumed, and a markdown-italic
# ``_x_`` (no backticks) is never extracted.
_INLINE_CODE_SPAN_RE = re.compile(r"`([^`\n]+)`")


def iter_inline_code_spans(text: str) -> Iterator[str]:
    r"""Yield the content of every inline code-span (`` `…` ``) in ``text``.

    Backtick-delimited and single-line by construction (the ``[^`\n]+`` body
    excludes newlines), so a fenced ```` ``` ```` block — whose fences sit on
    their own lines — is never consumed, and a markdown-italic ``_x_`` (no
    backticks) is never extracted. Pure function; the backticks are stripped
    from each yielded span.
    """
    for match in _INLINE_CODE_SPAN_RE.finditer(text):
        yield match.group(1)


def find_prose_leaks(text: str) -> list[str]:
    """Return every published-prose leak substring in ``text`` (pure function).

    Composes ``find_doc_ids`` (internal doc-IDs, ``.py:NN`` file:line citations,
    ``baldur_dormant.*`` paths) with three concept-guide-prose leak classes:

    * ``baldur_pro.*`` private-distribution paths (③-b),
    * ``baldur.``-rooted dotted paths with a private ``._segment`` tail
      (④-a-dotted),
    * a bare, unqualified private ``_name`` inside an inline code-span,
      dunder-excluded (④-a-bare).

    The doc-ID / ``baldur_pro`` / ``baldur.``-rooted rules are single-line and
    run over the whole ``text`` (so a leak inside a fenced block or an HTML
    comment is still caught when the gate scans that raw line); the bare-symbol
    rule runs ONLY inside inline code-spans. The public-looking internal symbol
    class (④-b) is intentionally NOT attempted — it is owned by the verification
    skill as a quote-and-match judgment, because mechanizing it would reintroduce
    a false-positive class indistinguishable from public symbols. ``DOC_ID_PATTERNS``
    is never mutated.
    """
    hits = find_doc_ids(text)
    for pattern in (_BALDUR_PRO_PATH_RE, _BALDUR_PRIVATE_SEGMENT_RE):
        hits.extend(match.group(0) for match in pattern.finditer(text))
    for span in iter_inline_code_spans(text):
        hits.extend(match.group(0) for match in _PRIVATE_BARE_SYMBOL_RE.finditer(span))
    return hits


def iter_docstrings(source_text: str) -> Iterator[str]:
    """Yield every docstring in ``source_text`` (pure function).

    Two passes, both pure-stdlib ``ast`` (no griffe dependency):

    1. **Conventional docstrings** — ``ast.get_docstring`` on the module and
       on every ``ClassDef`` / ``FunctionDef`` / ``AsyncFunctionDef`` node
       reached by ``ast.walk`` (covers nested methods, including an
       ``__init__`` merged into its class by ``merge_init_into_class``).
    2. **Attribute docstrings** — the bare string-literal ``Expr`` statement
       immediately following an ``Assign`` / ``AnnAssign`` in a module or
       class body. ``ast.get_docstring`` structurally cannot reach these, yet
       griffe (``show_docstring_attributes`` default-on) renders them.
    """
    try:
        tree = ast.parse(source_text)
    except SyntaxError:
        return

    # Pass 1 — conventional docstrings.
    for node in ast.walk(tree):
        if isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            doc = ast.get_docstring(node, clean=False)
            if doc:
                yield doc

    # Pass 2 — attribute docstrings (module body + every class body).
    scopes: list[ast.AST] = [tree]
    scopes.extend(n for n in ast.walk(tree) if isinstance(n, ast.ClassDef))
    for scope in scopes:
        body = getattr(scope, "body", None)
        if not isinstance(body, list):
            continue
        for index, statement in enumerate(body):
            if index == 0:
                continue  # the leading string is the conventional docstring
            if (
                isinstance(statement, ast.Expr)
                and isinstance(statement.value, ast.Constant)
                and isinstance(statement.value.value, str)
                and isinstance(body[index - 1], (ast.Assign, ast.AnnAssign))
            ):
                yield statement.value.value


def resolve_all_chain_files(
    packages: Iterable[str],
    src_root: Path,
) -> set[Path]:
    """Resolve the published-reference source files for ``packages``.

    Mirrors mkdocstrings reachability for a whole-package ``:::`` directive:
    each package contributes its OWN ``__init__`` module file (so the package
    module docstring is scanned) PLUS the defining file of every ``__all__``
    re-export (resolved via ``obj.__module__``). Files outside ``src_root``
    (stdlib / third-party / cross-package re-exports) are dropped.

    Generic by construction: ``packages`` and ``src_root`` are injected, so
    this helper names no specific (public or private) package and stays
    mirror-safe. Callers supply their own rendered-package tuple and root.
    """
    files: set[Path] = set()
    for package_name in packages:
        try:
            package = importlib.import_module(package_name)
        except Exception:
            continue
        # The package's own ``__init__`` module file — its module docstring is
        # rendered by the whole-package directive even though it defines no
        # ``__all__`` symbol of its own.
        own = getattr(package, "__file__", None)
        if own:
            own_path = Path(own).resolve()
            if src_root in own_path.parents:
                files.add(own_path)
        for symbol_name in getattr(package, "__all__", []):
            obj = getattr(package, symbol_name, None)
            if obj is None:
                continue
            module_name = getattr(obj, "__module__", None)
            module = (
                importlib.import_module(module_name)
                if module_name
                else inspect.getmodule(obj)
            )
            source = getattr(module, "__file__", None) if module else None
            if not source:
                continue
            path = Path(source).resolve()
            if src_root in path.parents:
                files.add(path)
    return files


# ---------------------------------------------------------------------------
# Enable-shape settings-field enumeration (shared by G18 + G32, impl doc 575 D3).
#
# Reflection over `BaseSettings.__subclasses__()` filtered to `baldur.settings.`,
# recursing into nested `BaseModel` sub-configs, force-loading every settings
# submodule via `pkgutil` so the enumeration does not depend on
# `settings/__init__.py` re-exports. G18 (`test_v1_default_enable.py`) consumes
# the whole set; G32 (`test_flag_consumer_reachability.py`) consumes the
# long-form subset. Single source of truth — no drift. Pydantic is imported
# lazily inside `discover_enable_fields()` so this module stays light to import.
# ---------------------------------------------------------------------------

_ENV_NESTED_DELIMITER = "__"


class EnableField(NamedTuple):
    """One discovered enable-shape boolean settings field.

    `module` is the leaf module filename (e.g. ``graceful_degradation.py``) —
    the key G18 matches against ``V1_LAUNCH_MANIFEST.yaml``. `source_file` is
    the POSIX path of the owning settings class's module relative to the project
    root — the additional field G32 keys its ``baseline.yaml`` entries on
    (``symbol = "ClassName.field"``), reflection-derivable unlike a line number.
    """

    module: str
    cls: str
    field: str
    default: Any
    env_var: str
    source_file: str


def _is_enable_shape(field_name: str) -> bool:
    """True for the bare ``enabled`` master toggle plus every long-form variant."""
    return (
        field_name == "enabled"
        or "_enabled" in field_name
        or field_name.startswith("enable_")
    )


def is_long_form_enable_field(field_name: str) -> bool:
    """True for ``*_enabled`` / ``enable_*`` fields, excluding bare ``enabled`` (D4-bare).

    G32 classifies only long-form names: the bare ``enabled`` shape collides
    with every settings class's master toggle AND every unrelated ``.enabled``
    attribute, so attribute-name matching cannot isolate a single field's reads.
    The bare shape is routed to the periodic claim-wiring audit instead.
    """
    return field_name != "enabled" and (
        "_enabled" in field_name or field_name.startswith("enable_")
    )


def _ensure_settings_loaded() -> None:
    """Force-load every ``baldur.settings.<module>`` so all subclasses register.

    ``baldur.settings.__init__`` re-exports many but not all settings modules,
    so the package import alone misses ~70 classes. Walking with
    ``pkgutil.iter_modules`` guarantees every ``.py`` file is imported.
    """
    import pkgutil

    package = importlib.import_module("baldur.settings")
    for module_info in pkgutil.iter_modules(package.__path__):
        if module_info.ispkg:
            continue
        importlib.import_module(f"{package.__name__}.{module_info.name}")


def _iter_settings_subclasses(root: type) -> set[type]:
    seen: set[type] = set()
    stack = [root]
    while stack:
        cls = stack.pop()
        for sub in cls.__subclasses__():
            if sub in seen:
                continue
            seen.add(sub)
            stack.append(sub)
    return seen


def _settings_env_prefix(cls: type) -> str:
    cfg = getattr(cls, "model_config", None)
    if not cfg:
        return ""
    prefix = cfg.get("env_prefix") if isinstance(cfg, dict) else None
    return prefix if isinstance(prefix, str) else ""


def _settings_module_filename(cls: type) -> str:
    module_name = cls.__module__ or ""
    leaf = module_name.rsplit(".", 1)[-1]
    return f"{leaf}.py"


def _settings_source_file(cls: type) -> str:
    import sys

    try:
        source = inspect.getsourcefile(cls)
    except TypeError:
        source = None
    if not source:
        module = sys.modules.get(cls.__module__)
        source = getattr(module, "__file__", None)
    return _to_posix(Path(source)) if source else ""


def discover_enable_fields() -> list[EnableField]:
    """Enumerate every enable-shape boolean field under ``baldur.settings.*``.

    Reflection-based and parameterless (walks the live ``BaseSettings``
    subclass registry, recursing into nested ``BaseModel`` sub-configs). Pydantic
    is imported lazily here so importing this module stays cheap. Returned sorted
    by ``(module, cls, field)`` for stable iteration.
    """
    from pydantic import BaseModel
    from pydantic_settings import BaseSettings

    _ensure_settings_loaded()

    discovered: dict[tuple[str, str, str], EnableField] = {}

    def walk(
        cls: type[BaseModel],
        env_prefix: str,
        module_filename: str,
        source_file: str,
        nested_path: tuple[str, ...],
    ) -> None:
        for field_name, field_info in cls.model_fields.items():
            annotation = field_info.annotation
            if annotation is bool:
                if not _is_enable_shape(field_name):
                    continue
                alias = field_info.validation_alias
                if isinstance(alias, str):
                    env_var = alias.upper()
                else:
                    parts = nested_path + (field_name,)
                    env_var = (env_prefix + _ENV_NESTED_DELIMITER.join(parts)).upper()
                key = (module_filename, cls.__name__, field_name)
                if key in discovered:
                    continue
                discovered[key] = EnableField(
                    module=module_filename,
                    cls=cls.__name__,
                    field=field_name,
                    default=field_info.default,
                    env_var=env_var,
                    source_file=source_file,
                )
            elif (
                isinstance(annotation, type)
                and issubclass(annotation, BaseModel)
                and not issubclass(annotation, BaseSettings)
            ):
                walk(
                    annotation,
                    env_prefix,
                    module_filename,
                    source_file,
                    nested_path + (field_name,),
                )

    for cls in _iter_settings_subclasses(BaseSettings):
        module_name = cls.__module__ or ""
        if not module_name.startswith("baldur.settings."):
            continue
        walk(
            cls,
            _settings_env_prefix(cls),
            _settings_module_filename(cls),
            _settings_source_file(cls),
            (),
        )
    return sorted(discovered.values())


# ---------------------------------------------------------------------------
# G31 — assurance-claim guard primitives (impl doc 575 D1, D2, D2-exists).
#
# A strong-guarantee docstring keyword MUST link a proving test via an adjacent
# `# verified-by: <test_ref>` comment. The matcher is the rare, low-false-positive
# strong-guarantee term-of-art set; the link lives in a `#` comment (never the
# docstring, which mkdocstrings publishes) and is checked for presence +
# well-formedness + name-existence (the semantic half stays with OOS #506(c)).
# ---------------------------------------------------------------------------

# Strong-guarantee keyword set (D1). Hyphenated delivery terms-of-art only —
# the spaced `exactly once` is a usage instruction ("invoke init exactly once"),
# the hyphenated `exactly-once` a delivery guarantee. Plus durability /
# consistency terms. Matched case-insensitively with hyphen-boundary
# preservation, so case-variants are caught while the spaced form stays excluded.
ASSURANCE_KEYWORDS: tuple[str, ...] = (
    "exactly-once",
    "at-least-once",
    "at-most-once",
    "zero data loss",
    "zero-data-loss",
    "linearizable",
    "wait-free",
)

_ASSURANCE_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(r"(?<![\w-])" + re.escape(keyword) + r"(?![\w-])", re.IGNORECASE)
    for keyword in ASSURANCE_KEYWORDS
)

# `# verified-by: <test_ref>` proof-link comment (D2). The token is a test
# function name or pytest node id, never a `file:line` citation.
_VERIFIED_BY_RE = re.compile(r"#\s*verified-by:\s*(\S+)")

_DOCSTRING_NODE_TYPES: tuple[type[ast.AST], ...] = (
    ast.Module,
    ast.ClassDef,
    ast.FunctionDef,
    ast.AsyncFunctionDef,
)


def find_assurance_claims(docstring: str) -> list[str]:
    """Return every strong-guarantee keyword (D1 set) present in ``docstring``.

    Case-insensitive (``re.IGNORECASE``) and hyphen-boundary-preserving: the
    spaced ``exactly once`` usage-instruction is NOT matched (only the
    hyphenated term-of-art ``exactly-once``), while ``Exactly-Once`` /
    ``EXACTLY-ONCE`` case-variants ARE. Pure function.
    """
    return [
        keyword
        for keyword, pattern in zip(
            ASSURANCE_KEYWORDS, _ASSURANCE_PATTERNS, strict=True
        )
        if pattern.search(docstring)
    ]


def verified_by_ref(line: str) -> str | None:
    """Return the ``<test_ref>`` token of a ``# verified-by:`` comment in ``line``.

    None when absent or malformed (no non-empty token). Well-formedness is the
    presence of a single ``\\S+`` token after ``# verified-by:``.
    """
    match = _VERIFIED_BY_RE.search(line)
    return match.group(1) if match else None


def iter_docstring_nodes(tree: ast.Module) -> Iterator[tuple[ast.AST, str]]:
    """Yield ``(node, docstring)`` for every Module/Class/Func node carrying one.

    Node-aware companion to ``iter_docstrings`` (which yields strings only): G31
    needs the owning node to correlate the ``# verified-by:`` adjacency window
    with raw source lines.
    """
    for node in ast.walk(tree):
        if isinstance(node, _DOCSTRING_NODE_TYPES):
            doc = ast.get_docstring(node, clean=False)
            if doc:
                yield node, doc


def _docstring_expr(node: ast.AST) -> ast.Expr | None:
    body = getattr(node, "body", None)
    if (
        isinstance(body, list)
        and body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        return body[0]
    return None


def assurance_symbol_span(node: ast.AST) -> tuple[int, int]:
    """Return the ``(start, end)`` raw-line window for ``# verified-by:`` adjacency.

    Per D2: from the symbol's first ``def`` / ``class`` / decorator line
    (``node.lineno``, or the first decorator line when decorated; line 1 for a
    module) through the line immediately after its docstring node
    (``docstring.end_lineno + 1``). Covers a comment in the signature block,
    between the signature and the docstring, or on the first line after the
    docstring, while excluding a comment belonging to a sibling symbol.
    """
    if isinstance(node, ast.Module):
        start = 1
    else:
        start = getattr(node, "lineno", 1)
        for deco in getattr(node, "decorator_list", []):
            start = min(start, deco.lineno)
    doc_expr = _docstring_expr(node)
    if doc_expr is not None and doc_expr.end_lineno is not None:
        end = doc_expr.end_lineno + 1
    else:
        end = start
    return start, end


def has_verified_by_link(raw_lines: list[str], node: ast.AST) -> bool:
    """True if a well-formed ``# verified-by:`` comment sits within ``node``'s span.

    ``raw_lines`` is the file source split into lines (1-based addressing). The
    adjacency window is ``assurance_symbol_span(node)`` (D2).
    """
    start, end = assurance_symbol_span(node)
    for lineno in range(start, end + 1):
        if 1 <= lineno <= len(raw_lines) and verified_by_ref(raw_lines[lineno - 1]):
            return True
    return False


def resolve_test_ref_name(test_ref: str) -> str:
    """Reduce a ``# verified-by:`` token to a bare test-function name (D2-exists).

    Splits a pytest node id on ``::`` (last component) and strips a ``[param]``
    parametrization suffix, so ``path::test_x[case]`` resolves to ``test_x``. A
    bare token is returned unchanged.
    """
    name = test_ref.split("::")[-1]
    return name.split("[", 1)[0]


@lru_cache(maxsize=8)
def collect_test_def_names(tests_root: Path = PROJECT_ROOT / "tests") -> frozenset[str]:
    """Return every ``def <name>`` defined anywhere under ``tests_root`` (D2-exists).

    Name-level (not path-level), so moving a test between files does not break a
    ``# verified-by:`` link and a parametrized ``test_x[case]`` resolves via its
    base ``def test_x``. Cached per root.
    """
    names: set[str] = set()
    if not tests_root.exists():
        return frozenset(names)
    for path in tests_root.rglob("*.py"):
        tree = parse_ast(path)
        if tree is None:
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                names.add(node.name)
    return frozenset(names)


# ---------------------------------------------------------------------------
# G32 — long-form flag consumer-reachability classifier (impl doc 575 D4, D5).
#
# For each long-form `*_enabled` / `enable_*` flag name, collect every consumer
# READ — both a static `<obj>.F` attribute load AND a dynamic
# `getattr(<obj>, "F", …)` string-constant access — and classify each by its
# nearest enclosing syntactic context into {gate | echo | ambiguous}. A flag is
# DEAD when it has no read at all, or every read is an echo-subscript (copied
# into a report dict, never gating real work). The getattr-string arm is
# load-bearing: without it, getattr-only-consumed live flags false-positive as
# none-dead. Variable-name getattr (`getattr(s, name)`) stays undetectable (D5).
# ---------------------------------------------------------------------------


def _build_parent_map(tree: ast.AST) -> dict[int, ast.AST]:
    parent: dict[int, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parent[id(child)] = node
    return parent


def oss_src_root() -> Path:
    """Return the ``src/baldur`` source root, located via the import system.

    Resolved through ``importlib.util.find_spec("baldur")`` so the OSS-source
    scan works whether ``baldur`` lives in this repository's ``src/baldur``
    (editable layout) or is consumed as an installed dependency — the case when
    the claim↔wiring consumer-reachability gate runs in the private repository,
    where OSS is a pip dependency rather than an in-tree package. Falls back to
    ``PROJECT_ROOT/src/baldur`` when no spec is found. Pure path computation;
    patchable in tests to point the scan at a fixture tree without a real
    install.
    """
    try:
        spec = importlib.util.find_spec("baldur")
    except (ImportError, ValueError):
        spec = None
    if spec is not None and spec.submodule_search_locations:
        return Path(next(iter(spec.submodule_search_locations))).resolve()
    return PROJECT_ROOT / "src" / "baldur"


def consumer_src_roots() -> tuple[Path, ...]:
    """The flag-consumer scan roots: OSS (`baldur`) + both private tiers.

    The cross-tier re-check is mandatory: a flag read only in a higher tier
    looks dead from OSS alone (e.g. a long-form flag gated only in a dormant
    adapter would otherwise false-positive as dead). The ``baldur`` root is
    resolved via ``oss_src_root()`` (so the scan finds OSS whether it is in-tree
    or an installed dependency); ``baldur_pro`` / ``baldur_dormant`` come from
    the local ``src/`` tree. ``walk_src`` skips absent roots, so an OSS-only or
    OSS/PRO-only checkout simply scans fewer tiers.
    """
    return (
        oss_src_root(),
        PROJECT_ROOT / "src" / "baldur_pro",
        PROJECT_ROOT / "src" / "baldur_dormant",
    )


def _is_getattr_string(node: ast.Call) -> bool:
    """True for ``getattr(<obj>, "<const>", …)`` — a string-constant dynamic read."""
    return (
        isinstance(node.func, ast.Name)
        and node.func.id == "getattr"
        and len(node.args) >= 2
        and isinstance(node.args[1], ast.Constant)
        and isinstance(node.args[1].value, str)
    )


def _string_key_read_name(node: ast.AST, flag_names: frozenset[str]) -> str | None:
    """Return the matched flag name if ``node`` is a string-key dict read, else None.

    Covers the two `model_dump()`→runtime-config→string-key bridge shapes (D5):
    a ``<obj>["F"]`` subscript Load and a ``<obj>.get("F", …)`` call whose first
    argument is the string-constant flag name. These complete D4's "name-matched
    dynamic reads" beyond `getattr` so a flag consumed only through a serialized
    settings dict (`config.get("F")`) is not mis-classified as dead.
    """
    if (
        isinstance(node, ast.Subscript)
        and isinstance(node.ctx, ast.Load)
        and isinstance(node.slice, ast.Constant)
        and isinstance(node.slice.value, str)
        and node.slice.value in flag_names
    ):
        return node.slice.value
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "get"
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
        and node.args[0].value in flag_names
    ):
        return node.args[0].value
    return None


def _read_role(par: ast.AST, cur: ast.AST) -> str | None:
    """Classify ``cur``'s role within its immediate parent ``par`` (None = transparent)."""
    if isinstance(par, ast.Assign):
        if cur is par.value:
            if len(par.targets) == 1 and isinstance(par.targets[0], ast.Subscript):
                return "echo"
            return "ambiguous"
        return None
    if isinstance(par, (ast.AnnAssign, ast.AugAssign)):
        return "ambiguous" if cur is par.value else None
    if isinstance(par, ast.Return):
        return "ambiguous" if cur is par.value else None
    if isinstance(par, ast.Call):
        if any(cur is arg for arg in par.args) or any(
            cur is kw.value for kw in par.keywords
        ):
            return "ambiguous"
        return None
    if isinstance(par, ast.Dict):
        return "ambiguous" if any(cur is value for value in par.values) else None
    if isinstance(par, (ast.If, ast.While, ast.IfExp)):
        return "gate" if cur is par.test else None
    if isinstance(par, ast.Assert):
        return "gate" if cur is par.test else None
    if isinstance(par, ast.BoolOp):
        return "gate"
    return None


def classify_read(read_node: ast.AST, parent: dict[int, ast.AST]) -> str:
    """Classify one flag read by its nearest enclosing syntactic context (D4).

    Returns ``"gate"`` (the read controls a code path — wired), ``"echo"`` (the
    read is the RHS of a report subscript assignment — suspect), or
    ``"ambiguous"`` (anything else — conservatively not-dead). The nearest
    classifying ancestor wins, so a flag passed as a function argument inside an
    ``if`` test scores ``ambiguous`` (the function-argument role dominates the
    enclosing gate). Climbing to the top without a classifying context defaults
    to ``ambiguous`` (not-dead).
    """
    cur = read_node
    par = parent.get(id(cur))
    while par is not None:
        role = _read_role(par, cur)
        if role is not None:
            return role
        cur = par
        par = parent.get(id(cur))
    return "ambiguous"


def _iter_flag_read_classes(
    tree: ast.Module, flag_names: frozenset[str]
) -> Iterator[tuple[str, str]]:
    parent = _build_parent_map(tree)
    for node in ast.walk(tree):
        name: str | None = None
        read_node: ast.AST | None = None
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.ctx, ast.Load)
            and node.attr in flag_names
        ):
            name, read_node = node.attr, node
        elif isinstance(node, ast.Call) and _is_getattr_string(node):
            const = node.args[1].value
            if const in flag_names:
                name, read_node = const, node
        else:
            string_key = _string_key_read_name(node, flag_names)
            if string_key is not None:
                name, read_node = string_key, node
        if name is not None and read_node is not None:
            yield name, classify_read(read_node, parent)


def classify_flag_in_source(source: str, flag_name: str) -> set[str]:
    """Return the set of read-classifications of ``flag_name`` in ``source`` (pure).

    ``set()`` (the ``none`` class) means no read at all. Used for inline-fixture
    unit tests; the gate uses ``collect_long_form_flag_reads`` over the file set.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()
    return {
        classification
        for _name, classification in _iter_flag_read_classes(
            tree, frozenset({flag_name})
        )
    }


def collect_long_form_flag_reads(
    flag_names: Iterable[str],
    roots: Iterable[Path] | None = None,
) -> dict[str, set[str]]:
    """Map each flag name to the set of its read-classifications across ``roots``.

    One AST pass per ``.py`` file under ``roots`` (``baldur`` + ``baldur_pro`` +
    ``baldur_dormant``, excluding ``tests/``). ``roots`` defaults to
    ``consumer_src_roots()`` — resolved lazily at call time so a test that
    patches ``oss_src_root`` redirects the scan. A flag whose name never appears
    as a read keeps its empty set → the ``none`` class.
    """
    if roots is None:
        roots = consumer_src_roots()
    targets = frozenset(flag_names)
    result: dict[str, set[str]] = {name: set() for name in targets}
    for path in walk_src(roots):
        tree = parse_ast(path)
        if tree is None:
            continue
        for name, classification in _iter_flag_read_classes(tree, targets):
            result[name].add(classification)
    return result


def flag_is_dead(classifications: set[str]) -> bool:
    """True when a flag's reads make it dead (D4): no read, or all echo-subscript."""
    return not (classifications - {"echo"})


# ---------------------------------------------------------------------------
# FEATURE_CATALOG 3-axis parsing (shared by G36 + G37, impl doc 589 D1/D5).
#
# G36 (no-false-dormant import scan) and G37 (catalog tier drift) MUST read the
# catalog through ONE parser so the triage predicate and the gate predicate
# cannot diverge — a Parked entry the two gates classify differently would defeat
# the enforced-empty guarantee. The parser is pure (string in, structured
# entries out) and value-set membership is the only "recognized axis" notion.
# ---------------------------------------------------------------------------

CATALOG_PATH = PROJECT_ROOT / "docs" / "features" / "FEATURE_CATALOG.md"

# The three physical source roots a `**Module**:` path may live under.
CATALOG_SRC_ROOTS: dict[str, Path] = {
    "baldur": PROJECT_ROOT / "src" / "baldur",
    "baldur_pro": PROJECT_ROOT / "src" / "baldur_pro",
    "baldur_dormant": PROJECT_ROOT / "src" / "baldur_dormant",
}

# Recognized axis values (D1). A value outside its set is a mistyped/drifted axis.
PRODUCT_STATUS_VALUES: frozenset[str] = frozenset({"OSS", "PRO", "Parked"})
CODE_ROLE_VALUES: frozenset[str] = frozenset({"product-feature", "internal-support"})
PACKAGE_VALUES: frozenset[str] = frozenset({"baldur", "baldur_pro", "baldur_dormant"})

CATALOG_AXIS_LABELS: tuple[str, ...] = ("Product Status", "Code Role", "Package")


def _catalog_axis_label_present(body: str, label: str) -> bool:
    return re.search(rf"\*\*{re.escape(label)}\*\*\s*:", body) is not None


def _catalog_axis_value(body: str, label: str) -> str | None:
    """Return the trimmed value of a ``**<label>**:`` axis field, or None.

    The value runs up to the next ``|`` pipe (axis fields are pipe-joined on one
    bullet line) or end of line; surrounding whitespace/backticks are stripped so
    format variance does not matter.
    """
    match = re.search(rf"\*\*{re.escape(label)}\*\*\s*:\s*([^|\n]+)", body)
    if not match:
        return None
    return match.group(1).strip().strip("`").strip()


def _catalog_module_paths(body: str) -> tuple[str, ...]:
    """Return every backtick path cited on a ``**Module**:`` bullet line."""
    paths: list[str] = []
    for line in body.splitlines():
        match = re.search(r"\*\*Module\*\*\s*:\s*(.+)$", line)
        if match:
            paths.extend(re.findall(r"`([^`]+)`", match.group(1)))
    return tuple(paths)


class CatalogEntry:
    """One ``###`` block of FEATURE_CATALOG.md, with parsed axis fields + modules."""

    def __init__(self, title: str, body: str) -> None:
        self.title = title
        self.body = body
        self.product_status = _catalog_axis_value(body, "Product Status")
        self.code_role = _catalog_axis_value(body, "Code Role")
        self.package = _catalog_axis_value(body, "Package")
        self.modules: tuple[str, ...] = _catalog_module_paths(body)

    @property
    def is_product_entry(self) -> bool:
        """A product entry carries a ``**Module**:`` field or any axis label.

        Keyed on label PRESENCE (not a recognized value), so a mistyped value or
        label still routes the entry through the completeness check rather than
        letting it slip past unscanned.
        """
        if self.modules:
            return True
        return any(
            _catalog_axis_label_present(self.body, label)
            for label in CATALOG_AXIS_LABELS
        )

    @property
    def is_sold_product_feature(self) -> bool:
        """OSS/PRO product-feature — its `Module` subtrees are the SOLD set (G36)."""
        return (
            self.product_status in {"OSS", "PRO"}
            and self.code_role == "product-feature"
        )


def parse_catalog_entries(text: str) -> list[CatalogEntry]:
    """Split catalog markdown into ``###`` entry blocks (pure).

    A ``## `` top-level section heading closes any open ``### `` entry, so a
    later ``**Module**:`` line in section prose is never folded into the
    preceding entry.
    """
    entries: list[CatalogEntry] = []
    title: str | None = None
    buf: list[str] = []

    def flush() -> None:
        if title is not None:
            entries.append(CatalogEntry(title, "\n".join(buf)))

    for line in text.splitlines():
        if line.startswith("### "):
            flush()
            title = line[4:].strip()
            buf = []
        elif line.startswith("## "):
            flush()
            title = None
            buf = []
        elif title is not None:
            buf.append(line)
    flush()
    return entries


def catalog_module_path_status(path: str) -> str:
    """Classify a ``**Module**:`` path: ``ok`` / ``missing`` / ``skip`` (589 D5c).

    ``ok`` — exists under ``src/baldur`` or under a PRESENT private root.
    ``skip`` — absent under ``src/baldur`` and both private roots are absent
    (OSS-only checkout: a pro/dormant-only path is unverifiable, G19 precedent).
    ``missing`` — absent everywhere a root is present.
    """
    if (CATALOG_SRC_ROOTS["baldur"] / path).exists():
        return "ok"
    any_private_present = False
    for key in ("baldur_pro", "baldur_dormant"):
        root = CATALOG_SRC_ROOTS[key]
        if root.exists():
            any_private_present = True
            if (root / path).exists():
                return "ok"
    return "missing" if any_private_present else "skip"


def resolve_module_locations(path: str) -> list[tuple[Path, str]]:
    """Resolve a ``**Module**:`` path to ``[(fs_path, dotted_module), ...]``.

    Tries each of the three source roots; a path may resolve under more than one
    (e.g. ``multiregion/`` lives in both ``baldur`` and ``baldur_dormant``). The
    dotted module is the root package name + the path with separators converted
    to dots and any trailing ``/`` or ``.py`` stripped — so
    ``services/dlq/`` under ``baldur_pro`` → ``baldur_pro.services.dlq`` and
    ``services/x/svc.py`` under ``baldur`` → ``baldur.services.x.svc``.
    """
    results: list[tuple[Path, str]] = []
    cleaned = path.rstrip("/")
    if cleaned.endswith(".py"):
        cleaned = cleaned[: -len(".py")]
    dotted_tail = cleaned.replace("/", ".")
    for root_name, root in CATALOG_SRC_ROOTS.items():
        if (root / path).exists():
            results.append((root / path, f"{root_name}.{dotted_tail}"))
    return results


__all__ = [
    "ASSURANCE_KEYWORDS",
    "BASELINE_PATH",
    "CATALOG_AXIS_LABELS",
    "CATALOG_PATH",
    "CATALOG_SRC_ROOTS",
    "CODE_ROLE_VALUES",
    "DEFAULT_SRC_ROOTS",
    "DOC_ID_ALLOWLIST",
    "DOC_ID_PATTERNS",
    "EnableField",
    "KOREAN_RE",
    "MODULE_SYMBOL",
    "OSS_TESTS_ROOT",
    "PACKAGE_VALUES",
    "PRODUCT_STATUS_VALUES",
    "PROJECT_ROOT",
    "REFERENCE_DIR",
    "RULE_REGISTRY_DOC",
    "CatalogEntry",
    "assurance_symbol_span",
    "baselined_count",
    "catalog_module_path_status",
    "classify_flag_in_source",
    "classify_read",
    "collect_long_form_flag_reads",
    "collect_test_def_names",
    "collect_violations",
    "core_dependency_modules",
    "directive_targets",
    "discover_enable_fields",
    "parse_catalog_entries",
    "resolve_module_locations",
    "find_assurance_claims",
    "find_doc_ids",
    "find_prose_leaks",
    "flag_is_dead",
    "format_violation",
    "has_verified_by_link",
    "is_long_form_enable_field",
    "iter_docstring_nodes",
    "iter_docstrings",
    "iter_inline_code_spans",
    "load_baseline",
    "optional_extras_modules",
    "parse_ast",
    "resolve_all_chain_files",
    "resolve_callsites",
    "resolve_test_ref_name",
    "symbol_of",
    "verified_by_ref",
    "walk_src",
]
