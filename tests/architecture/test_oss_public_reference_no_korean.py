"""G23 — No Korean on the OSS Public mkdocstrings reference surface.

The v1.0 rendered reference pages
(``/reference/services/``, ``/reference/adapters/memory/``) are produced by
mkdocstrings following the ``__all__`` re-exports of ``baldur.services`` and
``baldur.adapters.memory``. An earlier top-level-only glob
gate that did NOT recurse into sub-packages, so Korean docstrings in
``circuit_breaker/``, ``load_shedding/``, ``idempotency/``, ``security/``,
``layered_repository/`` (etc.) silently shipped to readers while a
``mkdocs build --strict`` still exited 0.

This fitness function is the permanent, CI-enforced replacement for that
one-time PR gate. It walks the LIVE ``__all__`` chains, resolves each exported
symbol to its defining module (``obj.__module__``), and asserts those source
files contain zero Hangul. Because it follows ``__all__`` at runtime, a NEW
sub-package added to ``__all__`` is covered automatically — unlike a hardcoded
path list, which would repeat that blind spot.

Code comments/docstrings/log messages must be English per CLAUDE.md
§ Code Language Rules. Baseline is enforced-empty.

Rule registry: ``ARCHITECTURE.md#g23-oss-reference-no-korean``
"""

from __future__ import annotations

from pathlib import Path

from tests.architecture.conftest import (
    KOREAN_RE,
    PROJECT_ROOT,
    resolve_all_chain_files,
)

# Packages whose ``__all__`` re-exports are rendered onto the v1.0 reference
# surface by ``:::baldur.services`` / ``:::baldur.adapters.memory`` in
# ``docs/reference/`` (see ``mkdocs.yml`` mkdocstrings handler config).
_RENDERED_PACKAGES = (
    "baldur.services",
    "baldur.adapters.memory",
)

_SRC_ROOT = (PROJECT_ROOT / "src" / "baldur").resolve()


def _resolve_reference_source_files() -> set[Path]:
    """Resolve the ``__all__`` chains to the set of defining OSS source files.

    Delegates to the shared ``resolve_all_chain_files`` primitive (the G24
    resolver model: each package's own ``__init__`` file PLUS every ``__all__``
    member's defining file), so G23/G26 never drift on reachability. Re-pointing
    to the shared resolver also ADDS the two rendered packages' own ``__init__``
    module files to the scan — a latent own-file gap the prior member-only walk
    missed (both verified Korean-free).
    """
    return resolve_all_chain_files(_RENDERED_PACKAGES, _SRC_ROOT)


def _korean_lines(path: Path) -> list[tuple[int, str]]:
    hits: list[tuple[int, str]] = []
    text = path.read_text(encoding="utf-8")
    for lineno, line in enumerate(text.splitlines(), start=1):
        if KOREAN_RE.search(line):
            hits.append((lineno, line.strip()))
    return hits


class TestOssPublicReferenceNoKorean:
    """G23 — the mkdocstrings-reachable OSS Public source set is Korean-free."""

    def test_reference_source_set_is_resolvable(self):
        """Sanity guard: the ``__all__`` walk must yield in-tree source files.

        If this returns empty (e.g. an import refactor breaks resolution), the
        no-Korean assertion below would vacuously pass — an anti-silent-pass
        check, mirroring the G20/G21 enforced-empty-baseline discipline.
        """
        files = _resolve_reference_source_files()
        assert files, (
            "G23: resolved zero reference source files from "
            f"{_RENDERED_PACKAGES} __all__ — the walk is broken, so the "
            "no-Korean gate would vacuously pass."
        )

    def test_no_korean_on_reference_surface(self):
        offenders: list[str] = []
        for path in sorted(_resolve_reference_source_files()):
            hits = _korean_lines(path)
            if hits:
                rel = path.relative_to(PROJECT_ROOT).as_posix()
                sample = "; ".join(f"L{n}: {text}" for n, text in hits[:3])
                offenders.append(f"{rel} ({len(hits)} line(s)) — {sample}")

        assert not offenders, (
            f"G23: Korean text found on the OSS Public reference surface "
            f"({len(offenders)} file(s)). Translate to English per CLAUDE.md "
            "§ Code Language Rules — these docstrings render to "
            "baldur.sh/reference/.\n" + "\n".join(offenders)
        )
