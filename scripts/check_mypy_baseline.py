"""
Compare current mypy output against the committed ``.mypy_baseline.json``.

Wave 6D-A introduces this gate to freeze the existing ~2,058-error backlog
so Wave 6D-B can burn it down without new regressions sneaking in. The
script is the mypy analogue of ``scripts/check_wiring_baseline.py``: it
loads a snapshot of errors, diffs against the current run, and fails only
when *new* errors appear. Fixes are accepted silently with a hint to
refresh the baseline.

Usage (CI gate)::

    mypy src/baldur/ --show-error-codes > dist/mypy-now.txt || true
    python scripts/check_mypy_baseline.py dist/mypy-now.txt

Refresh the baseline (after intentional fixes or version bumps)::

    python scripts/check_mypy_baseline.py --refresh dist/mypy-now.txt

Baseline must be captured in the Linux CI environment (ubuntu-latest)
with the pinned mypy version from ``.github/workflows/ci.yml``. Do NOT
commit a baseline captured on macOS/Windows: minor platform differences
(stdlib stubs, path handling) can produce phantom diffs even when the
source has not changed. If a refresh is needed, download
``dist/mypy-now.txt`` from the lint job artifacts or run mypy in WSL.

Exit codes::

    0 - current matches baseline (no regression)
    1 - regression detected (new mypy errors)
    2 - usage error

The match key is ``(file, code, normalized_msg)``. Line numbers are
intentionally excluded because trivial code motion (adding an import,
reordering) would otherwise flag everything below the change as a
"regression". Messages are normalised by collapsing every double-quoted
substring to ``"_"`` so identifier renames inside an existing error
don't masquerade as a new error (precedent: ``dist/analyze_mypy_cat_a.py``
``msg_counter`` normalisation). Absolute line numbers embedded in a
message (e.g. no-redef's "already defined on line 614") are likewise
collapsed to ``on line N`` so code motion does not desync the key.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
BASELINE_PATH = PROJECT_ROOT / ".mypy_baseline.json"

# Matches `path:line: error: message  [code]`. Notes and the summary
# `Found N errors` line are skipped by virtue of not matching.
_ERROR_RE = re.compile(
    r"^(?P<file>.+?):(?P<line>\d+):\s+error:\s+(?P<msg>.*?)\s+\[(?P<code>[\w-]+)\]\s*$"
)

# Normalize quoted identifiers / type names inside the message so renames
# of an existing error are not mistaken for a new error.
_QUOTED_RE = re.compile(r'"[^"]+"')

# Some messages embed an absolute line number, e.g. no-redef's
# "Name X already defined on line 614". Trivial code motion shifts that
# number and makes a stable error masquerade as fixed+new, defeating the
# line-independent match key. Collapse it so only the (file, code, shape)
# key matters.
_ON_LINE_RE = re.compile(r"on line \d+")


def _normalize_path(file: str) -> str:
    """Coerce ``\\`` separators to ``/`` so Windows-captured and
    Linux-captured baselines diff consistently against each other."""
    return file.replace("\\", "/")


def _normalize_msg(msg: str) -> str:
    msg = _QUOTED_RE.sub('"_"', msg)
    return _ON_LINE_RE.sub("on line N", msg)


def _load_mypy_output(path: Path) -> list[dict]:
    """Parse mypy ``--show-error-codes`` output into a list of error dicts.

    Each entry has ``file`` (forward-slash normalised), ``line``,
    ``code``, and ``normalized_msg``. Notes, blank lines, and the
    trailing ``Found N errors`` summary are skipped.
    """
    if not path.exists():
        raise FileNotFoundError(path)

    errors: list[dict] = []
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.rstrip("\n")
            m = _ERROR_RE.match(line)
            if m is None:
                continue
            errors.append(
                {
                    "file": _normalize_path(m["file"]),
                    "line": int(m["line"]),
                    "code": m["code"],
                    "normalized_msg": _normalize_msg(m["msg"]),
                }
            )
    return errors


def _key(entry: dict) -> tuple[str, str, str]:
    """Match key — line excluded so trivial code motion doesn't fire."""
    return (entry["file"], entry["code"], entry["normalized_msg"])


def _diff(
    current: list[dict], baseline: list[dict]
) -> tuple[list[dict], set[tuple[str, str, str]]]:
    """Return (new_errors, fixed_keys).

    ``new_errors`` preserves the current run's entries (so we can echo
    the original line numbers in the failure message) and only includes
    keys not present in baseline. ``fixed_keys`` is the set of baseline
    keys absent from current.
    """
    base_keys = {_key(e) for e in baseline}
    curr_keys = {_key(e) for e in current}

    new_keys = curr_keys - base_keys
    fixed_keys = base_keys - curr_keys

    new_errors = [e for e in current if _key(e) in new_keys]
    return new_errors, fixed_keys


def _write_baseline(errors: list[dict], output: Path | None) -> None:
    """Serialize a sorted, deterministic baseline.

    Sorting by (file, line, code) keeps git diffs minimal across
    refreshes. ``ensure_ascii=False`` matches the wiring-baseline
    style for unicode-friendly diff output.
    """
    errors_sorted = sorted(errors, key=lambda e: (e["file"], e["line"], e["code"]))
    payload = {
        "version": 1,
        "count": len(errors_sorted),
        "errors": errors_sorted,
    }
    text = json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    if output is None:
        sys.stdout.write(text)
    else:
        output.write_text(text, encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Detect mypy regressions against committed baseline"
    )
    parser.add_argument(
        "current",
        type=Path,
        help="Path to current mypy --show-error-codes output",
    )
    parser.add_argument(
        "--baseline",
        type=Path,
        default=BASELINE_PATH,
        help="Baseline JSON (default: .mypy_baseline.json)",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Write the current mypy output as the new baseline (to "
        "--baseline path, or to stdout if --baseline is '-').",
    )
    args = parser.parse_args(argv)

    try:
        current = _load_mypy_output(args.current)
    except FileNotFoundError:
        print(
            f"ERROR: current mypy output not found at {args.current}", file=sys.stderr
        )
        return 2

    if args.refresh:
        if str(args.baseline) == "-":
            _write_baseline(current, None)
        else:
            _write_baseline(current, args.baseline)
            print(
                f"OK: wrote {len(current)} errors to {args.baseline}", file=sys.stderr
            )
        return 0

    if not args.baseline.exists():
        print(f"ERROR: baseline not found at {args.baseline}", file=sys.stderr)
        print(
            "       Generate with: python scripts/check_mypy_baseline.py "
            "--refresh <mypy-output>",
            file=sys.stderr,
        )
        return 2

    try:
        with open(args.baseline, encoding="utf-8") as fh:
            baseline_data = json.load(fh)
    except json.JSONDecodeError as exc:
        print(f"ERROR: malformed baseline JSON {args.baseline}: {exc}", file=sys.stderr)
        return 2

    baseline = baseline_data.get("errors", [])
    new_errors, fixed_keys = _diff(current, baseline)

    if fixed_keys:
        print(
            f"INFO: {len(fixed_keys)} mypy error(s) fixed since baseline.",
            file=sys.stderr,
        )
        print(
            "      Refresh .mypy_baseline.json to lock in the improvement.",
            file=sys.stderr,
        )

    if new_errors:
        print(
            f"FAIL: {len(new_errors)} new mypy error(s) over baseline "
            f"({len(baseline)} baseline, {len(current)} current):",
            file=sys.stderr,
        )
        # Sort by file/line so the operator can navigate the failures in order.
        new_errors.sort(key=lambda e: (e["file"], e["line"]))
        for entry in new_errors:
            print(
                f"  - {entry['file']}:{entry['line']}: "
                f"[{entry['code']}] {entry['normalized_msg']}",
                file=sys.stderr,
            )
        return 1

    print(
        f"OK: mypy stable - {len(current)} error(s), matches baseline "
        f"({len(baseline)})."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
