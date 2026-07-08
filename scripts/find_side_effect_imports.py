"""
Detect imports that look unused but trigger module-level side effects.

These are the imports that ruff's F401 rule (or an IDE quick-fix) is
likely to strip the next time it runs. Each one is a candidate for a
``# noqa: F401`` defensive marker with a one-line reason.

Workflow
--------
1. Phase 1 — classify every Python module under ``src/baldur*`` as either
   ``side_effect`` or ``inert`` based on import-time AST shape:
     * module-level call to a registration-like function (``register_*``,
       ``subscribe``, ``add_task``, ``connect``, ``initialise_*``, ...)
     * module-level decorator usage on a top-level function/class with a
       known registration shape (``@shared_task``, ``@app.task``,
       ``@receiver``, ``@register_*``, ``@event_handler``)
     * module defines ``__init_subclass__`` (subclass auto-registration)
     * module-level explicit ``EventBus.subscribe(...)`` style calls
2. Phase 2 — for every ``import``/``from`` statement in src/, check
   whether the imported name is referenced anywhere else in the file
   body. If not — and the imported module is "side_effect carrying" —
   the import is a ``sitting duck`` candidate.
3. Phase 3 — emit a list grouped by importer, with the side-effect type
   detected on the imported module. Output is suitable for grep-driven
   triage and review.

Usage
-----
    python scripts/find_side_effect_imports.py [src/baldur]
        [--json] [--count]

Exit code is 1 when any sitting-duck imports are detected, 0 otherwise.
This makes the script CI-friendly once the project agrees on the
expected zero-state (after marking everything ``# noqa: F401`` with
reasons or rewriting the side-effect into an explicit setup function).
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from collections import defaultdict
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Function names that imply a registration side-effect when called at
# module level.
_REGISTRATION_CALL_NAMES = {
    "register",
    "register_signal_handlers",
    "register_provider",
    "register_service",
    "register_default",
    "register_default_recorders",
    "register_statistical_defaults",
    "subscribe",
    "add_task",
    "add_periodic_task",
    "connect",
    "wire",
    "install",
    "configure",
}

# Decorator names that bind a top-level function/class into an external
# registry (Celery / Django signal / event bus / DRF / pytest plugin).
_REGISTRATION_DECORATOR_NAMES = {
    "shared_task",
    "task",
    "periodic_task",
    "receiver",
    "register",
    "register_provider",
    "register_service",
    "event_handler",
    "subscribe",
    "hookimpl",
    "fixture",  # pytest plugin discovery via decorator
}


def _is_dunder_init(path: Path) -> bool:
    return path.name == "__init__.py"


# ---------------------------------------------------------------------------
# Phase 1 — module side-effect classifier
# ---------------------------------------------------------------------------


def _decorator_name(dec: ast.expr) -> str | None:
    """Return the rightmost dotted name of a decorator expression."""
    if isinstance(dec, ast.Call):
        return _decorator_name(dec.func)
    if isinstance(dec, ast.Attribute):
        return dec.attr
    if isinstance(dec, ast.Name):
        return dec.id
    return None


def _call_func_name(call: ast.Call) -> str | None:
    func = call.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return None


def classify_module(tree: ast.Module) -> set[str]:
    """Return a set of side-effect tags detected at module level."""
    tags: set[str] = set()

    for node in tree.body:
        # Module-level call — `register_*()`, `bus.subscribe(...)`
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
            name = _call_func_name(node.value)
            if name and (
                name in _REGISTRATION_CALL_NAMES or name.startswith("register_")
            ):
                tags.add(f"call:{name}")

        # Module-level decorator on function/class
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            for dec in node.decorator_list:
                dname = _decorator_name(dec)
                if dname and (
                    dname in _REGISTRATION_DECORATOR_NAMES
                    or dname.startswith("register_")
                ):
                    tags.add(f"decorator:{dname}")

        # Class with __init_subclass__ — subclass auto-registration
        if isinstance(node, ast.ClassDef):
            for child in node.body:
                if (
                    isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and child.name == "__init_subclass__"
                ):
                    tags.add("init_subclass")

    return tags


def scan_modules(roots: list[Path]) -> dict[str, set[str]]:
    """Return {dotted_module_name: side_effect_tags} for all py files."""
    result: dict[str, set[str]] = {}

    for root in roots:
        if not root.exists():
            continue
        # Find the importable name root — assume root == .../src/<pkg>
        package_root_name = root.name  # e.g. "baldur"
        for py in root.rglob("*.py"):
            try:
                source = py.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            try:
                tree = ast.parse(source, filename=str(py))
            except SyntaxError:
                continue

            tags = classify_module(tree)
            if not tags:
                continue

            # Build dotted module name relative to root parent
            rel = py.relative_to(root).with_suffix("")
            parts = [package_root_name, *rel.parts]
            if parts[-1] == "__init__":
                parts = parts[:-1]
            dotted = ".".join(parts)
            result[dotted] = tags

    return result


# ---------------------------------------------------------------------------
# Phase 2 — sitting duck import detector
# ---------------------------------------------------------------------------


class _NameUsageCollector(ast.NodeVisitor):
    """Collect every Name.id and Attribute.attr referenced after imports."""

    def __init__(self):
        self.names: set[str] = set()

    def visit_Name(self, node):  # noqa: N802
        self.names.add(node.id)

    def visit_Attribute(self, node):  # noqa: N802
        # Walk to root to capture the head of a dotted reference like `pkg.x`
        cur = node
        while isinstance(cur, ast.Attribute):
            cur = cur.value
        if isinstance(cur, ast.Name):
            self.names.add(cur.id)
        self.generic_visit(node)


def _file_has_noqa_for_import(line: str) -> bool:
    """Return True if the import line carries a noqa: F401 marker."""
    return "noqa" in line and "F401" in line


def find_sitting_ducks(
    file_path: Path,
    side_effect_modules: dict[str, set[str]],
) -> list[dict]:
    """Find imports that:

    1. Pull from a side_effect_carrying module.
    2. Have no name reference in the rest of the file body.
    3. Lack a ``# noqa: F401`` defensive marker on the import line.

    Returns a list of finding dicts.
    """
    try:
        source = file_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    try:
        tree = ast.parse(source, filename=str(file_path))
    except SyntaxError:
        return []

    source_lines = source.splitlines()

    # Collect every name reference in the file (excluding pure import statements).
    usage = _NameUsageCollector()
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        usage.visit(node)
    referenced_names = usage.names

    findings: list[dict] = []

    for node in ast.walk(tree):
        if not isinstance(node, (ast.Import, ast.ImportFrom)):
            continue

        line_idx = node.lineno - 1
        if 0 <= line_idx < len(source_lines):
            line_text = source_lines[line_idx]
            # Multiline imports: scan all spanned lines for noqa
            spans = (
                source_lines[line_idx : (node.end_lineno or node.lineno)]
                if hasattr(node, "end_lineno")
                else [line_text]
            )
            has_noqa = any(_file_has_noqa_for_import(ln) for ln in spans)
        else:
            has_noqa = False

        if has_noqa:
            continue

        if isinstance(node, ast.Import):
            # `import X.Y.Z [as alias]`
            for alias in node.names:
                imported_module = alias.name
                local_name = alias.asname or imported_module.split(".")[0]
                if local_name in referenced_names:
                    continue
                tags = _module_tags(imported_module, side_effect_modules)
                if not tags:
                    continue
                findings.append(
                    {
                        "lineno": node.lineno,
                        "kind": "import",
                        "module": imported_module,
                        "local_name": local_name,
                        "side_effect_tags": sorted(tags),
                    }
                )
            continue

        # ast.ImportFrom — `from X import Y [as alias], Z`
        from_module = node.module or ""
        for alias in node.names:
            local_name = alias.asname or alias.name
            if local_name in referenced_names:
                continue
            # The imported symbol may be a submodule re-export → resolve as
            # `<from_module>.<alias.name>` and as `<from_module>` itself.
            candidate_modules = [from_module]
            if from_module:
                candidate_modules.append(f"{from_module}.{alias.name}")

            tags: set[str] = set()
            for cand in candidate_modules:
                tags |= _module_tags(cand, side_effect_modules)
            if not tags:
                continue
            findings.append(
                {
                    "lineno": node.lineno,
                    "kind": "from",
                    "module": from_module,
                    "imported_name": alias.name,
                    "local_name": local_name,
                    "side_effect_tags": sorted(tags),
                }
            )

    return findings


def _module_tags(
    module_name: str, side_effect_modules: dict[str, set[str]]
) -> set[str]:
    """Return tags for `module_name` or any of its parents in the registry."""
    if not module_name:
        return set()
    if module_name in side_effect_modules:
        return side_effect_modules[module_name]
    # Walk up dotted path so `from X.Y.Z import a` matches a side-effect at
    # any ancestor.
    parts = module_name.split(".")
    for i in range(len(parts) - 1, 0, -1):
        ancestor = ".".join(parts[:i])
        if ancestor in side_effect_modules:
            return side_effect_modules[ancestor]
    return set()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Detect sitting-duck side-effect imports vulnerable to ruff F401"
    )
    parser.add_argument(
        "roots",
        nargs="*",
        default=["src/baldur"],
        help="Package source roots to scan",
    )
    parser.add_argument("--json", action="store_true", help="JSON output")
    parser.add_argument(
        "--count", action="store_true", help="Print only summary counts"
    )
    args = parser.parse_args()

    roots = [(PROJECT_ROOT / r).resolve() for r in args.roots]

    side_effect_modules = scan_modules(roots)

    by_file: dict[str, list[dict]] = defaultdict(list)
    total_findings = 0
    files_scanned = 0

    for root in roots:
        if not root.exists():
            continue
        for py in sorted(root.rglob("*.py")):
            files_scanned += 1
            # __init__.py imports are protected by pyproject.toml per-file-ignore
            # for F401 — re-exports survive ruff strip. Skip from importer side.
            if _is_dunder_init(py):
                continue
            findings = find_sitting_ducks(py, side_effect_modules)
            if findings:
                rel = str(py.relative_to(PROJECT_ROOT))
                by_file[rel].extend(findings)
                total_findings += len(findings)

    if args.json:
        out = {
            "files_scanned": files_scanned,
            "side_effect_modules": len(side_effect_modules),
            "files_with_findings": len(by_file),
            "total_findings": total_findings,
            "findings": dict(sorted(by_file.items())),
        }
        print(json.dumps(out, indent=2, ensure_ascii=False))
        return 1 if total_findings else 0

    if args.count:
        print(
            f"Scanned {files_scanned} files; "
            f"{len(side_effect_modules)} side-effect modules; "
            f"{total_findings} sitting-duck imports in {len(by_file)} files"
        )
        return 1 if total_findings else 0

    if not by_file:
        print("No sitting-duck imports detected.")
        return 0

    print(f"=== Sitting-duck imports ({total_findings} in {len(by_file)} files) ===")
    print(f"=== Side-effect modules registered: {len(side_effect_modules)} ===\n")
    for fpath, items in sorted(by_file.items()):
        print(fpath)
        for f in items:
            tags = ",".join(f["side_effect_tags"])
            if f["kind"] == "import":
                print(
                    f"  L{f['lineno']}: import {f['module']} "
                    f"as {f['local_name']}  ← {tags}"
                )
            else:
                print(
                    f"  L{f['lineno']}: from {f['module']} import "
                    f"{f['imported_name']}  ← {tags}"
                )
        print()

    return 1


if __name__ == "__main__":
    sys.exit(main())
