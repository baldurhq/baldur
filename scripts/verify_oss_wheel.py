#!/usr/bin/env python3
"""Publish-time guard: no relocated private-tier path in any public artifact.

Impl doc 599 D13. The tag-triggered publish path (``build-oss.yml``) runs no
tests and has no ``needs`` dependency on ``ci.yml``, so a tag cut from a red
commit — or a ``pyproject.toml`` build-config drift (e.g. ``force-include``,
sdist ``include``) invisible to G39's src-tree checks — would otherwise publish
silently. This script verifies the *built artifacts* (the exact ``dist/*``
upload set), complementing G39 (`tests/architecture/test_src_tier_placement.py`)
which verifies the src tree at PR time.

Checks per artifact:

* **Wheel** (``*.whl``):
  - none of the ``RELOCATED_PATH_PREFIXES`` appears in the namelist;
  - no private top-level package (``baldur_pro/``, ``baldur_dormant/``);
  - anti-vacuous positives: ``baldur/__init__.py`` and the package-native
    ``baldur/_data/V1_LAUNCH_MANIFEST.yaml`` are present.
* **Sdist** (``*.tar.gz``):
  - none of the ``RELOCATED_PATH_PREFIXES`` appears under ``<dist>/src/``;
  - no private top-level source tree (``src/baldur_pro``, ``src/baldur_dormant``)
    — the pre-599 sdist ``include = ["/src"]`` shipped both wholesale. The v1.0
    launch manifest is now package-native (``src/baldur/_data/``), so it rides
    the ``/src/baldur`` include in both the sdist and the wheel — no separate
    required-source assertion is needed.

Every ``*.whl`` / ``*.tar.gz`` in the target directory is verified: the OSS
publish lane uploads ``dist/*`` wholesale, so a stray private artifact in that
directory IS a publish leak, not a false positive. Run it on the exact
directory that will be uploaded.

Usage::

    python scripts/verify_oss_wheel.py [dist_dir]

Exit codes: 0 = all artifacts clean; 1 = violations found; 2 = no artifacts
to verify (blocking — an empty dist directory means the guard ran against
the wrong path, never a pass).

Shared single source of truth: ``RELOCATED_PATH_PREFIXES`` is imported by the
G39 gate so the PR-time src-tree check and this publish-time artifact check
can never drift apart.
"""

from __future__ import annotations

import sys
import tarfile
import zipfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# The ten relocated path prefixes (impl doc 599 D2/D4/D5, SC1's pinned list).
# Package-root-relative, no trailing slash. A prefix matches a directory
# (``<prefix>/...``) or a module (``<prefix>.py``).
RELOCATED_PATH_PREFIXES: tuple[str, ...] = (
    "baldur/services/runbook",
    "baldur/services/circuit_mesh",
    "baldur/services/finops",
    "baldur/services/ml_models",
    "baldur/services/predictive_forecaster",
    "baldur/services/learning",
    "baldur/services/correlation_engine",
    "baldur/services/compliance",
    "baldur/multiregion",
    "baldur/coordination/redis_elector",
)

# Private distribution top-level packages — must never appear in a public
# artifact (528 D2/D4; same invariant as scripts/audit_wheel_contents.py).
PRIVATE_TOP_LEVEL: tuple[str, ...] = ("baldur_pro", "baldur_dormant")

# The v1.0 launch manifest is now package-native under ``src/baldur/_data/``,
# so it rides the ``/src/baldur`` sdist include and the wheel ``packages``
# entry — no force-include and therefore no separate sdist-source requirement.
_SDIST_REQUIRED: tuple[str, ...] = ()
_WHEEL_REQUIRED: tuple[str, ...] = (
    "baldur/__init__.py",
    "baldur/_data/V1_LAUNCH_MANIFEST.yaml",
)


def matches_relocated_prefix(pkg_rel_path: str) -> bool:
    """True when a package-relative posix path falls under a relocated prefix."""
    for prefix in RELOCATED_PATH_PREFIXES:
        if pkg_rel_path == f"{prefix}.py" or pkg_rel_path.startswith(f"{prefix}/"):
            return True
    return False


def verify_wheel_names(names: list[str]) -> list[str]:
    """Violations for a wheel namelist (entries are package-root-relative)."""
    violations: list[str] = []
    for name in names:
        if matches_relocated_prefix(name):
            violations.append(f"relocated path in wheel: {name}")
        top = name.split("/", 1)[0]
        if top in PRIVATE_TOP_LEVEL:
            violations.append(f"private top-level package in wheel: {name}")
    for required in _WHEEL_REQUIRED:
        if required not in names:
            violations.append(f"missing required wheel entry: {required}")
    return violations


def verify_sdist_names(names: list[str]) -> list[str]:
    """Violations for an sdist namelist (entries are ``<dist>-<ver>/...``)."""
    violations: list[str] = []
    required_seen = dict.fromkeys(_SDIST_REQUIRED, False)
    for name in names:
        # Strip the `<dist>-<version>/` leading component (PEP 517 sdist layout).
        _, _, inner = name.partition("/")
        if not inner:
            continue
        for required in _SDIST_REQUIRED:
            if inner == required:
                required_seen[required] = True
        if not inner.startswith("src/"):
            continue
        pkg_rel = inner[len("src/") :]
        if matches_relocated_prefix(pkg_rel):
            violations.append(f"relocated path in sdist: {name}")
        top = pkg_rel.split("/", 1)[0]
        if top in PRIVATE_TOP_LEVEL:
            violations.append(f"private source tree in sdist: {name}")
    for required, seen in required_seen.items():
        if not seen:
            violations.append(
                # Keep printed strings ASCII-only: a cp949/legacy Windows
                # console cannot encode typographic dashes and would crash
                # the guard mid-report.
                f"missing required sdist entry: {required} "
                "(wheel force-include source - wheel-from-sdist builds break without it)"
            )
    return violations


def verify_artifact(path: Path) -> list[str]:
    """Dispatch on artifact type and return its violations."""
    if path.suffix == ".whl":
        with zipfile.ZipFile(path) as zf:
            return verify_wheel_names(zf.namelist())
    with tarfile.open(path) as tf:
        return verify_sdist_names(tf.getnames())


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    dist_dir = Path(args[0]) if args else PROJECT_ROOT / "dist"
    if not dist_dir.is_dir():
        print(f"ERROR: dist directory not found: {dist_dir}", file=sys.stderr)
        return 2

    artifacts = sorted(dist_dir.glob("*.whl")) + sorted(dist_dir.glob("*.tar.gz"))
    if not artifacts:
        print(f"ERROR: no *.whl / *.tar.gz artifacts in {dist_dir}", file=sys.stderr)
        return 2

    failed = False
    for artifact in artifacts:
        violations = verify_artifact(artifact)
        verdict = "PASS" if not violations else "FAIL"
        print(f"[verify-oss] {artifact.name}: {verdict}")
        for violation in violations:
            print(f"[verify-oss]   - {violation}")
        failed = failed or bool(violations)

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
