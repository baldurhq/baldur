#!/usr/bin/env python3
"""Leak guard — keep private references out of the public-first OSS repository.

The OSS core develops in the open. The commercial tier and its internal
documentation live in a separate private repository; the private *source* never
travels. This guard is the thin forward defense against accidentally committing
a *textual reference* to a private artifact — the private repository name, a
private documentation tree, or an internal architecture-decision id — which
content-scrubbing would otherwise let slip into the public history.

Self-disabling. In the private monorepo the private packages exist on disk, so
the guard exits 0 immediately and is INERT there: every monorepo commit —
including ones that legitimately touch the private trees or reference internal
docs — is unaffected. It does real work only in the public-first repository,
where the private packages are absent.

It is wired as a repo-local pre-commit hook (content + commit-message stages);
a CI job re-runs the identical scan on push/PR so ``git commit --no-verify``
only defers the failure to CI and cannot ship a leak.

Token policy (private-only references, chosen for a near-zero false-positive
rate in public OSS text):

  Scanned in BOTH staged content and the commit message — zero legitimate use
  anywhere in the public tree:
    * the private repository name
    * private documentation paths ``docs/{laws,impl,maintainer,self_healing}/``
    * internal architecture-decision refs ``ADR-<n>``

  Scanned in the commit MESSAGE ONLY — the private source-tree paths
  ``src/baldur_{pro,dormant}/``. They are deliberately not content-scanned: the
  OSS boundary tests reference those paths legitimately (a gate that enforces
  the boundary must be able to name it), and the bare package name is the public
  product name. A commit message, by contrast, has no reason to spell a private
  source path, so there it is treated as a leak.

  Deliberately NOT scanned anywhere (public-repo norms — avoid friction):
  the bare product names, ``#<n>`` issue refs, ``G<nn>`` gate numbers (public,
  documented in ARCHITECTURE.md), and internal release-campaign refs.

Run modes:
    check_leak_guard.py FILE...                 scan staged file content
    check_leak_guard.py --commit-msg FILE       scan a commit-message file
    check_leak_guard.py --all                   scan every tracked text file
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

# The private repository name, assembled at runtime via ``+`` (which the
# formatter does not fold, unlike adjacent string literals) so this guard does
# not itself carry the literal token it scans for — belt-and-suspenders on top
# of the self-exclusion below.
_PRIVATE_REPO_NAME = "self" + "healing-python"

# Private documentation trees (excluded from publication) — a path into any of
# them has zero legitimate use in public OSS content.
PRIVATE_DOC_PATHS: tuple[str, ...] = (
    "docs/laws/",
    "docs/impl/",
    "docs/maintainer/",
    "docs/self_healing/",
)

# Private source trees. Content-allowed (the OSS boundary tests reference these
# paths legitimately), but a leak in a commit message — scanned there only.
PRIVATE_SOURCE_PATHS: tuple[str, ...] = (
    "src/baldur_pro/",
    "src/baldur_dormant/",
)

# (label, compiled pattern). References with zero legitimate use in public OSS
# content. The private source paths above are intentionally absent here (see the
# module docstring) and added only for the commit-message scan.
CONTENT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("private repository name", re.compile(re.escape(_PRIVATE_REPO_NAME))),
    *(
        ("private documentation path", re.compile(re.escape(p)))
        for p in PRIVATE_DOC_PATHS
    ),
    ("internal architecture-decision ref", re.compile(r"\bADR-\d+\b")),
)

# Commit messages additionally must not spell a private source-tree path.
MESSAGE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = CONTENT_PATTERNS + tuple(
    ("private source-tree path", re.compile(re.escape(p))) for p in PRIVATE_SOURCE_PATHS
)

# This guard legitimately spells the doc-path / ADR patterns it scans for, so it
# is excluded from the content scan (the established scanner self-exclusion
# precedent). Its unit test plants positive samples and is excluded likewise.
_SELF_EXCLUDE: frozenset[str] = frozenset(
    {
        "scripts/check_leak_guard.py",
        "tests/unit/test_leak_guard.py",
    }
)


def scan(
    text: str, patterns: tuple[tuple[str, re.Pattern[str]], ...]
) -> list[tuple[str, str]]:
    """Return ``(label, matched-substring)`` for every private reference in ``text``."""
    hits: list[tuple[str, str]] = []
    for label, pattern in patterns:
        for match in pattern.finditer(text):
            hits.append((label, match.group(0)))
    return hits


def find_content_leaks(text: str) -> list[tuple[str, str]]:
    """Private references that may not appear in public *file content*."""
    return scan(text, CONTENT_PATTERNS)


def find_message_leaks(text: str) -> list[tuple[str, str]]:
    """Private references that may not appear in a public *commit message*."""
    return scan(text, MESSAGE_PATTERNS)


def is_private_monorepo(repo_root: Path) -> bool:
    """True in the private monorepo, where the private packages exist on disk.

    The guard is inert when this is True — references to the private trees are
    legitimate where those trees actually live.
    """
    return (repo_root / "src" / "baldur_pro").is_dir()


def _repo_root() -> Path:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=True,
        )
        return Path(out.stdout.strip())
    except (subprocess.CalledProcessError, OSError):
        return Path.cwd()


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None  # binary / unreadable — nothing textual to leak


def _tracked_text_files(repo_root: Path) -> list[Path]:
    try:
        out = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=True,
        )
    except (subprocess.CalledProcessError, OSError):
        return []
    return [repo_root / rel for rel in out.stdout.split("\0") if rel]


def _rel(path: Path, repo_root: Path) -> str:
    try:
        return path.resolve().relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def _scan_message_files(files: list[str]) -> list[tuple[str, str, str]]:
    """Scan commit-message file(s) for the commit-message token set."""
    failures: list[tuple[str, str, str]] = []
    for name in files:
        text = _read_text(Path(name))
        if text is None:
            continue
        failures.extend((name, label, hit) for label, hit in find_message_leaks(text))
    return failures


def _scan_content_files(
    paths: list[Path], repo_root: Path
) -> list[tuple[str, str, str]]:
    """Scan file content for the content token set, skipping the guard's own files."""
    failures: list[tuple[str, str, str]] = []
    for path in paths:
        rel = _rel(path, repo_root)
        if rel in _SELF_EXCLUDE:
            continue
        text = _read_text(path)
        if text is None:
            continue
        failures.extend((rel, label, hit) for label, hit in find_content_leaks(text))
    return failures


def _report(failures: list[tuple[str, str, str]]) -> None:
    print(
        "Leak guard: private reference(s) found — these must not reach the "
        "public repository:",
        file=sys.stderr,
    )
    for where, label, hit in failures:
        print(f"  {where}: {label}: {hit!r}", file=sys.stderr)
    print(
        "\nRemove the reference (repoint a private-doc path to ARCHITECTURE.md, "
        "drop an internal id). If it is a genuine false positive, adjust the "
        "token policy in scripts/check_leak_guard.py with review.",
        file=sys.stderr,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--commit-msg",
        action="store_true",
        help="scan commit-message file(s) (uses the commit-message token set)",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="scan every tracked text file's content (CI backstop)",
    )
    parser.add_argument("files", nargs="*", help="files to scan")
    args = parser.parse_args(argv)

    repo_root = _repo_root()

    # Inert in the private monorepo — references to private trees are legitimate
    # where those trees actually exist.
    if is_private_monorepo(repo_root):
        return 0

    if args.commit_msg:
        failures = _scan_message_files(args.files)
    else:
        paths = (
            _tracked_text_files(repo_root)
            if args.all
            else [Path(name) for name in args.files]
        )
        failures = _scan_content_files(paths, repo_root)

    if failures:
        _report(failures)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
