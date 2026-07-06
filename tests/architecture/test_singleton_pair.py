"""G8 — Singleton `get_*()` / `reset_*()` pair completeness.

Two layered checks (conservative — minimises false positives):

1. **`get_*_settings` / `reset_*_settings` pair**: Every module-level
   ``def get_<stem>_settings(...)`` MUST have a matching
   ``def reset_<stem>_settings(...)`` in the same module, and vice versa.
   This is the dominant documented pattern (~140 occurrences in
   ``src/baldur/settings/``).

2. **Reset without getter**: Any module-level ``def reset_<stem>(...)`` MUST
   have a matching ``def get_<stem>(...)``. Reset without a getter is
   unambiguously broken — there is nothing for the test to reset to.

Both halves of the contract land in this single test so a violation message
points to the missing companion regardless of which side was authored first.

Rule registry: ``ARCHITECTURE.md#g8-singleton-pair``
"""

from __future__ import annotations

import ast
from pathlib import Path

from tests.architecture.conftest import (
    DEFAULT_SRC_ROOTS,
    collect_violations,
    parse_ast,
    walk_src,
)

_RULE_KEY = "singleton_pair"
_RULE_ANCHOR = "#g8-singleton-pair"


def _module_level_functions(tree: ast.Module) -> dict[str, int]:
    """Return ``{function_name: lineno}`` for module-level defs only."""
    found: dict[str, int] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            found[node.name] = node.lineno
    return found


def _scan(path: Path) -> list[tuple[Path, int, str, str]]:
    tree = parse_ast(path)
    if tree is None:
        return []
    funcs = _module_level_functions(tree)
    violations: list[tuple[Path, int, str, str]] = []

    get_settings_stems: dict[str, int] = {}
    reset_settings_stems: dict[str, int] = {}
    reset_any_stems: dict[str, int] = {}
    get_any_stems: dict[str, int] = {}

    for name, lineno in funcs.items():
        if name.startswith("get_") and name.endswith("_settings"):
            stem = name[len("get_") : -len("_settings")]
            if stem:
                get_settings_stems[stem] = lineno
        if name.startswith("reset_") and name.endswith("_settings"):
            stem = name[len("reset_") : -len("_settings")]
            if stem:
                reset_settings_stems[stem] = lineno
        if name.startswith("reset_"):
            stem = name[len("reset_") :]
            if stem:
                reset_any_stems[stem] = lineno
        if name.startswith("get_"):
            stem = name[len("get_") :]
            if stem:
                get_any_stems[stem] = lineno

    # The offending def is always module-level, so its name equals its
    # qualname — emit it directly as the drift-stable symbol key (D5).
    for stem, lineno in get_settings_stems.items():
        if stem not in reset_settings_stems:
            name = f"get_{stem}_settings"
            violations.append(
                (path, lineno, name, f"{name} without reset_{stem}_settings")
            )
    for stem, lineno in reset_settings_stems.items():
        if stem not in get_settings_stems:
            name = f"reset_{stem}_settings"
            violations.append(
                (path, lineno, name, f"{name} without get_{stem}_settings")
            )

    for stem, lineno in reset_any_stems.items():
        if stem.endswith("_settings"):
            continue
        if stem not in get_any_stems:
            name = f"reset_{stem}"
            violations.append((path, lineno, name, f"{name} without get_{stem}"))

    return violations


class TestSingletonPairContract:
    """G8 — singleton modules MUST ship both halves of the get/reset pair."""

    def test_no_unbaselined_violations(self):
        raw: list[tuple[Path, int | None, str | None, str | None]] = []
        for path in walk_src(DEFAULT_SRC_ROOTS):
            for offender_path, line, symbol, extra in _scan(path):
                raw.append((offender_path, line, symbol, extra))

        violations = collect_violations(_RULE_KEY, raw, _RULE_ANCHOR)
        assert not violations, (
            f"G8: singleton pair regressions ({len(violations)}). "
            "Add the missing companion (`get_<stem>` or `reset_<stem>`) or add "
            "a baseline entry under `singleton_pair:` with reason+ticket.\n"
            + "\n".join(violations)
        )
