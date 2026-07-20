"""G23 — No Korean anywhere in the OSS source tree.

Baldur ships as readable source: nothing is obfuscated or compiled away, so
``src/baldur`` is what a prospective user actually reads to decide whether the
library does what they need. A Korean comment is a wall for that reader — the
code is visible but its intent is not.

This rule started narrower. It covered only the source files reachable from the
``__all__`` chains of ``baldur.services`` and ``baldur.adapters.memory``, because
those are what mkdocstrings renders onto the published reference pages. That
scope was right while the rest of the tree still held Korean and a wider gate
would have blocked every run. The tree is now Korean-free end to end, so the
scope widens to match the real requirement, and the rendered-reference surface
is covered as a strict subset.

Scope is every ``*.py`` under ``src/baldur``, matched line by line against
Hangul. Comments, docstrings, log messages and string literals alike must be
English per CLAUDE.md § Code Language Rules. Baseline is enforced-empty: a new
Korean line is a failure, never a baseline entry.

Rule registry: ``ARCHITECTURE.md#g23-oss-source-no-korean``
"""

from __future__ import annotations

from pathlib import Path

from tests.architecture.conftest import KOREAN_RE, PROJECT_ROOT

_SRC_ROOT = (PROJECT_ROOT / "src" / "baldur").resolve()

# Anti-vacuous floor. The tree holds well over a thousand modules; if a path or
# packaging change ever makes the walk resolve to a handful of files, the
# no-Korean assertion would pass without having read anything. Deliberately far
# below the real count so ordinary growth or pruning never trips it.
_MIN_EXPECTED_FILES = 400


def _source_files() -> list[Path]:
    return sorted(p for p in _SRC_ROOT.rglob("*.py") if p.is_file())


def _korean_lines(path: Path) -> list[tuple[int, str]]:
    hits: list[tuple[int, str]] = []
    text = path.read_text(encoding="utf-8")
    for lineno, line in enumerate(text.splitlines(), start=1):
        if KOREAN_RE.search(line):
            hits.append((lineno, line.strip()))
    return hits


class TestOssSourceNoKorean:
    """G23 — the OSS source tree is Korean-free."""

    def test_source_set_is_resolvable(self):
        """Sanity guard: the walk must find the source tree it claims to scan.

        Without this, a broken root path turns the gate below into a silent
        pass — the same anti-vacuous discipline as the G20/G21 enforced-empty
        baselines.
        """
        files = _source_files()
        assert len(files) >= _MIN_EXPECTED_FILES, (
            f"G23: resolved only {len(files)} source file(s) under {_SRC_ROOT} "
            f"(expected at least {_MIN_EXPECTED_FILES}) — the walk is broken, "
            "so the no-Korean gate would vacuously pass."
        )

    def test_no_korean_in_oss_source(self):
        offenders: list[str] = []
        for path in _source_files():
            hits = _korean_lines(path)
            if hits:
                rel = path.relative_to(PROJECT_ROOT).as_posix()
                sample = "; ".join(f"L{n}: {text}" for n, text in hits[:3])
                offenders.append(f"{rel} ({len(hits)} line(s)) — {sample}")

        assert not offenders, (
            f"G23: Korean text found in the OSS source tree "
            f"({len(offenders)} file(s)). Translate to English per CLAUDE.md "
            "§ Code Language Rules — this source is published as-is, and part "
            "of it renders to baldur.sh/reference/.\n" + "\n".join(offenders)
        )
