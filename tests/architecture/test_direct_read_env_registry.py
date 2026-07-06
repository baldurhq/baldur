"""G33 — direct-read ``BALDUR_*`` registry MUST equal the discovered read set.

Impl doc 576. The runtime unknown-env-var scan
(``bootstrap._warn_unknown_env_vars``) treats a ``BALDUR_*`` key as *known* if it
either resolves to a Pydantic settings field OR is catalogued in
``baldur.settings.introspection.KNOWN_DIRECT_READ_ENV_VARS`` — the OSS vars read
straight from ``os.environ`` via a string literal, with no backing Pydantic
field. If that constant drifts from the real source reads, the scan either
false-positives (a real direct-read var warns as unknown) or under-covers (a
removed read lingers in the constant). This gate keeps the two enforced-equal.

**Detection.** AST-scan of ``src/baldur`` for the three literal-key read shapes:

* ``os.environ.get("BALDUR_…")`` / ``os.environ.get("BALDUR_…", default)``,
* ``os.getenv("BALDUR_…")``,
* ``os.environ["BALDUR_…"]`` **reads** (``ast.Load`` only — an
  ``os.environ[...] =`` *write*, e.g. ``cli/_config.apply_config_to_env``, is an
  ``ast.Store`` subscript and correctly excluded).

Computed-name reads (``f"BALDUR_{x}"``, e.g. ``DegradedModeHandler.get``) are
invisible to a literal scan by construction (the AST key is a ``JoinedStr`` /
``Name``, not a ``Constant``), so they are neither discovered nor required here —
they are covered via the ``register_direct_read_env_vars`` Channel-2 seam.

**Scope: ``src/baldur`` only.** The ``src/baldur_pro`` drift-guard + pro registry
constant is pro-tier work (Out of Scope); the Channel-2 seam keeps pro from
regressing until then.

**Baseline granularity** — ENFORCED-EMPTY (``direct_read_env_registry: []``). A
drift is FIXED by pasting the printed add/remove diff into
``KNOWN_DIRECT_READ_ENV_VARS``, never baselined.

Rule registry:
``ARCHITECTURE.md#g33-direct-read-env-registry``
"""

from __future__ import annotations

import ast
from collections import defaultdict
from pathlib import Path

from baldur.settings.introspection import KNOWN_DIRECT_READ_ENV_VARS
from tests.architecture.conftest import (
    PROJECT_ROOT,
    collect_violations,
    parse_ast,
    symbol_of,
    walk_src,
)

_RULE_KEY = "direct_read_env_registry"
_RULE_ANCHOR = "#g33-direct-read-env-registry"

_SRC_BALDUR = PROJECT_ROOT / "src" / "baldur"
_INTROSPECTION_PY = _SRC_BALDUR / "settings" / "introspection.py"

_BALDUR_PREFIX = "BALDUR_"


def _is_os_environ(node: ast.AST) -> bool:
    """True for an ``os.environ`` attribute access."""
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "environ"
        and isinstance(node.value, ast.Name)
        and node.value.id == "os"
    )


def _literal_baldur_arg(call: ast.Call) -> str | None:
    """Return the first positional arg of ``call`` iff a ``"BALDUR_*"`` string literal."""
    if not call.args:
        return None
    first = call.args[0]
    if (
        isinstance(first, ast.Constant)
        and isinstance(first.value, str)
        and first.value.startswith(_BALDUR_PREFIX)
    ):
        return first.value
    return None


def _iter_baldur_reads(tree: ast.Module) -> list[tuple[str, ast.AST]]:
    """Yield ``(var, node)`` for every literal ``BALDUR_*`` os.environ READ.

    Covers ``os.environ.get(...)``, ``os.getenv(...)``, and ``os.environ[...]``
    Load-subscripts. Store-subscripts (env writes) are excluded.
    """
    reads: list[tuple[str, ast.AST]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if (
                isinstance(func, ast.Attribute)
                and func.attr == "getenv"
                and isinstance(func.value, ast.Name)
                and func.value.id == "os"
            ) or (
                isinstance(func, ast.Attribute)
                and func.attr == "get"
                and _is_os_environ(func.value)
            ):
                var = _literal_baldur_arg(node)
                if var is not None:
                    reads.append((var, node))
        elif (
            isinstance(node, ast.Subscript)
            and _is_os_environ(node.value)
            and isinstance(node.ctx, ast.Load)
            and isinstance(node.slice, ast.Constant)
            and isinstance(node.slice.value, str)
            and node.slice.value.startswith(_BALDUR_PREFIX)
        ):
            reads.append((node.slice.value, node))
    return reads


def _discover_direct_reads() -> dict[str, list[tuple[Path, int | None, str]]]:
    """Map each discovered ``BALDUR_*`` read to its ``(file, line, symbol)`` sites."""
    locations: dict[str, list[tuple[Path, int | None, str]]] = defaultdict(list)
    for path in walk_src([_SRC_BALDUR]):
        tree = parse_ast(path)
        if tree is None:
            continue
        for var, node in _iter_baldur_reads(tree):
            locations[var].append(
                (path, getattr(node, "lineno", None), symbol_of(tree, node))
            )
    return locations


class TestDirectReadEnvRegistry:
    """G33 — ``KNOWN_DIRECT_READ_ENV_VARS`` stays in sync with real os.environ reads."""

    def test_registry_equals_discovered_reads(self):
        """The committed constant MUST equal the AST-discovered literal read set."""
        locations = _discover_direct_reads()
        discovered = set(locations)

        # Anti-vacuous guard: the bootstrap reads (BALDUR_TEST_MODE etc.) always
        # exist — an empty discovery means the scanner broke, not a clean tree.
        assert discovered, (
            "G33: scanner found no BALDUR_* os.environ reads — detection is broken"
        )

        raw: list[tuple[Path, int | None, str | None, str | None]] = []
        # Missing: read in source but absent from the constant → point at a read.
        for var in sorted(discovered - KNOWN_DIRECT_READ_ENV_VARS):
            path, line, symbol = locations[var][0]
            raw.append(
                (
                    path,
                    line,
                    symbol,
                    f"{var} is read via os.environ but missing from "
                    f"KNOWN_DIRECT_READ_ENV_VARS — add it",
                )
            )
        # Extra: in the constant but no longer read → point at the constant.
        for var in sorted(KNOWN_DIRECT_READ_ENV_VARS - discovered):
            raw.append(
                (
                    _INTROSPECTION_PY,
                    None,
                    "KNOWN_DIRECT_READ_ENV_VARS",
                    f"{var} is in KNOWN_DIRECT_READ_ENV_VARS but no longer read "
                    f"via an os.environ literal — remove it",
                )
            )

        violations = collect_violations(_RULE_KEY, raw, _RULE_ANCHOR)
        assert not violations, (
            f"G33: KNOWN_DIRECT_READ_ENV_VARS drifted from the discovered "
            f"os.environ read set ({len(violations)}). Paste the add/remove diff "
            f"into KNOWN_DIRECT_READ_ENV_VARS in "
            f"src/baldur/settings/introspection.py:\n" + "\n".join(violations)
        )
