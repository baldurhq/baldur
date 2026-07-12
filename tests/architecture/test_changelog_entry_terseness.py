"""G63 — CHANGELOG entries stay terse (single line, bounded length).

The public ``CHANGELOG.md`` is a per-release delta of notable changes in
disciplined Keep-a-Changelog form: one terse line per entry — a feature/change
noun plus at most one factual impact clause. Implementation mechanics, migration
tables, and "previously … now …" narratives belong in the concept guides, not
here. Without a gate this drifts: a busy release inlines the full "why it broke"
story into each bullet and the file balloons into release-notes prose.

This rule mechanizes two objective terseness properties over every entry — a
``- `` bullet under a ``### Category`` inside a ``## [version]`` section:

* **single line** — every non-blank line inside a category block must itself be a
  top-level ``- `` entry. An indented continuation line, a wrapped paragraph, a
  sub-bullet, or an embedded ``| … |`` migration table is a multi-line entry and
  fails. Preamble prose before the first ``### Category`` (the file header, a
  version's inaugural note) is not inside a category and is not checked.
* **bounded length** — each entry line is at most ``MAX_ENTRY_CHARS`` characters.

Both properties are pure string checks, so the matcher is unit-tested against
verbose / terse / table / length fixtures (anti-silent-pass), and the real-file
test additionally asserts the parser found a non-empty entry set — so a path or
parse break fails loudly instead of passing vacuously.

Rule registry: ``ARCHITECTURE.md#g63-changelog-entry-terseness``
"""

from __future__ import annotations

from collections.abc import Iterator

from tests.architecture.conftest import PROJECT_ROOT

# Entries are bare one-liners: the change noun (plus, for a breaking change, the
# replacement symbol) and nothing else — no mechanism, before-state, or scope.
# The longest such entry measures ~85 chars; the collapsed paragraphs this gate
# replaced ran 350-1500. 100 clears a one-liner with margin while rejecting any
# return to multi-sentence release-notes prose.
MAX_ENTRY_CHARS = 100

CHANGELOG_PATH = PROJECT_ROOT / "CHANGELOG.md"


def iter_category_lines(text: str) -> Iterator[tuple[int, str, bool]]:
    """Yield ``(lineno, line, is_continuation)`` for every category-body line.

    Walks ``text`` tracking whether the cursor is inside a ``### Category`` block
    of a ``## [version]`` section. Within such a block every non-blank line is
    yielded: a line starting with ``- `` is a top-level entry
    (``is_continuation=False``); any other non-blank line is a continuation —
    an indented wrap, a sub-bullet, or a table row (``is_continuation=True``).
    Lines outside a category block (the file header, a version's preamble note)
    are skipped. Pure function.
    """
    in_version = False
    in_category = False
    for lineno, line in enumerate(text.splitlines(), start=1):
        if line.startswith("## "):
            in_version = True
            in_category = False
            continue
        if line.startswith("### "):
            in_category = in_version
            continue
        if not in_category or not line.strip():
            continue
        yield lineno, line, not line.startswith("- ")


def find_changelog_violations(
    text: str, max_chars: int = MAX_ENTRY_CHARS
) -> list[tuple[int, str, str]]:
    """Return ``(lineno, kind, detail)`` terseness violations in ``text`` (pure).

    ``kind`` is ``"multiline"`` (a continuation / table / sub-bullet line breaks
    the single-line rule) or ``"too_long"`` (a ``- `` entry exceeds ``max_chars``).
    """
    violations: list[tuple[int, str, str]] = []
    for lineno, line, is_continuation in iter_category_lines(text):
        if is_continuation:
            violations.append((lineno, "multiline", line.strip()[:60]))
        elif len(line) > max_chars:
            violations.append((lineno, "too_long", f"{len(line)} chars"))
    return violations


class TestChangelogEntryTerseness:
    """G63 — every CHANGELOG entry is a single line within the length bound."""

    def test_changelog_present(self) -> None:
        assert CHANGELOG_PATH.is_file(), (
            f"G63: {CHANGELOG_PATH.name} not found — the terseness gate would "
            "pass vacuously."
        )

    def test_parser_finds_entries(self) -> None:
        """Anti-vacuous-pass: the real file parses into a non-empty entry set."""
        text = CHANGELOG_PATH.read_text(encoding="utf-8")
        entries = [
            lineno
            for lineno, _line, is_continuation in iter_category_lines(text)
            if not is_continuation
        ]
        assert len(entries) >= 10, (
            f"G63: parsed only {len(entries)} entries from CHANGELOG.md — the "
            "section parser is broken, so the gate would pass vacuously."
        )

    def test_entries_are_terse(self) -> None:
        text = CHANGELOG_PATH.read_text(encoding="utf-8")
        violations = find_changelog_violations(text)
        formatted: list[str] = []
        for lineno, kind, detail in violations:
            if kind == "multiline":
                formatted.append(
                    f"  CHANGELOG.md:{lineno} — entry is not a single line "
                    f"(continuation / table / sub-bullet): {detail!r}"
                )
            else:
                formatted.append(
                    f"  CHANGELOG.md:{lineno} — entry exceeds "
                    f"{MAX_ENTRY_CHARS} chars ({detail})"
                )
        assert not violations, (
            f"G63: {len(violations)} non-terse CHANGELOG entr(y/ies). Each entry "
            "is one line — a feature/change noun plus at most one factual impact "
            "clause; push implementation mechanics, migration tables, and "
            "'previously … now …' narratives to the concept guides.\n"
            + "\n".join(formatted)
        )


# --------------------------------------------------------------------------
# Anti-silent-pass unit tests — the pure matcher must flag a too-long entry, a
# migration table, and a wrapped continuation, and must leave a terse fixture
# (plus over-length preamble prose that is NOT an entry) clean.
# --------------------------------------------------------------------------

_FIXTURE_TERSE = """# Changelog

## [Unreleased]

### Added

- A short terse entry describing one capability.
- `@retry` — a single decorator for sync and async, auto-detecting the call style.

### Removed

- `OldThing` — removed; use `NewThing` instead.
"""

_FIXTURE_TOO_LONG = (
    "# Changelog\n\n## [Unreleased]\n\n### Added\n\n- " + ("x" * 300) + "\n"
)

_FIXTURE_TABLE = """# Changelog

## [Unreleased]

### Changed

- Something moved. **Breaking** migration table:

  | Old | New |
  |-----|-----|
  | `A` | `B` |
"""

_FIXTURE_CONTINUATION = """# Changelog

## [Unreleased]

### Fixed

- First line of an entry
  that wraps onto a second indented line.
"""

# Over-length header prose and an over-length inaugural note, neither of which is
# a ``- `` entry inside a category — the parser must not flag either.
_FIXTURE_PREAMBLE_OK = (
    "# Changelog\n\n"
    + "Header prose that is deliberately far longer than the entry ceiling "
    + ("y" * 300)
    + "\n\n## [1.0.0] - 2026-06-23\n\n"
    + "This inaugural note is also longer than the ceiling but is not an entry "
    + ("z" * 300)
    + "\n\n### Added\n\n- Circuit Breaker — stops calling a failing dependency.\n"
)


class TestChangelogTersenessMatcher:
    """The pure ``find_changelog_violations`` matcher is correct and FP-free."""

    def test_terse_fixture_clean(self) -> None:
        assert find_changelog_violations(_FIXTURE_TERSE) == []

    def test_too_long_flagged(self) -> None:
        violations = find_changelog_violations(_FIXTURE_TOO_LONG)
        assert len(violations) == 1
        assert violations[0][1] == "too_long"

    def test_table_rows_flagged_as_multiline(self) -> None:
        kinds = {
            kind for _lineno, kind, _detail in find_changelog_violations(_FIXTURE_TABLE)
        }
        assert kinds == {"multiline"}, (
            "an embedded migration table must trip the single-line rule"
        )

    def test_wrapped_continuation_flagged(self) -> None:
        violations = find_changelog_violations(_FIXTURE_CONTINUATION)
        assert [kind for _lineno, kind, _detail in violations] == ["multiline"]

    def test_preamble_prose_not_checked(self) -> None:
        assert find_changelog_violations(_FIXTURE_PREAMBLE_OK) == [], (
            "prose outside a ### category (file header, inaugural note) is not "
            "an entry and must never be flagged, regardless of length"
        )
