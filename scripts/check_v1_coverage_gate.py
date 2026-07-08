"""
v1.0 per-module coverage gate (523 D4) — OSS launch-surface variant.

Reads a pytest-cov JSON report and verifies that every OSS v1.0
launch-surface file meets the per-module coverage threshold (default 80%).
Per 523 D6 the unit of measurement is the module; every target here is a
single file, so each uses that file's coverage summary directly. (The PRO
service-directory targets live in a per-tier copy of this gate in the
private repository — see 699 D6.)

Usage::

    pytest tests/unit -n 6 --timeout=30 \
        --cov=src/baldur \
        --cov-report=json:dist/cov-v1.json
    python scripts/check_v1_coverage_gate.py dist/cov-v1.json

Exit codes::

    0 - all targets meet the threshold
    1 - at least one target is below the threshold (or missing from report)
    2 - usage error (file not found, malformed JSON, etc.)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_COVERAGE_JSON = PROJECT_ROOT / "dist" / "cov-v1.json"
DEFAULT_THRESHOLD = 80.0

# 5 OSS v1.0 launch-surface file targets. Paths are expressed with forward
# slashes; the script normalises coverage.py's native separators for matching.
TARGETS: list[dict[str, str]] = [
    {
        "name": "api/django/reauthentication.py",
        "type": "file",
        "path": "src/baldur/api/django/reauthentication.py",
    },
    {
        "name": "api/django/throttle_adapter.py",
        "type": "file",
        "path": "src/baldur/api/django/throttle_adapter.py",
    },
    {
        "name": "api/django/serializers/pydantic_integration.py",
        "type": "file",
        "path": "src/baldur/api/django/serializers/pydantic_integration.py",
    },
    {
        "name": "api/handlers/canary.py",
        "type": "file",
        "path": "src/baldur/api/handlers/canary.py",
    },
    {
        "name": "api/handlers/compliance.py",
        "type": "file",
        "path": "src/baldur/api/handlers/compliance.py",
    },
]


def _norm(path: str) -> str:
    return path.replace("\\", "/")


def _measure(
    files: dict[str, dict],
    target: dict[str, str],
) -> tuple[int, int, list[str]]:
    """Return (covered_statements, total_statements, matched_file_paths).

    For a directory target, sums across all files whose normalised path
    starts with the target path + "/". For a file target, matches the
    single normalised path.
    """
    target_path = target["path"]
    target_type = target["type"]

    matched: list[str] = []
    covered = 0
    total = 0

    for raw_path, entry in files.items():
        norm_path = _norm(raw_path)
        if target_type == "dir":
            if not norm_path.startswith(target_path + "/"):
                continue
        else:  # file
            if norm_path != target_path:
                continue
        summary = entry.get("summary", {})
        # coverage.py reports num_statements + covered_lines (line-level
        # coverage). Branch coverage is not folded in - the gate matches
        # the doc baseline numbers which are statement-percent_covered.
        total += summary.get("num_statements", 0)
        covered += summary.get("covered_lines", 0)
        matched.append(norm_path)

    return covered, total, matched


def _format_row(
    name: str, percent: float, covered: int, total: int, status: str
) -> str:
    return f"  {status}  {name:<60s} {percent:5.1f}%  ({covered}/{total} stmts)"


def _write_step_summary(
    summary_path: Path,
    threshold: float,
    records: list[tuple[str, float, int, int, str]],
) -> None:
    """Append a markdown table to $GITHUB_STEP_SUMMARY so operators can
    see the per-target verdict on the CI summary tab without scraping
    the raw log. records = list of (name, percent, covered, total, status)."""
    lines: list[str] = [
        f"### v1.0 coverage gate (threshold {threshold:.1f}%)",
        "",
        "| target | coverage | covered/total | status |",
        "|---|---:|---:|---|",
    ]
    for name, percent, covered, total, status in records:
        pct_cell = "n/a" if status == "MISS" else f"{percent:.1f}%"
        lines.append(f"| `{name}` | {pct_cell} | {covered}/{total} | {status} |")
    lines.append("")
    with open(summary_path, "a", encoding="utf-8") as fh:
        fh.write("\n".join(lines))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Enforce v1.0 per-module test coverage gate (doc 523)",
    )
    parser.add_argument(
        "coverage_json",
        type=Path,
        nargs="?",
        default=DEFAULT_COVERAGE_JSON,
        help=f"pytest-cov JSON report (default: {DEFAULT_COVERAGE_JSON.relative_to(PROJECT_ROOT)})",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=DEFAULT_THRESHOLD,
        help=f"Per-module pass threshold in percent (default: {DEFAULT_THRESHOLD})",
    )
    args = parser.parse_args(argv)

    if not args.coverage_json.exists():
        print(
            f"ERROR: coverage report not found at {args.coverage_json}",
            file=sys.stderr,
        )
        return 2

    try:
        with open(args.coverage_json, encoding="utf-8") as fh:
            report = json.load(fh)
    except json.JSONDecodeError as exc:
        print(f"ERROR: malformed JSON in {args.coverage_json}: {exc}", file=sys.stderr)
        return 2

    files = report.get("files", {})
    if not files:
        print(
            f"ERROR: no 'files' entries in {args.coverage_json}",
            file=sys.stderr,
        )
        return 2

    threshold = args.threshold
    failures: list[str] = []
    rows: list[str] = []
    records: list[tuple[str, float, int, int, str]] = []

    for target in TARGETS:
        covered, total, matched = _measure(files, target)
        if total == 0:
            failures.append(
                f"{target['name']}: 0 statements measured (target missing from coverage report)"
            )
            rows.append(_format_row(target["name"], 0.0, 0, 0, "MISS"))
            records.append((target["name"], 0.0, 0, 0, "MISS"))
            continue
        percent = covered * 100.0 / total
        if percent + 1e-9 < threshold:
            status = "FAIL"
            failures.append(
                f"{target['name']}: {percent:.1f}% < {threshold:.1f}% ({covered}/{total})"
            )
        else:
            status = "PASS"
        rows.append(_format_row(target["name"], percent, covered, total, status))
        records.append((target["name"], percent, covered, total, status))

    print(f"v1.0 coverage gate (threshold {threshold:.1f}%):")
    for row in rows:
        print(row)

    summary_env = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_env:
        _write_step_summary(Path(summary_env), threshold, records)

    if failures:
        print("", file=sys.stderr)
        print(f"FAIL: {len(failures)} target(s) below threshold:", file=sys.stderr)
        for line in failures:
            print(f"  - {line}", file=sys.stderr)
        return 1

    print(f"\nOK: all {len(TARGETS)} targets meet the {threshold:.1f}% threshold.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
