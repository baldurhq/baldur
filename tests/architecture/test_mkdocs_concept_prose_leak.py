r"""G29 — no published-prose leaks in the public concept-guide layer.

Wave 7C authors public concept guides under ``docs/concepts/``
(``baldur.sh/concepts/``). ``CONCEPT_GUIDE_STANDARDS.md`` §6 bans four leak
classes from that hand-authored ``.md`` prose; this rule machine-enforces it.

The leak classes (the §6 ban-list):

* internal doc-IDs (``NNN D/G/R/C`` / ``ADR-NN`` / ``Wave NN`` / ``#NNNN`` /
  ``OOS`` / ``DEC-NN``) and ``docs/{impl,laws,self_healing}/...`` path refs,
* ``.py:NN`` file:line citations,
* private-distribution paths (``baldur_dormant.*`` AND ``baldur_pro.*``),
* internal symbol names not in the public ``__all__`` — of which this gate
  mechanizes the unambiguous subset: a ``baldur.``-rooted dotted path with a
  private ``._segment`` (④-a-dotted) and a bare unqualified private ``_name``
  inside an inline code-span (④-a-bare, dunder-excluded).

**Mechanized ↔ judgment boundary (§6/§9).** The "public-looking internal
symbol" class — no underscore, not in ``__all__`` — and a bare *unqualified*
private symbol pasted into a fenced example block are NOT mechanized here. A
zero-false-positive gate cannot distinguish them from public symbols, env vars,
literals, or legitimate OSS locals without importing the private ``__all__``,
which a public-repo-safe ``tests/`` gate cannot do (G19/G20). The verification
skill (``CONCEPT_GUIDE_STANDARDS.md`` §9) owns that residual as a
quote-and-match judgment.

**Scope (§6 / 562 D3).** A raw-line scan of ``docs/concepts/**`` read directly
from the source tree — independent of the ``mkdocs.yml`` ``exclude_docs``
publish wiring, so the gate fires the moment a guide is authored. HTML comments
(``<!-- -->``) are INCLUDED: Python-Markdown passes them through to published
HTML, so a private path in a comment is a real view-source leak. Files whose
path carries an ``_``-prefixed component are EXCLUDED (mkdocs treats them as
unpublished); ``_TEMPLATE.md`` in particular carries the §6 ban-list as
instructional examples and must not self-trip the gate.

**Anti-vacuous (562 D4), two layers.** Layer 1: synthetic ``_FIXTURE_WITH_LEAK``
/ ``_FIXTURE_CLEAN`` constants keep the matcher live independently of the real
tree. Layer 2: the discovery-wiring assertion proves the concepts-dir glob
resolves ``_TEMPLATE.md`` — a broken glob path fails loudly instead of scanning
zero files forever. Asserting the *published-guide set* is non-empty is
forbidden (false at land time, when only ``_TEMPLATE.md`` exists).

Baseline is enforced-empty (no ``baseline.yaml`` key) — same discipline as
G20–G28. A confirmed false positive tightens the matcher, never gets baselined.

Rule registry:
``ARCHITECTURE.md#g29-mkdocs-concept-prose-leak``
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.architecture.conftest import (
    PROJECT_ROOT,
    find_prose_leaks,
    iter_inline_code_spans,
)

_CONCEPTS_DIR = (PROJECT_ROOT / "docs" / "concepts").resolve()

# The §6 pointer embedded in the failure message so a non-coder guide author
# can jump straight to the rule.
_GUIDE_POINTER = "CONCEPT_GUIDE_STANDARDS.md §6 (published-prose leak ban)"


def _rel(path: Path) -> str:
    try:
        return path.resolve().relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def _scannable_concept_files(concepts_dir: Path = _CONCEPTS_DIR) -> list[Path]:
    """Return the concept-guide ``.md`` files the gate scans.

    Excludes any file whose path (relative to the concepts dir) carries an
    ``_``-prefixed component — ``_TEMPLATE.md`` and any ``_draft/`` subtree —
    mirroring the mkdocs underscore-prefix = unpublished convention. Read
    directly from the source tree, independent of the ``exclude_docs`` publish
    wiring (562 D3).
    """
    files: list[Path] = []
    for path in sorted(concepts_dir.rglob("*.md")):
        rel_parts = path.relative_to(concepts_dir).parts
        if any(part.startswith("_") for part in rel_parts):
            continue
        files.append(path)
    return files


def _scan_text_for_leaks(rel_name: str, text: str) -> list[str]:
    """Raw-line scan ``text``, returning one formatted offender per leaking line.

    The unit shared by the live gate and the actionable-message test: scans
    line by line (so HTML comments and fenced content are covered uniformly)
    and renders ``  <file>:<line> — <leaked tokens>`` for each line that leaks.
    """
    offenders: list[str] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        leaks = find_prose_leaks(line)
        if leaks:
            unique = ", ".join(sorted(set(leaks)))
            offenders.append(f"  {rel_name}:{lineno} — {unique}")
    return offenders


def _build_failure_message(offenders: list[str]) -> str:
    """Render the actionable G29 failure message (file:line list + §6 pointer)."""
    return (
        f"G29: published-prose leaks found in concept guides "
        f"({len(offenders)} line(s)). These pages ship to baldur.sh/concepts/ "
        f"— remove the leaked token per {_GUIDE_POINTER}.\n" + "\n".join(offenders)
    )


def _assert_concepts_dir_discoverable(concepts_dir: Path) -> None:
    """Layer-2 anti-vacuous guard — the concepts-dir glob MUST find a guide.

    A broken glob path resolves zero files, which would let the leak scan pass
    vacuously forever. The private repo anchors on ``_TEMPLATE.md``; the public
    repo omits the template (not published) but ships the published
    (``_``-unprefixed) guides, so the anchor is ``_TEMPLATE.md`` present OR ≥1
    published guide present. A truly broken glob (both empty) still fails loudly,
    so ``test_broken_concepts_dir_discovery_fails_loudly`` still holds. (Asserting
    the *published-guide* set alone is non-empty is forbidden — it is false at the
    template's land time, when only ``_TEMPLATE.md`` exists.)
    """
    names = {p.name for p in concepts_dir.rglob("*.md")}
    has_published_guide = bool(_scannable_concept_files(concepts_dir))
    assert "_TEMPLATE.md" in names or has_published_guide, (
        f"G29: docs/concepts/ glob discovered neither _TEMPLATE.md nor any "
        f"published guide under {concepts_dir} — the scan path is broken, so the "
        "prose-leak gate would vacuously pass on zero files."
    )


class TestMkdocsConceptProseLeak:
    """G29 — the published concept-guide prose carries no §6 leaks."""

    def test_concepts_dir_discovery_wiring(self):
        """The real concepts dir resolves and exposes ``_TEMPLATE.md`` (Layer 2)."""
        _assert_concepts_dir_discoverable(_CONCEPTS_DIR)

    def test_broken_concepts_dir_discovery_fails_loudly(self):
        """A bogus concepts-dir path FAILS the discovery guard (anti-vacuous proof).

        This is the only way to prove the gate cannot silently pass on zero
        files without authoring a real published guide (562 Testability Notes /
        SC #5).
        """
        bogus = PROJECT_ROOT / "docs" / "concepts__does_not_exist"
        with pytest.raises(AssertionError):
            _assert_concepts_dir_discoverable(bogus)

    def test_no_prose_leaks_in_concept_guides(self):
        offenders: list[str] = []
        for path in _scannable_concept_files():
            text = path.read_text(encoding="utf-8")
            offenders.extend(_scan_text_for_leaks(_rel(path), text))

        assert not offenders, _build_failure_message(offenders)


# --------------------------------------------------------------------------
# Layer-1 synthetic fixtures — keep the matcher live independently of the real
# tree (which holds only ``_TEMPLATE.md`` at land time). The actionable-message
# test renders these through the same scan + formatter the live gate uses.
# --------------------------------------------------------------------------

_FIXTURE_WITH_LEAK = """\
# Some Feature

It is wired through baldur_pro.adaptive._helpers internally.
The `_refresh_state` hook fires on every recovery attempt.
<!-- maintainer note: see 508 D6 for the rationale -->
"""

_FIXTURE_CLEAN = """\
# Circuit Breaker

When the failure rate crosses the threshold the circuit `OPEN`s and trips.
Configure it with `BALDUR_CB_ENABLED`. The public facade lives at baldur.core.

```python
breaker = get_circuit_breaker()
self._state = "open"  # an OSS example — OSS source is public, not a leak
```

Use _italic_ emphasis freely; see ../../reference/index.md for the full API.
"""


class TestFixtureScanAndMessage:
    """The Layer-1 fixtures flag/clear correctly and the message is actionable."""

    def test_leak_fixture_produces_offenders(self):
        offenders = _scan_text_for_leaks("docs/concepts/sample.md", _FIXTURE_WITH_LEAK)
        assert offenders, "the leak fixture must produce at least one offender"

    def test_clean_fixture_produces_no_offenders(self):
        offenders = _scan_text_for_leaks("docs/concepts/sample.md", _FIXTURE_CLEAN)
        assert not offenders, f"clean fixture unexpectedly flagged: {offenders}"

    def test_failure_message_is_actionable(self):
        """The rendered failure message carries a file:line form and the §6 pointer.

        Mirrors G24's published-markdown message shape so a non-coder author can
        locate the leak and read the rule (562 SC #6).
        """
        offenders = _scan_text_for_leaks("docs/concepts/sample.md", _FIXTURE_WITH_LEAK)
        message = _build_failure_message(offenders)
        assert "§6" in message, "the message must point the author at §6"

        assert re.search(r"docs/concepts/sample\.md:\d+", message), (
            "the message must carry an actionable file:line location"
        )


# --------------------------------------------------------------------------
# Anti-silent-pass matcher suite — 8 POSITIVE / 7 NEGATIVE. Each positive maps
# to a §6 leak class the gate mechanizes; each negative locks an FP exclusion.
# Class name carries "Matcher"; methods carry "positive"/"negative" so the
# `-k "Matcher or Positive or Negative"` filter collects exactly these 15.
# --------------------------------------------------------------------------

_POSITIVE_PROSE_LEAKS = [
    "508 D6",  # ① internal doc-ID (qualified)
    "defined in protect.py:99",  # ② file:line citation
    "baldur_dormant.adapters.kafka.consumer.ConsumedEvent",  # ③-a dormant path
    "baldur_pro.services.bulkhead",  # ③-b pro path
    "baldur.adaptive._helpers",  # ④-a-dotted baldur-rooted private segment
    "the `_refresh_state` hook fires",  # ④-a-bare bare private symbol in a span
    "<!-- internal: baldur_pro.adaptive._x -->",  # ③-b inside an HTML comment
    # split via ``+`` (which the formatter does not fold) so this fixture
    # exercises the matcher at runtime without carrying a contiguous private path.
    "see docs/" + "impl/562_concept_guide.md",  # ① path-ref doc-ID
]

_NEGATIVE_PROSE_LEAKS = [
    "_circuit breaker_",  # markdown italic — not a code-span, never extracted
    "`__init__` is public protocol",  # dunder in a span — excluded
    "pip install baldur-pro",  # hyphen, no `baldur_pro.` — clean
    "```python\nself._state = 'open'\n```",  # fenced block — ④-a-bare skips it
    "baldur.core.exceptions",  # baldur-rooted PUBLIC path — no private segment
    "see ../../reference/index.md",  # public See-also link (not impl/laws/...)
    "`BALDUR_CB_ENABLED` defaults to false",  # env var in a span — no leading _
]


class TestProseLeakMatcher:
    """The pure ``find_prose_leaks`` matcher flags every leak class, FP-free."""

    @pytest.mark.parametrize("text", _POSITIVE_PROSE_LEAKS)
    def test_positive_prose_leak_flagged(self, text: str):
        assert find_prose_leaks(text), f"expected a prose leak in {text!r}"

    @pytest.mark.parametrize("text", _NEGATIVE_PROSE_LEAKS)
    def test_negative_prose_clean(self, text: str):
        assert not find_prose_leaks(text), f"unexpected prose leak in {text!r}"


# --------------------------------------------------------------------------
# Inline-code-span extractor — the ④-a-bare scan unit. Confirms the
# newline-excluded `[^`\n]+` body extracts inline spans, skips markdown italics
# and fenced blocks, and that dunder content is extracted-but-not-flagged.
# --------------------------------------------------------------------------


class TestInlineCodeSpanExtractor:
    """The inline-span extractor delimits ④-a-bare's scan unit correctly."""

    def test_inline_span_content_is_extracted(self):
        assert list(iter_inline_code_spans("the `_x` hook")) == ["_x"]

    def test_extracted_span_drives_bare_private_detection(self):
        assert find_prose_leaks("the `_x` hook") == ["_x"]

    def test_dunder_span_extracted_but_not_flagged(self):
        # The span IS extracted, but ④-a-bare excludes the dunder.
        assert list(iter_inline_code_spans("`__init__` here")) == ["__init__"]
        assert not find_prose_leaks("`__init__` here")

    def test_markdown_italic_is_not_a_span(self):
        # No backtick delimiters → never extracted, so ④-a-bare never sees it.
        assert list(iter_inline_code_spans("use _x_ for emphasis")) == []

    def test_fenced_block_is_not_consumed(self):
        # The newline-excluded regex never spans the fence lines, so a bare
        # private symbol inside a fenced block is the ④-b residual §9 owns.
        fenced = "```\nresult = obj._method()\n```"
        assert list(iter_inline_code_spans(fenced)) == []
        assert not find_prose_leaks(fenced)


class TestG29DiscoveryAnchor:
    """663 D5 — the discovery anchor is ``_TEMPLATE.md`` OR a published guide.

    The private repo anchors on the template; the public repo omits it (not
    published) but ships the ``_``-unprefixed published guides. A truly broken
    glob (neither present) still fails loudly, so the anti-vacuous guarantee holds
    in both layouts.
    """

    def test_template_only_is_discoverable(self, tmp_path):
        # The private repo land-time layout: only _TEMPLATE.md present.
        (tmp_path / "_TEMPLATE.md").write_text("# template\n", encoding="utf-8")
        _assert_concepts_dir_discoverable(tmp_path)  # must not raise

    def test_published_guide_only_is_discoverable(self, tmp_path):
        # The public layout: template stripped, >=1 published (_-unprefixed) guide.
        guide_dir = tmp_path / "foundations"
        guide_dir.mkdir()
        (guide_dir / "intro.md").write_text("# intro\n", encoding="utf-8")
        _assert_concepts_dir_discoverable(tmp_path)  # must not raise

    def test_empty_dir_fails_loudly(self, tmp_path):
        # Neither anchor present -> a broken glob; the guard must fail (not pass
        # vacuously on zero files).
        with pytest.raises(AssertionError):
            _assert_concepts_dir_discoverable(tmp_path)

    def test_unpublished_only_fails_loudly(self, tmp_path):
        # Only an underscore-excluded draft (no template, no published guide):
        # _scannable_concept_files excludes it, so the anchor is absent -> fail.
        draft = tmp_path / "_draft"
        draft.mkdir()
        (draft / "wip.md").write_text("# wip\n", encoding="utf-8")
        with pytest.raises(AssertionError):
            _assert_concepts_dir_discoverable(tmp_path)
