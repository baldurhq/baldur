"""
Generate the third-party license inventory (THIRD_PARTY_LICENSES.md).

Resolves the dependency closure of an installed distribution (default
``baldur-framework[all]``) from local package metadata and renders a Markdown
inventory of every third-party dependency with its version and license.

The license string mirrors the ``license-check`` CI gate in
``.github/workflows/security.yml`` (license classifiers preferred, then
``License-Expression``, then the ``License`` field), so the inventory and the
gate never disagree about how a package is licensed. Weak copyleft (LGPL) is
called out separately with the redistribution note; any strong copyleft
(GPL / AGPL) is flagged prominently — there should be none, because the
license-check gate fails the build otherwise.

The closure is unioned across the Linux / Windows / macOS platform markers so
the inventory is independent of the host the generator runs on: a dependency
gated by ``sys_platform`` (e.g. ``pywin32``) is included regardless of where
the script runs. Scope is the resolved dependency closure only, so dev and
test tooling (pytest, ruff, mypy, locust, ...) is intentionally excluded.

Usage::

    pip install -e ".[all]"
    python scripts/generate_notices.py
    # write to stdout:
    python scripts/generate_notices.py --output -

Exit codes::

    0 - inventory written
    2 - usage error (root distribution not installed, bad output path, etc.)
"""

from __future__ import annotations

import argparse
import re
import sys
from importlib import metadata
from pathlib import Path

from packaging.markers import Marker
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT = PROJECT_ROOT / "THIRD_PARTY_LICENSES.md"

# Distributions that are the project itself, never third-party.
SELF_DISTRIBUTIONS = {
    "baldur-framework",
    "baldur-pro",
    "baldur-dormant",
    "baldur",
    "selfhealing",  # legacy package name, may linger in stale environments
}

# Platform marker environments the closure is unioned over, so the inventory
# is independent of the host the generator runs on.
_PLATFORMS = (
    {"sys_platform": "linux", "platform_system": "Linux", "os_name": "posix"},
    {"sys_platform": "win32", "platform_system": "Windows", "os_name": "nt"},
    {"sys_platform": "darwin", "platform_system": "Darwin", "os_name": "posix"},
)

# Strong copyleft is genuinely incompatible with Apache-2.0 redistribution;
# weak/library copyleft (LGPL) is not, when consumed as a separately installed
# dependency. The exclusion of LGPL/Lesser mirrors the license-check gate.
_STRONG_COPYLEFT = re.compile(r"GPL|AGPL", re.IGNORECASE)
_WEAK_COPYLEFT = re.compile(r"LGPL|Lesser", re.IGNORECASE)


def _marker_holds(marker: Marker | None, extra: str) -> bool:
    """True if ``marker`` evaluates true for ``extra`` on any supported OS."""
    if marker is None:
        # A requirement with no marker is an unconditional base dependency;
        # count it once, under the empty-extra context.
        return extra == ""
    for platform_env in _PLATFORMS:
        try:
            if marker.evaluate({**platform_env, "extra": extra}):
                return True
        except Exception:
            continue
    return False


def _installed_index() -> dict[str, metadata.Distribution]:
    """Map canonical distribution name -> Distribution for the active env."""
    index: dict[str, metadata.Distribution] = {}
    for dist in metadata.distributions():
        name = dist.metadata["Name"]
        if name:
            index[canonicalize_name(name)] = dist
    return index


def resolve_closure(
    root: str, extras: set[str], index: dict[str, metadata.Distribution]
) -> set[str]:
    """Return canonical names in the dependency closure of ``root[extras]``."""
    seen: set[tuple[str, frozenset[str]]] = set()
    found: set[str] = set()
    stack: list[tuple[str, frozenset[str]]] = [
        (canonicalize_name(root), frozenset(extras))
    ]
    while stack:
        name, active = stack.pop()
        if (name, active) in seen:
            continue
        seen.add((name, active))
        dist = index.get(name)
        if dist is None:
            continue
        found.add(name)
        for raw in dist.requires or []:
            req = Requirement(raw)
            included = _marker_holds(req.marker, "")
            if not included:
                included = any(_marker_holds(req.marker, e) for e in active)
            if included:
                stack.append((canonicalize_name(req.name), frozenset(req.extras)))
    return found


def license_of(dist: metadata.Distribution) -> str:
    """License string matching the license-check gate's resolution order."""
    md = dist.metadata
    classifiers = [
        c.split("::")[-1].strip()
        for c in md.get_all("Classifier", [])
        if c.startswith("License ::")
    ]
    if classifiers:
        return "; ".join(classifiers)
    expression = md.get("License-Expression")
    if expression:
        return expression.strip()
    declared = md.get("License")
    if declared and declared.strip() and "\n" not in declared.strip():
        return declared.strip()
    return "UNKNOWN"


def homepage_of(dist: metadata.Distribution) -> str:
    """Best-effort project URL for attribution / source availability."""
    md = dist.metadata
    home = md.get("Home-page")
    if home and home.strip():
        return home.strip()
    fallback = ""
    for entry in md.get_all("Project-URL", []):
        label, _, url = entry.partition(",")
        label, url = label.strip().lower(), url.strip()
        if label in ("homepage", "home"):
            return url
        if not fallback:
            fallback = url
    return fallback


def _escape(text: str) -> str:
    """Escape Markdown table-breaking characters."""
    return text.replace("|", "\\|").strip()


def render(package: str, extras: set[str], rows: list[dict[str, str]]) -> str:
    strong = [r for r in rows if r["category"] == "strong"]
    weak = [r for r in rows if r["category"] == "weak"]
    extras_label = ",".join(sorted(extras)) or "(none)"

    lines: list[str] = []
    lines.append("# Third-Party Licenses")
    lines.append("")
    lines.append(
        f"`{package}` (Apache-2.0) depends on the open-source packages listed "
        "below. They are installed separately from PyPI — not vendored into the "
        "baldur wheel — so this inventory is provided for attribution and "
        "license-compliance transparency, not as a redistribution of their code."
    )
    lines.append("")
    lines.append(
        f"Scope: the resolved `{package}[{extras_label}]` dependency closure "
        "(development and test tooling is excluded). Versions reflect the "
        "environment the inventory was generated in; regenerate at release time."
    )
    lines.append("")
    lines.append(
        "<!-- GENERATED by scripts/generate_notices.py — do not edit by hand. -->"
    )
    lines.append("")
    lines.append("Regenerate with:")
    lines.append("")
    lines.append("```")
    lines.append('pip install -e ".[all]"')
    lines.append("python scripts/generate_notices.py")
    lines.append("```")
    lines.append("")

    lines.append("## Copyleft dependencies")
    lines.append("")
    if strong:
        lines.append(
            "> **WARNING — strong copyleft detected.** The following packages "
            "use GPL / AGPL, which is incompatible with Apache-2.0 "
            "redistribution. This must be resolved (the license-check CI gate "
            "fails on these):"
        )
        lines.append("")
        lines.append("| Package | Version | License | Source |")
        lines.append("|---------|---------|---------|--------|")
        for r in strong:
            lines.append(
                f"| {r['name']} | {r['version']} | {r['license']} | {r['homepage']} |"
            )
        lines.append("")
    else:
        lines.append(
            "No strong copyleft (GPL / AGPL) licenses are present in the "
            "dependency tree."
        )
        lines.append("")
    if weak:
        lines.append(
            "The following use weak/library copyleft (LGPL). baldur consumes "
            "them as separately pip-installed packages (dynamic linking), which "
            "imposes no source-disclosure obligation on baldur's own code. If "
            "you redistribute a *bundled* artifact (e.g. a Docker image or "
            "single-file build) that embeds these binaries, include their "
            "license text and note source availability:"
        )
        lines.append("")
        lines.append("| Package | Version | License | Source |")
        lines.append("|---------|---------|---------|--------|")
        for r in weak:
            lines.append(
                f"| {r['name']} | {r['version']} | {r['license']} | {r['homepage']} |"
            )
        lines.append("")

    lines.append(f"## Full inventory ({len(rows)} packages)")
    lines.append("")
    lines.append("| Package | Version | License | Homepage |")
    lines.append("|---------|---------|---------|----------|")
    for r in rows:
        lines.append(
            f"| {r['name']} | {r['version']} | {r['license']} | {r['homepage']} |"
        )
    lines.append("")
    return "\n".join(lines)


def build_rows(
    names: set[str], index: dict[str, metadata.Distribution]
) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for name in names:
        if name in SELF_DISTRIBUTIONS:
            continue
        dist = index.get(name)
        if dist is None:
            continue
        license_str = license_of(dist)
        if _STRONG_COPYLEFT.search(license_str) and not _WEAK_COPYLEFT.search(
            license_str
        ):
            category = "strong"
        elif _WEAK_COPYLEFT.search(license_str):
            category = "weak"
        else:
            category = "permissive"
        rows.append(
            {
                "name": _escape(dist.metadata["Name"]),
                "version": _escape(dist.version),
                "license": _escape(license_str),
                "homepage": _escape(homepage_of(dist)),
                "category": category,
            }
        )
    rows.sort(key=lambda r: r["name"].lower())
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument(
        "--package",
        default="baldur-framework",
        help="root distribution to resolve (default: baldur-framework)",
    )
    parser.add_argument(
        "--extras",
        default="all",
        help="comma-separated extras to include (default: all)",
    )
    parser.add_argument(
        "--output",
        default=str(DEFAULT_OUTPUT),
        help="output path, or '-' for stdout "
        f"(default: {DEFAULT_OUTPUT.relative_to(PROJECT_ROOT)})",
    )
    args = parser.parse_args(argv)

    extras = {e.strip() for e in args.extras.split(",") if e.strip()}
    index = _installed_index()
    if canonicalize_name(args.package) not in index:
        print(
            f"error: '{args.package}' is not installed. Run "
            f'`pip install -e ".[{args.extras}]"` first.',
            file=sys.stderr,
        )
        return 2

    names = resolve_closure(args.package, extras, index)
    rows = build_rows(names, index)
    content = render(args.package, extras, rows) + "\n"

    if args.output == "-":
        sys.stdout.write(content)
        return 0
    out_path = Path(args.output)
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(content, encoding="utf-8")
    except OSError as exc:
        print(f"error: cannot write {out_path}: {exc}", file=sys.stderr)
        return 2
    strong = sum(1 for r in rows if r["category"] == "strong")
    weak = sum(1 for r in rows if r["category"] == "weak")
    print(
        f"Wrote {len(rows)} packages to {out_path} "
        f"({strong} strong copyleft, {weak} weak copyleft)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
