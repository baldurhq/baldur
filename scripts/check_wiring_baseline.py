"""
Compare current wiring state against the committed `.wiring_baseline.json`.

Usage (CI gate)::

    python scripts/verify_wiring.py --json > /tmp/wiring_now.json
    python scripts/check_wiring_baseline.py /tmp/wiring_now.json

Exit codes::

    0 — current state matches baseline (no regression)
    1 — regression detected (new orphans, fewer connected services, or
        new dep-graph warnings)
    2 — usage error

The baseline only protects against `regression`. Improvements (orphans
disappearing, more services connected) are silently accepted; the
operator is expected to refresh `.wiring_baseline.json` after
intentional improvements::

    python scripts/verify_wiring.py --json > .wiring_baseline.json
    python -c "import json; d=json.load(open('.wiring_baseline.json', encoding='utf-8')); d.pop('timestamp', None); json.dump(d, open('.wiring_baseline.json','w',encoding='utf-8'), indent=2, ensure_ascii=False, sort_keys=True)"
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
BASELINE_PATH = PROJECT_ROOT / ".wiring_baseline.json"


def _load(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _orphan_names(report: dict) -> set[str]:
    return {o["name"] for o in report.get("orphans", []) if "name" in o}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Detect wiring regressions against committed baseline"
    )
    parser.add_argument(
        "current",
        type=Path,
        help="Path to current verify_wiring.py --json output",
    )
    parser.add_argument(
        "--baseline",
        type=Path,
        default=BASELINE_PATH,
        help="Baseline JSON (default: .wiring_baseline.json)",
    )
    args = parser.parse_args()

    if not args.baseline.exists():
        print(f"ERROR: baseline not found at {args.baseline}", file=sys.stderr)
        return 2
    if not args.current.exists():
        print(f"ERROR: current report not found at {args.current}", file=sys.stderr)
        return 2

    baseline = _load(args.baseline)
    current = _load(args.current)

    base_orphans = _orphan_names(baseline)
    curr_orphans = _orphan_names(current)

    new_orphans = curr_orphans - base_orphans
    fixed_orphans = base_orphans - curr_orphans

    # Count regressions: connected count must not decrease relative to baseline
    base_connected = baseline.get("connected", 0)
    curr_connected = current.get("connected", 0)
    connected_regression = curr_connected < base_connected

    base_warnings = set(baseline.get("dep_graph_warnings", []))
    curr_warnings = set(current.get("dep_graph_warnings", []))
    new_warnings = curr_warnings - base_warnings

    regressions: list[str] = []
    if new_orphans:
        regressions.append(f"new orphan services: {sorted(new_orphans)}")
    if connected_regression:
        regressions.append(
            f"connected count dropped: baseline={base_connected} "
            f"current={curr_connected}"
        )
    if new_warnings:
        regressions.append(f"new dependency-graph warnings: {sorted(new_warnings)}")

    if fixed_orphans:
        print(
            f"INFO: orphans fixed since baseline: {sorted(fixed_orphans)}",
            file=sys.stderr,
        )
        print(
            "      Refresh .wiring_baseline.json to lock in the improvement.",
            file=sys.stderr,
        )

    if regressions:
        print("FAIL: wiring regressions detected", file=sys.stderr)
        for r in regressions:
            print(f"  - {r}", file=sys.stderr)
        return 1

    print(
        f"OK: wiring stable - "
        f"{curr_connected} connected, "
        f"{len(curr_orphans)} orphan(s) (matches baseline)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
