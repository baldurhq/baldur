#!/usr/bin/env python3
"""533 - import-graph PRO-leak classifier for ``tests/``.

Replaces the 528 catalog-path-mirror + all-or-nothing classifier (533 G1: it
reported 0 move candidates because the test layout diverges from ``src`` module
paths — e.g. ``baldur_pro.services.throttle`` source vs ``tests/unit/throttle/``
test dir — and one unmarked file pinned a whole subtree). This version
classifies every ``.py`` file under ``tests/`` by its production-import
graph (file-level, not directory-level) and emits:

  - an auto-move plan (``pure_pro`` + ``support_only``) -> ``git mv`` to ``tests/pro/``
  - the ``pro_dominant`` split (move all except the named STAY exceptions) +
    ``true_boundary`` (stay) as a manual-review report
  - a private-leak report (files importing a ``baldur_pro`` / ``baldur_dormant``
    private symbol, a private-module path, or a wildcard) — the SUT=PRO
    move/split override of 533 D1

Why import-graph, not catalog-path-mirror: the catalog approach is
structurally blind (test dirs do not mirror ``src`` paths; one unmarked file
in a mixed dir pins the subtree). The file-level production-import graph is the
robust SUT signal. ``tests.*`` (test-infra) and OSS support-prefix
``baldur.*`` imports are excluded from the SUT determination.

The per-file verdict is exposed as the pure function :func:`classify_source` /
:func:`classify_file` so the G21 fitness function
(``tests/architecture/test_oss_tests_pro_classification.py``) reuses it as
the single source of truth (533 D12) — there is no second copy to drift. The
function does pure AST/string analysis and never imports ``baldur_pro``, so it
runs unchanged in the public mirror where the PRO package source is absent.

Usage:
    python scripts/classify_pro_importing_tests.py                 # human report
    python scripts/classify_pro_importing_tests.py --format git-mv # shell move plan
    python scripts/classify_pro_importing_tests.py --format move-plan
    python scripts/classify_pro_importing_tests.py --tests-root X --target-root Y
"""

from __future__ import annotations

import argparse
import ast
import sys
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_TESTS_ROOT = PROJECT_ROOT / "tests" / "oss"
DEFAULT_TARGET_ROOT = PROJECT_ROOT / "tests" / "pro"

PRO_PACKAGE = "baldur_pro"
DORMANT_PACKAGE = "baldur_dormant"
OSS_PACKAGE = "baldur"
GATED_PACKAGES: tuple[str, ...] = (PRO_PACKAGE, DORMANT_PACKAGE)

# OSS submodules that are shared "support" surface, not a system-under-test:
# importing only these alongside baldur_pro still means the SUT is PRO (533 D2).
SUPPORT_PREFIXES: tuple[str, ...] = (
    "baldur.core.exceptions",
    "baldur.settings",
    "baldur.utils",
    "baldur.models",
    "baldur.context",
    "baldur.interfaces",
    "baldur.constants",
)

# The 8 pro_dominant boundary tests that legitimately STAY under tests/
# (533 D3, authoritative named list). Matched by file basename. These keep
# their requires_pro marker and are content-clean of private imports
# (test_throttle_config_applier is content-fixed in place per 533 D7).
STAY_BASENAMES: frozenset[str] = frozenset(
    {
        "test_pro_registration_flow.py",
        "test_throttle_config_applier.py",
        "test_throttle_adapter.py",
        "test_emergency_handlers.py",
        "test_serializable_migration_363b.py",
        "test_shutdown_handlers_399.py",
        "test_canary_error_budget_gate.py",
        "test_audit_helpers_compliance_finops.py",
    }
)

# Verdict constants (also the G21 gate keys).
PURE_PRO = "pure_pro"
SUPPORT_ONLY = "support_only"
PRO_DOMINANT = "pro_dominant"
TRUE_BOUNDARY = "true_boundary"
NONE = "none"

# Verdicts G21 fails on: the file's SUT is PRO and it leaks at the mirror build.
LEAKING_VERDICTS: frozenset[str] = frozenset({PURE_PRO, SUPPORT_ONLY})


@dataclass(frozen=True)
class FileVerdict:
    """Pure result of import-graph analysis of one test file.

    ``verdict`` is the count-based SUT classification (the G21 gate keys off
    ``verdict in LEAKING_VERDICTS``). ``private_imports`` / ``wildcard_imports``
    are the orthogonal private-leak axis (533 D1 SUT-override / G20).
    """

    verdict: str
    pro_count: int
    oss_count: int
    has_dormant: bool
    private_imports: tuple[str, ...]
    wildcard_imports: tuple[str, ...]
    nonsupport_oss: tuple[str, ...]

    @property
    def has_private_leak(self) -> bool:
        return bool(self.private_imports) or bool(self.wildcard_imports)


def _top_level(name: str | None) -> str | None:
    if not name:
        return None
    return name.split(".", 1)[0]


def _is_support(module: str | None) -> bool:
    if not module:
        return False
    return any(module == p or module.startswith(p + ".") for p in SUPPORT_PREFIXES)


def _is_private_component(component: str) -> bool:
    """True for a private dotted-path component: ``_x`` but not a dunder."""
    if not component.startswith("_"):
        return False
    if component.startswith("__") and component.endswith("__"):
        return False
    return True


def _is_private_symbol(name: str) -> bool:
    """True for an imported private symbol: ``_foo`` but not a dunder (e.g. ``__all__``)."""
    return _is_private_component(name)


def _module_has_private_component(module: str | None) -> bool:
    """True when any dotted component of ``module`` is private (533 D6)."""
    if not module:
        return False
    return any(_is_private_component(part) for part in module.split("."))


def classify_source(source: str, filename: str = "<unknown>") -> FileVerdict | None:
    """Classify one test file's production-import graph. Pure; never imports PRO.

    Returns ``None`` when the source cannot be parsed. The verdict is
    count-based (533 D1); private-symbol / private-module / wildcard leaks are
    reported separately on the same result so callers can apply the SUT=PRO
    move/split override (G20).
    """
    try:
        tree = ast.parse(source, filename=filename)
    except (SyntaxError, ValueError):
        return None

    pro_count = oss_count = 0
    has_dormant = False
    private_imports: list[str] = []
    wildcard_imports: list[str] = []
    oss_modules: list[str] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            tl = _top_level(node.module)
            if tl in GATED_PACKAGES:
                if tl == PRO_PACKAGE:
                    pro_count += 1
                else:
                    has_dormant = True
                module = node.module or tl
                if _module_has_private_component(module):
                    private_imports.append(module)
                for alias in node.names:
                    if alias.name == "*":
                        wildcard_imports.append(f"{module}.*")
                    elif _is_private_symbol(alias.name):
                        private_imports.append(f"{module}.{alias.name}")
            elif tl == OSS_PACKAGE:
                oss_count += 1
                oss_modules.append(node.module or OSS_PACKAGE)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                tl = _top_level(alias.name)
                if tl in GATED_PACKAGES:
                    if tl == PRO_PACKAGE:
                        pro_count += 1
                    else:
                        has_dormant = True
                    if _module_has_private_component(alias.name):
                        private_imports.append(alias.name)
                elif tl == OSS_PACKAGE:
                    oss_count += 1
                    oss_modules.append(alias.name)

    nonsupport = tuple(sorted({m for m in oss_modules if not _is_support(m)}))

    if pro_count == 0:
        verdict = NONE
    elif oss_count == 0:
        verdict = PURE_PRO
    elif all(_is_support(m) for m in oss_modules):
        verdict = SUPPORT_ONLY
    elif pro_count > oss_count:
        verdict = PRO_DOMINANT
    else:
        verdict = TRUE_BOUNDARY

    return FileVerdict(
        verdict=verdict,
        pro_count=pro_count,
        oss_count=oss_count,
        has_dormant=has_dormant,
        private_imports=tuple(dict.fromkeys(private_imports)),
        wildcard_imports=tuple(dict.fromkeys(wildcard_imports)),
        nonsupport_oss=nonsupport,
    )


def classify_file(path: Path) -> FileVerdict | None:
    """Read and classify a file. Returns ``None`` on read/parse failure."""
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    return classify_source(source, filename=str(path))


def _walk_tests(tests_root: Path):
    for path in sorted(tests_root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        yield path


def _target_path(path: Path, tests_root: Path, target_root: Path) -> Path:
    """Mirror ``tests/<rest>`` -> ``tests/pro/<rest>``."""
    return target_root / path.relative_to(tests_root)


@dataclass(frozen=True)
class Classification:
    auto_move: list[Path]  # pure_pro + support_only
    pro_dominant_move: list[Path]  # pro_dominant minus STAY (the 44)
    stay: list[Path]  # pro_dominant STAY (8) + true_boundary
    private_leak: list[Path]  # any verdict, imports private/wildcard
    verdicts: dict[Path, FileVerdict]


def classify_tree(tests_root: Path) -> Classification:
    verdicts: dict[Path, FileVerdict] = {}
    auto_move: list[Path] = []
    pro_dominant_move: list[Path] = []
    stay: list[Path] = []
    private_leak: list[Path] = []

    for path in _walk_tests(tests_root):
        result = classify_file(path)
        if result is None:
            continue
        verdicts[path] = result
        # The private-leak report covers ALL .py (incl. conftests): a staying
        # conftest leaking a PRO private symbol is a real leak G20 must catch.
        if result.has_private_leak and result.verdict != NONE:
            private_leak.append(path)
        # The move/stay buckets cover only test files. conftest.py / __init__.py
        # are directory infrastructure — they follow their dir's test files
        # (moved with the dir when it empties, copied when it splits), never
        # relocated on their own import verdict. Bucketing them here would, e.g.,
        # wrongly move tests/unit/rate_limit/conftest.py (a pro_dominant
        # conftest) out from under its staying OSS rate_limit tests.
        if not path.name.startswith("test_"):
            continue
        if result.verdict in (PURE_PRO, SUPPORT_ONLY):
            auto_move.append(path)
        elif result.verdict == PRO_DOMINANT:
            if path.name in STAY_BASENAMES:
                stay.append(path)
            else:
                pro_dominant_move.append(path)
        elif result.verdict == TRUE_BOUNDARY:
            stay.append(path)

    return Classification(
        auto_move=auto_move,
        pro_dominant_move=pro_dominant_move,
        stay=stay,
        private_leak=private_leak,
        verdicts=verdicts,
    )


def _rel(path: Path) -> str:
    try:
        return path.relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def _emit_git_mv(
    classification: Classification, tests_root: Path, target_root: Path
) -> str:
    lines: list[str] = []
    for path in classification.auto_move + classification.pro_dominant_move:
        dst = _target_path(path, tests_root, target_root)
        lines.append(f"git mv {_rel(path)} {_rel(dst)}")
    return "\n".join(lines)


def _emit_move_plan(
    classification: Classification, tests_root: Path, target_root: Path
) -> str:
    lines: list[str] = []
    for path in classification.auto_move + classification.pro_dominant_move:
        dst = _target_path(path, tests_root, target_root)
        lines.append(f"{_rel(path)} -> {_rel(dst)}")
    return "\n".join(lines)


def _summary_counts(classification: Classification) -> dict[str, int]:
    """Per-verdict counts over TEST files only.

    Matches the move-plan and G21 scope: conftest.py / __init__.py are
    directory infrastructure, not classified tests (a PRO-importing conftest
    that legitimately serves staying boundary tests is acceptable public-API
    surface — 533 R2 — and is checked for private leaks by G20, not relocated).
    """
    counts = {PURE_PRO: 0, SUPPORT_ONLY: 0, PRO_DOMINANT: 0, TRUE_BOUNDARY: 0}
    for path, verdict in classification.verdicts.items():
        if not path.name.startswith("test_"):
            continue
        if verdict.verdict in counts:
            counts[verdict.verdict] += 1
    return counts


def _infra_verdicts(classification: Classification) -> list[tuple[str, Path]]:
    """PRO-importing non-test infrastructure (conftest/helpers) that stays."""
    out: list[tuple[str, Path]] = []
    for path, verdict in sorted(classification.verdicts.items()):
        if path.name.startswith("test_"):
            continue
        if verdict.verdict in (PURE_PRO, SUPPORT_ONLY, PRO_DOMINANT, TRUE_BOUNDARY):
            out.append((verdict.verdict, path))
    return out


def _emit_text_report(classification: Classification) -> str:
    counts = _summary_counts(classification)
    lines: list[str] = []
    lines.append("533 import-graph PRO-leak classifier (tests/)")
    lines.append("")
    lines.append("Verdict counts (TEST files importing baldur_pro):")
    lines.append(f"  pure_pro      : {counts[PURE_PRO]}")
    lines.append(f"  support_only  : {counts[SUPPORT_ONLY]}")
    lines.append(f"  pro_dominant  : {counts[PRO_DOMINANT]}")
    lines.append(f"  true_boundary : {counts[TRUE_BOUNDARY]}")
    lines.append("")
    infra = _infra_verdicts(classification)
    lines.append(
        "PRO-importing non-test infra (conftest/helpers; stays, G20 checks private): "
        f"{len(infra)}"
    )
    for verdict_name, path in infra:
        lines.append(f"  [{verdict_name}] {_rel(path)}")
    lines.append("")
    lines.append(
        f"=> auto-move (pure_pro + support_only) : {len(classification.auto_move)}"
    )
    lines.append(
        f"=> pro_dominant move (minus STAY)      : {len(classification.pro_dominant_move)}"
    )
    lines.append(
        f"=> STAY (pro_dominant STAY + true_boundary): {len(classification.stay)}"
    )
    lines.append(
        f"=> private-leak (move/split override)  : {len(classification.private_leak)}"
    )
    lines.append("")
    lines.append("=== STAY pro_dominant (named exceptions, 533 D3) ===")
    for path in classification.stay:
        verdict = classification.verdicts[path]
        if verdict.verdict == PRO_DOMINANT:
            lines.append(
                f"  [pro={verdict.pro_count} oss={verdict.oss_count}] {_rel(path)}"
            )
    lines.append("")
    lines.append("=== private-leak files (G20 must be empty after move) ===")
    for path in classification.private_leak:
        verdict = classification.verdicts[path]
        lines.append(f"  [{verdict.verdict}] {_rel(path)}")
        for sym in verdict.private_imports:
            lines.append(f"        priv: {sym}")
        for wild in verdict.wildcard_imports:
            lines.append(f"        wild: {wild}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--tests-root", type=Path, default=DEFAULT_TESTS_ROOT)
    parser.add_argument("--target-root", type=Path, default=DEFAULT_TARGET_ROOT)
    parser.add_argument(
        "--format",
        choices=("text", "git-mv", "move-plan"),
        default="text",
        help="text: human report (default). git-mv: shell move commands. "
        "move-plan: SRC -> DST lines.",
    )
    args = parser.parse_args(argv)

    if not args.tests_root.exists():
        print(f"ERROR: tests root not found: {args.tests_root}", file=sys.stderr)
        return 2

    classification = classify_tree(args.tests_root)

    if args.format == "git-mv":
        print(_emit_git_mv(classification, args.tests_root, args.target_root))
    elif args.format == "move-plan":
        print(_emit_move_plan(classification, args.tests_root, args.target_root))
    else:
        print(_emit_text_report(classification))
    return 0


if __name__ == "__main__":
    sys.exit(main())
