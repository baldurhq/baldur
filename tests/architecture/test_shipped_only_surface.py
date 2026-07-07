"""G58 — the published OSS surface speaks only shipped reality (existence-ban class).

The published docs site (mkdocs Material + mkdocstrings) is a fail-closed
publish allowlist. Everything it renders speaks to an external reader as an
available capability. A family of symbols legitimately lives in the OSS public
API — boundary Protocols and NoOp defaults that OSS code type-hints against so
it stays import-clean when the private packages are absent — but whose real
implementations ship only in the non-installable private distribution. Naming
those on the site discloses a capability no public user can enable, plus the
private distribution's name and install extras.

This gate is the OSS half of the shipped-only-surface invariant. It scans for
the **existence-ban token class** — tokens that leak the *existence and shape*
of the private, non-installable surface: the private-distribution package name
and install-extra form, the retired tier label, and the storage/eventing/
coordination technologies whose adapters ship only privately. These tokens are
already present in this repo's test surface (the doc-ID-leak gate's fixtures,
the false-dormant gate), so gating them here adds no new disclosure.

The three rendered surfaces this rule gates (a deliberate superset of what
mkdocstrings actually renders — exact-rendered truth is closed at build time by
a post-build HTML scan):

* **(i) Rendered docstrings** — public-symbol docstrings pulled by ``:::``
  directives across ``docs/reference/**``. Reuses the doc-ID gate's resolver
  (each ``:::`` target -> its defining source file) and AST docstring
  extraction.
* **(ii) Authored markdown** — the published ``.md`` text across the full
  publish allowlist (root pages + ``getting-started/`` + ``concepts/`` +
  ``reference/``). A page's intro prose and headings render even when a symbol
  carries no docstring.
* **(iii) Rendered symbol names** — the leaf name of every ``:::`` target plus
  the ``__all__`` member names of a whole-package/module target. A symbol name
  is a heading on the site, so ``NoOpKafkaEventBus`` discloses its family even
  with an empty docstring.

Deferred-feature names (the post-launch roadmap) are NOT gated here — a public
token list would itself republish the roadmap this invariant exists to remove.
That half runs privately.

Baseline is enforced-empty (no allowlist) — a leak is scrubbed, never
baselined.

Rule registry:
``ARCHITECTURE.md#g58-shipped-only-surface``
"""

from __future__ import annotations

import importlib
import importlib.util
import re
from pathlib import Path

from tests.architecture.conftest import (
    PROJECT_ROOT,
    REFERENCE_DIR,
    directive_targets,
    iter_docstrings,
)
from tests.architecture.test_mkdocs_internal_doc_id_leak import (
    _resolve_reference_source_files,
)

_DOCS_DIR = PROJECT_ROOT / "docs"


# ---------------------------------------------------------------------------
# The existence-ban token matcher (pure — unit-tested below).
#
# Each token carries a DISTINCTIVE anchor (a word boundary + case-sensitivity
# where the lowercase form is a legitimate English word). ``Dormant`` / ``WORM``
# / ``ENT`` are matched case-sensitively so ``a dormant connection``, ``worm``,
# and ``environment`` never match; the technology proper nouns and hyphenated
# terms of art are matched case-insensitively so a module-path ``.kafka`` hits.
# ---------------------------------------------------------------------------
_BAN_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("baldur_dormant", re.compile(r"\bbaldur_dormant\b")),
    ("Dormant", re.compile(r"\bDormant\b")),  # the retired private-tier label
    ("WORM", re.compile(r"\bWORM\b")),  # write-once storage acronym
    ("Object Lock", re.compile(r"\bObject Lock\b", re.IGNORECASE)),
    ("baldur-pro[extra]", re.compile(r"\bbaldur-pro\[")),  # private install extra
    ("ENT", re.compile(r"\bENT\b")),  # the abolished tier label
    ("private-tier", re.compile(r"\bprivate-tier\b", re.IGNORECASE)),
    ("multi-region", re.compile(r"\bmulti-?region\b", re.IGNORECASE)),
    ("quorum", re.compile(r"\bquorum\b", re.IGNORECASE)),
    ("self-learning", re.compile(r"\bself-learning\b", re.IGNORECASE)),
    ("Kafka", re.compile(r"\bKafka\b", re.IGNORECASE)),
)


def find_banned_tokens(text: str) -> list[str]:
    """Return every existence-ban substring in ``text`` (pure function).

    Anchored + case-scoped so the lowercase-legitimate forms (``dormant``
    connection, ``worm``, ``environment``) do not match. Returns the matched
    substrings for a self-describing failure message. Use for prose surfaces
    (docstrings + authored markdown); a camelCase identifier needs
    ``find_banned_symbol_tokens`` instead (word boundaries do not sit inside
    ``NoOpKafkaEventBus``).
    """
    hits: list[str] = []
    for _label, pattern in _BAN_PATTERNS:
        hits.extend(match.group(0) for match in pattern.finditer(text))
    return hits


# The identifier-embeddable subset of the ban tokens: the technology / tier
# NOUNS that appear as a camelCase or ``_``-delimited component of a rendered
# symbol name (a heading), where the prose matcher's word boundaries never
# fire. Case-insensitive on the split components, so ``NoOpKafkaEventBus`` ->
# ``kafka`` and ``QuorumWitnessProtocol`` -> ``quorum`` are caught. The
# multi-word / hyphenated tokens (``multi-region``, ``self-learning``,
# ``Object Lock``) are prose-only and never encoded in a single identifier.
_SYMBOL_NAME_NOUNS: frozenset[str] = frozenset({"kafka", "worm", "quorum", "dormant"})

# camelCase + acronym + ``_`` component splitter: an all-caps run before a
# ``Xx`` boundary (``WORMAdapter`` -> ``WORM``), a ``Xxx`` word, a lowercase
# run, or a digit run.
_IDENT_COMPONENT_RE = re.compile(
    r"[A-Z]+(?=[A-Z][a-z])|[A-Z][a-z]+|[A-Z]+|[a-z]+|[0-9]+"
)


def find_banned_symbol_tokens(name: str) -> list[str]:
    """Return every existence-ban NOUN embedded in identifier ``name`` (pure).

    Splits ``name`` into camelCase / acronym / ``_`` components and matches each
    (case-insensitively) against ``_SYMBOL_NAME_NOUNS``. A heading is a leak
    regardless of case, so — unlike the prose matcher — ``dormant`` matches in
    any case here.
    """
    components = {m.group(0).lower() for m in _IDENT_COMPONENT_RE.finditer(name)}
    return sorted(_SYMBOL_NAME_NOUNS & components)


def _published_markdown_files(docs_dir: Path = _DOCS_DIR) -> list[Path]:
    """Return the published ``.md`` set per the ``mkdocs.yml`` publish allowlist.

    The allowlist re-includes exactly the root pages (``docs/*.md``),
    ``getting-started/``, ``concepts/`` (minus ``_``-prefixed scaffolding), and
    ``reference/``. ``runbooks/`` / ``impl/`` / ``laws/`` are excluded and are
    not scanned.
    """
    files: list[Path] = sorted(docs_dir.glob("*.md"))
    for subdir in ("getting-started", "concepts", "reference"):
        tree = docs_dir / subdir
        if tree.exists():
            files.extend(
                sorted(p for p in tree.rglob("*.md") if not p.name.startswith("_"))
            )
    return files


def scannable_markdown_lines(text: str) -> list[tuple[int, str]]:
    """Yield ``(lineno, line)`` for prose lines, skipping ``:::`` option blocks.

    A ``:::`` autodoc directive line and its indented mkdocstrings ``options:``
    block (``filters:`` regex strings, etc.) are consumed by mkdocstrings and
    never render as prose — but a ``filters`` regex legitimately spells a banned
    member name (``"!(?i)postmortem"``) to hide it, which would otherwise trip
    this raw-content scan. The directive target itself is covered by the
    rendered-symbol-name scan, so dropping these lines loses no coverage. Pure
    function; 1-based line numbers.
    """
    out: list[tuple[int, str]] = []
    in_directive_block = False
    for lineno, line in enumerate(text.splitlines(), start=1):
        if line.startswith(":::"):
            in_directive_block = True
            continue
        if in_directive_block:
            if line.strip() == "":
                in_directive_block = False
                continue
            if line[:1] in (" ", "\t"):
                continue  # indented mkdocstrings option line
            in_directive_block = False
        out.append((lineno, line))
    return out


def _rendered_symbol_names(reference_dir: Path = REFERENCE_DIR) -> set[str]:
    """Return every symbol NAME mkdocstrings renders as a heading (superset).

    The leaf of every ``:::`` target, plus the ``__all__`` member names of a
    target that imports as a package/module (a whole-package directive renders
    each member's name). Symbol targets are NOT imported (their leaf name is
    already captured); only package/module targets are, so no optional-extra
    adapter is executed beyond what a whole-package directive already implies.
    """
    names: set[str] = set()
    for target in directive_targets(reference_dir):
        names.add(target.rsplit(".", 1)[-1])
        try:
            spec = importlib.util.find_spec(target)
        except (ImportError, AttributeError, ValueError):
            continue
        if spec is None:
            continue  # a genuine symbol target — leaf already captured
        module = _safe_import(target)
        if module is not None:
            names.update(getattr(module, "__all__", []))
    return names


def _safe_import(name: str):
    try:
        return importlib.import_module(name)
    except Exception:
        return None


def _rel(path: Path) -> str:
    try:
        return path.resolve().relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return path.as_posix()


class TestShippedOnlySurface:
    """G58 — the published OSS surface carries no existence-ban tokens."""

    def test_scan_inputs_are_nonempty(self):
        """Anti-vacuous-pass guard: a broken resolver would pass every scan."""
        assert _resolve_reference_source_files(), (
            "G58: resolved zero reference source files — the ::: resolver is "
            "broken, so the docstring scan would pass vacuously."
        )
        assert _published_markdown_files(), (
            "G58: zero published markdown files discovered — the allowlist walk "
            "is broken."
        )
        assert _rendered_symbol_names(), (
            "G58: zero rendered symbol names discovered — the directive walk is broken."
        )

    def test_no_banned_tokens_in_rendered_docstrings(self):
        offenders: list[str] = []
        for path in sorted(_resolve_reference_source_files()):
            source = path.read_text(encoding="utf-8")
            hits: list[str] = []
            for docstring in iter_docstrings(source):
                hits.extend(find_banned_tokens(docstring))
            if hits:
                unique = ", ".join(sorted(set(hits)))
                offenders.append(f"  {_rel(path)} — {unique}")

        assert not offenders, (
            f"G58: existence-ban tokens found in rendered docstrings "
            f"({len(offenders)} file(s)). These docstrings ship to "
            "baldur.sh/reference/ — de-publish the boundary symbol (drop its "
            "::: line and add it to G25's _UNRENDERED_BOUNDARY_SYMBOLS) or "
            "rewrite the clause neutrally.\n" + "\n".join(offenders)
        )

    def test_no_banned_tokens_in_published_markdown(self):
        offenders: list[str] = []
        for path in _published_markdown_files():
            text = path.read_text(encoding="utf-8")
            for lineno, line in scannable_markdown_lines(text):
                hits = find_banned_tokens(line)
                if hits:
                    unique = ", ".join(sorted(set(hits)))
                    offenders.append(f"  {_rel(path)}:{lineno} — {unique}")

        assert not offenders, (
            f"G58: existence-ban tokens found in published markdown "
            f"({len(offenders)} line(s)). Rewrite the boundary statement without "
            "the roadmap/private vocabulary — never delete it (that would make "
            "the docs overclaim).\n" + "\n".join(offenders)
        )

    def test_no_banned_tokens_in_rendered_symbol_names(self):
        offenders: list[str] = []
        for name in sorted(_rendered_symbol_names()):
            hits = find_banned_symbol_tokens(name)
            if hits:
                unique = ", ".join(sorted(set(hits)))
                offenders.append(f"  {name} — {unique}")

        assert not offenders, (
            f"G58: existence-ban tokens in rendered symbol names "
            f"({len(offenders)} symbol(s)). A symbol name is a heading on the "
            "site — de-publish it (drop its ::: directive) even if its docstring "
            "is empty.\n" + "\n".join(offenders)
        )


# --------------------------------------------------------------------------
# Anti-silent-pass — the pure matcher is anchored + FP-free, and each of the
# three scan paths flags a deliberately injected token while a clean fixture
# does not.
# --------------------------------------------------------------------------

_POSITIVE_TOKENS = [
    "baldur_dormant.adapters.kafka.consumer",
    "the Dormant tier",
    "WORM storage",
    "S3 Object Lock retention",
    "pip install baldur-pro[dormant,aws]",
    "the ENT tier is gone",
    "a private-tier adapter",
    "multi-region failover",
    "multiregion split-brain",
    "the quorum witness",
    "a self-learning strategy",
    "the Kafka event bus",
    "baldur_dormant.adapters.kafka",  # dotted path with lowercase kafka
]

_NEGATIVE_TOKENS = [
    "the connection went dormant",  # lowercase — legitimate English
    "read the environment variable",  # 'ENT' inside a word
    "a different component is present",  # 'ent' inside words
    "worm-eaten wood",  # lowercase worm
    "the region is single",  # 'region' without 'multi-'
    "BALDUR_MULTIREGION_QUORUM_REDIS_URL",  # env-var-name substring, no boundary
    "learning rate",  # 'learning' without 'self-'
    "baldur-pro is the private tier",  # 'baldur-pro' without the extra bracket
    "object storage",  # 'object' without 'lock'
]


class TestBannedTokenMatcher:
    """The pure ``find_banned_tokens`` matcher is anchored and FP-free."""

    def test_positive_tokens_flagged(self):
        for text in _POSITIVE_TOKENS:
            assert find_banned_tokens(text), f"expected a ban match in {text!r}"

    def test_negative_tokens_clean(self):
        for text in _NEGATIVE_TOKENS:
            assert not find_banned_tokens(text), f"unexpected ban match in {text!r}"


_FIXTURE_DOCSTRING_WITH_TOKEN = '''
"""Bridges to the Kafka event bus (baldur_dormant.adapters.kafka)."""


class Sample:
    """A clean class docstring."""
'''

_FIXTURE_DOCSTRING_CLEAN = '''
"""A clean module summary with no private surface."""


class Sample:
    """A clean class docstring."""
'''


class TestScanPathsAntiSilentPass:
    """Each of the three scan paths flags an injected token; clean fixtures do not."""

    def test_docstring_path_flags(self):
        hits: list[str] = []
        for docstring in iter_docstrings(_FIXTURE_DOCSTRING_WITH_TOKEN):
            hits.extend(find_banned_tokens(docstring))
        assert "Kafka" in hits
        assert "baldur_dormant" in hits

    def test_docstring_path_clean_not_flagged(self):
        hits: list[str] = []
        for docstring in iter_docstrings(_FIXTURE_DOCSTRING_CLEAN):
            hits.extend(find_banned_tokens(docstring))
        assert not hits

    def test_markdown_path_flags(self):
        md_text = (
            "# Heading\n\nThe traffic-routing adapter for multi-region failover.\n"
        )
        hits: list[str] = []
        for _lineno, line in scannable_markdown_lines(md_text):
            hits.extend(find_banned_tokens(line))
        assert "multi-region" in hits

    def test_markdown_scan_skips_directive_option_blocks(self):
        # A ``filters`` regex legitimately spells a banned member name to hide
        # it — the option block must be skipped, but prose after it scanned.
        md_text = (
            "Intro prose.\n\n"
            "::: baldur.ProviderRegistry\n"
            "    options:\n"
            '      filters: ["!^_", "!(?i)quorum"]\n\n'
            "The Kafka bus is gone.\n"
        )
        hits: list[str] = []
        for _lineno, line in scannable_markdown_lines(md_text):
            hits.extend(find_banned_tokens(line))
        # 'quorum' lives only in the skipped filter line; 'Kafka' in real prose.
        assert "Kafka" in hits
        assert not any(h.lower() == "quorum" for h in hits)

    def test_symbol_name_path_flags(self):
        # The symbol-name scan splits camelCase — a rendered heading
        # ``NoOpKafkaEventBus`` must flag even with no docstring.
        assert find_banned_symbol_tokens("NoOpKafkaEventBus") == ["kafka"]
        assert find_banned_symbol_tokens("QuorumWitnessProtocol") == ["quorum"]
        assert find_banned_symbol_tokens("NoOpWormAdapter") == ["worm"]

    def test_symbol_name_path_clean_not_flagged(self):
        # A public symbol name must not trip the identifier matcher.
        assert not find_banned_symbol_tokens("RedisCacheAdapter")
        assert not find_banned_symbol_tokens("CircuitBreakerService")
