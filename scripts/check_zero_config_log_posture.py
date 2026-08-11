#!/usr/bin/env python3
"""Zero-config log posture harness — measure (and gate) a first-contact run.

A clean ``pip install baldur-framework`` with no environment variables, no
Redis and no optional extras must not greet its first user with WARNING,
ERROR or CRITICAL lines. This script executes that posture for real, in a
child interpreter, and counts what a user would actually see on the console.

Two paths are measured, because they emit through different machinery:

``decorator``
    The README quick example — ``@baldur.protected("name")`` with no
    ``baldur.init()`` — plus three succeeding and three raising calls, so
    the retry / DLQ / circuit-breaker recording paths are exercised too.

``init``
    ``baldur.init()`` on a zero-config non-production process, the shape
    every framework adapter reaches through its startup hook.

Clean-install simulation. The child blocks every optional-extra distribution
and the private tiers by assigning ``sys.modules[name] = None``, which is
faithful to both absence probes used in the tree: ``import pkg`` raises and
``importlib.util.find_spec(pkg)`` returns ``None``. A meta-path finder that
raises is NOT a valid substitute — it breaks ``find_spec``. The blocked list
is derived from the package metadata (the ``all`` extra names the runtime
optional integrations) minus the transitive closure of the core
dependencies, so a new extra is covered without editing this file.

Counting. Lines are matched in BOTH renderings. ``structured_json`` defaults
to True, so anything emitted after ``configure_structlog()`` is JSON, while
anything emitted before it is console-rendered by structlog's pre-configure
default. A single-form pattern counts one subset and silently passes.

Usage::

    python scripts/check_zero_config_log_posture.py              # measure
    python scripts/check_zero_config_log_posture.py --check      # gate
    python scripts/check_zero_config_log_posture.py --strict-log-validation
    python scripts/check_zero_config_log_posture.py --path init --show-lines
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

# --------------------------------------------------------------------------
# Rendering-agnostic level patterns. The JSON arm matches the JSONRenderer
# output; the bracket arm matches structlog's ConsoleRenderer, which is what
# import-time emissions use (no configuration installed yet).
# --------------------------------------------------------------------------
_LEVEL_PATTERNS: dict[str, re.Pattern[str]] = {
    "warning+": re.compile(
        r'"level"\s*:\s*"(warning|error|critical)"|\[(warning|error|critical)\s*]',
        re.IGNORECASE,
    ),
    "info": re.compile(r'"level"\s*:\s*"info"|\[info\s*]', re.IGNORECASE),
    "debug": re.compile(r'"level"\s*:\s*"debug"|\[debug\s*]', re.IGNORECASE),
}

# The posture announcement (D7) — exactly one line per process is the contract.
POSTURE_EVENT = "baldur.runtime_posture"

# Whose line is it? After configuration every record carries its logger name,
# and baldur owns the root handler, so a host framework's own WARNING would be
# rendered in the same JSON. Lines emitted before configuration have no logger
# field at all — at that point only baldur has imported anything.
_BALDUR_LOGGER = re.compile(r'"logger"\s*:\s*"baldur', re.IGNORECASE)
_ANY_LOGGER = re.compile(r'"logger"\s*:', re.IGNORECASE)

_CHILD_TIMEOUT_SECONDS = 300

PATHS = ("decorator", "init")


# ==========================================================================
# Child side — runs inside the scrubbed interpreter
# ==========================================================================


def _core_distribution_closure() -> set[str]:
    """Distribution names reachable from the core (extra-free) dependencies.

    Blocking one of these would simulate a broken install rather than a bare
    one — ``typer`` pulls ``click``, ``requests`` pulls ``urllib3``, and an
    optional extra may name the same transitive package.
    """
    from importlib.metadata import PackageNotFoundError, distribution

    closure: set[str] = set()
    pending = [_normalize_dist("baldur-framework")]
    while pending:
        name = pending.pop()
        if name in closure:
            continue
        closure.add(name)
        try:
            requires = distribution(name).requires or []
        except PackageNotFoundError:
            continue
        for requirement in requires:
            if _requirement_extra(requirement) is not None:
                continue
            dependency = _normalize_dist(_requirement_name(requirement))
            if dependency not in closure:
                pending.append(dependency)
    return closure


RUNTIME_OPTIONAL_EXTRA = "all"


def _optional_extra_distributions() -> set[str]:
    """Distributions contributed only by the runtime optional extras.

    Derivation seam: the ``all`` extra is the union of the runtime
    integration extras (``docs`` / ``dev`` / ``test-e2e`` are deliberately
    outside it), and packaging flattens its recursive self-reference into
    concrete distributions in the installed metadata. So a new integration
    extra joins this set by joining ``all`` — nothing here is hand-listed.
    """
    from importlib.metadata import distribution

    requires = distribution("baldur-framework").requires or []
    blocked = {
        _normalize_dist(_requirement_name(requirement))
        for requirement in requires
        if _requirement_extra(requirement) == RUNTIME_OPTIONAL_EXTRA
    }
    return blocked - _core_distribution_closure()


def _requirement_name(requirement: str) -> str:
    return re.split(r"[\s\[(<>=!;~]", requirement.strip(), maxsplit=1)[0]


def _requirement_extra(requirement: str) -> str | None:
    match = re.search(r"extra\s*==\s*[\"']([^\"']+)[\"']", requirement)
    return match.group(1) if match else None


def _normalize_dist(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def blocked_module_names() -> list[str]:
    """Top-level module names that a bare clean install cannot import."""
    from importlib.metadata import packages_distributions

    optional_dists = _optional_extra_distributions()
    modules: set[str] = {"baldur_pro", "baldur_dormant"}
    for module, dists in packages_distributions().items():
        # Some wheels advertise build-artifact top-level names (``__init__``,
        # ``cpython``); only real importable package names are blockable.
        if module.startswith("__") or not module.isidentifier():
            continue
        if any(_normalize_dist(dist) in optional_dists for dist in dists):
            modules.add(module)
    # An extra whose distribution is not installed here needs no blocking, but
    # the well-known module names are added unconditionally so the harness
    # measures the same posture on a host that happens to have them vendored.
    return sorted(modules)


def _install_absence(modules: list[str]) -> None:
    for name in modules:
        sys.modules[name] = None  # type: ignore[assignment]
    _hide_entry_points(set(modules))


def _hide_entry_points(modules: set[str]) -> None:
    """Drop entry points whose backing module is one of the blocked ones.

    Blocking ``sys.modules`` alone hides the code but leaves the
    distribution *metadata* in place, so entry-point consumers still find a
    hook they cannot load — a state no clean install has. Without this the
    private tier's ``baldur.bootstrap_hooks`` entry contributes a WARNING
    that only the simulation can produce.
    """
    import importlib.metadata as metadata

    discover = metadata.entry_points

    def entry_points(**params: object) -> object:
        found = discover(**params)  # type: ignore[arg-type]
        kept = tuple(
            entry
            for entry in found
            if entry.value.split(":", 1)[0].split(".", 1)[0] not in modules
        )
        return metadata.EntryPoints(kept)

    metadata.entry_points = entry_points  # type: ignore[assignment]


def _run_decorator_path() -> None:
    """README quick example + three succeeding and three raising calls."""
    import baldur

    @baldur.protected("charge-customer")
    def charge(order_id: str) -> dict:
        return {"status": "ok", "order_id": order_id}

    @baldur.protected("charge-flaky", retry=True, dlq=True)
    def charge_flaky(order_id: str) -> dict:
        raise RuntimeError("payment gateway unreachable")

    for index in range(3):
        charge(f"order-{index}")
    for index in range(3):
        try:
            charge_flaky(f"order-fail-{index}")
        except Exception:  # noqa: BLE001 — the harness measures logs, not results
            pass


def _run_init_path() -> None:
    import baldur

    baldur.init()


def _child_main(path: str) -> int:
    _install_absence(blocked_module_names())
    if path == "decorator":
        _run_decorator_path()
    else:
        _run_init_path()
    return 0


# ==========================================================================
# Parent side — spawns the child, counts, reports
# ==========================================================================


@dataclass
class PathResult:
    path: str
    returncode: int
    counts: dict[str, int]
    posture_lines: int
    lines: list[str] = field(default_factory=list)

    @property
    def warning_plus(self) -> int:
        return self.counts["warning+"]


def baldur_lines_at_warning_or_above(lines: list[str]) -> list[str]:
    """Select the WARNING-or-above lines that baldur itself emitted."""
    return [
        line
        for line in lines
        if _LEVEL_PATTERNS["warning+"].search(line)
        and (_BALDUR_LOGGER.search(line) or not _ANY_LOGGER.search(line))
    ]


def _scan_log_file(path: Path) -> int:
    """Gate an existing log file — a real framework boot captured by CI.

    Shares the level patterns with the measured runs above, so "a baldur
    WARNING-or-above line" has one definition rather than one per caller.
    """
    if not path.exists():
        print(f"ERROR: no such log file: {path}", file=sys.stderr)
        return 1
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    offending = baldur_lines_at_warning_or_above(lines)
    if offending:
        print(
            f"FAIL: {path} holds {len(offending)} baldur WARNING-or-above "
            "line(s) from a zero-config boot",
            file=sys.stderr,
        )
        for line in offending:
            print(line, file=sys.stderr)
        return 1
    print(f"OK: {path} holds no baldur WARNING-or-above line")
    return 0


def _child_environment(strict_log_validation: bool) -> dict[str, str]:
    """A zero-config environment: every ``BALDUR_*`` variable removed."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("BALDUR_")}
    env.pop("REDIS_URL", None)
    env.pop("DJANGO_SETTINGS_MODULE", None)
    env["PYTHONIOENCODING"] = "utf-8"
    if strict_log_validation:
        env["BALDUR_LOGGING_SETTINGS_STRICT_LOG_VALIDATION"] = "true"
    return env


def _measure(path: str, strict_log_validation: bool) -> PathResult:
    completed = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "--_child", path],
        capture_output=True,
        env=_child_environment(strict_log_validation),
        timeout=_CHILD_TIMEOUT_SECONDS,
        check=False,
    )
    # Decode explicitly: text=True picks the console codepage on Windows and
    # can silently yield an empty stream on a decode failure.
    output = (completed.stdout + completed.stderr).decode("utf-8", errors="replace")
    lines = [line for line in output.splitlines() if line.strip()]
    counts = {
        level: sum(1 for line in lines if pattern.search(line))
        for level, pattern in _LEVEL_PATTERNS.items()
    }
    return PathResult(
        path=path,
        returncode=completed.returncode,
        counts=counts,
        posture_lines=sum(1 for line in lines if POSTURE_EVENT in line),
        lines=lines,
    )


def _report(results: list[PathResult], show_lines: bool) -> None:
    print(
        f"{'path':<12} {'WARNING+':>9} {'INFO':>6} {'DEBUG':>6} {'posture':>8} {'exit':>5}"
    )
    for result in results:
        print(
            f"{result.path:<12} {result.warning_plus:>9} {result.counts['info']:>6} "
            f"{result.counts['debug']:>6} {result.posture_lines:>8} {result.returncode:>5}"
        )
    if show_lines:
        for result in results:
            _print_offending_lines(result)


def _print_offending_lines(result: PathResult) -> None:
    offending = [
        line for line in result.lines if _LEVEL_PATTERNS["warning+"].search(line)
    ]
    if offending:
        print(f"\n--- {result.path}: WARNING-or-above lines ---")
        for line in offending:
            print(line)


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--_child", choices=PATHS, help=argparse.SUPPRESS)
    parser.add_argument(
        "--path",
        choices=(*PATHS, "both"),
        default="both",
        help="which zero-config path to measure (default: both)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit non-zero when any WARNING-or-above line is emitted",
    )
    parser.add_argument(
        "--strict-log-validation",
        action="store_true",
        help="run with BALDUR_STRICT_LOG_VALIDATION=true so event-name "
        "convention violations raise instead of being counted",
    )
    parser.add_argument(
        "--show-lines",
        action="store_true",
        help="print every WARNING-or-above line that was counted",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit the measurement as JSON instead of a table",
    )
    parser.add_argument(
        "--scan-log",
        metavar="PATH",
        help="instead of measuring, gate an existing log file (a real "
        "framework boot captured by CI) for baldur WARNING-or-above lines",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    if args._child:
        return _child_main(args._child)

    if args.scan_log:
        return _scan_log_file(Path(args.scan_log))

    paths = PATHS if args.path == "both" else (args.path,)
    results = [_measure(path, args.strict_log_validation) for path in paths]

    if args.json:
        print(
            json.dumps(
                [
                    {
                        "path": r.path,
                        "returncode": r.returncode,
                        "counts": r.counts,
                        "posture_lines": r.posture_lines,
                    }
                    for r in results
                ],
                indent=2,
            )
        )
    else:
        _report(results, args.show_lines)

    failed = [r for r in results if r.returncode != 0]
    if failed:
        for result in failed:
            print(
                f"ERROR: the {result.path} path exited {result.returncode}",
                file=sys.stderr,
            )
            for line in result.lines[-40:]:
                print(line, file=sys.stderr)
        return 1

    if args.check:
        noisy = [r for r in results if r.warning_plus]
        if noisy:
            for result in noisy:
                print(
                    f"FAIL: the {result.path} path emitted {result.warning_plus} "
                    "WARNING-or-above line(s) on a zero-config run",
                    file=sys.stderr,
                )
                if not args.show_lines:
                    _print_offending_lines(result)
            return 1
        print("OK: zero WARNING-or-above lines on every measured path")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
