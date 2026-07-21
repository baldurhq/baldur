"""G33 — direct-read ``BALDUR_*`` registry MUST equal the discovered read set.

Impl doc 576. The runtime unknown-env-var scan
(``bootstrap._warn_unknown_env_vars``) treats a ``BALDUR_*`` key as *known* if it
either resolves to a Pydantic settings field OR is catalogued in
``baldur.settings.introspection.KNOWN_DIRECT_READ_ENV_VARS`` — the OSS vars read
straight from ``os.environ`` via a string literal, with no backing Pydantic
field. If that constant drifts from the real source reads, the scan either
false-positives (a real direct-read var warns as unknown) or under-covers (a
removed read lingers in the constant). This gate keeps the two enforced-equal.

**Detection.** AST-scan of ``src/baldur`` for the three key read shapes:

* ``os.environ.get(KEY)`` / ``os.environ.get(KEY, default)``,
* ``os.getenv(KEY)``,
* ``os.environ[KEY]`` **reads** (``ast.Load`` only — an ``os.environ[...] =``
  *write*, e.g. ``cli/_config.apply_config_to_env``, is an ``ast.Store``
  subscript and correctly excluded).

``KEY`` is either a ``"BALDUR_…"`` string literal or a **named module-level
constant** bound to one (``WAL_DIR_ENV_VAR = "BALDUR_AUDIT_WAL_DIR"``, read as
``os.environ.get(WAL_DIR_ENV_VAR)``). Constants are resolved tree-wide, not just
within the reading module, because the canonical name is typically defined next
to its settings and imported by the reader. Resolving them is load-bearing:
centralizing a name into a constant is the *preferred* direction, and a
literal-only scan would report the var as "no longer read" and invite deleting a
registry entry that is still the var's only registration — which would make a
correctly-set variable warn as unknown at startup.

A constant name bound to two different values anywhere in the tree is ambiguous
and left unresolved (the reading module's own binding still wins), so a
collision degrades to the Channel-2 seam rather than guessing.

Genuinely computed reads (``f"BALDUR_{x}"``, e.g. ``DegradedModeHandler.get``)
have no static key at all and remain invisible by construction — they are
covered via the ``register_direct_read_env_vars`` Channel-2 seam.

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
from collections.abc import Callable
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


def _module_level_baldur_constants(tree: ast.Module) -> dict[str, str]:
    """Map module-level ``NAME = "BALDUR_…"`` bindings to their value.

    Only top-level assignments are collected: a name rebound inside a function
    is not a stable canonical-name constant.
    """
    constants: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets: list[ast.expr] = list(node.targets)
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        else:
            continue

        value = node.value
        if not (
            isinstance(value, ast.Constant)
            and isinstance(value.value, str)
            and value.value.startswith(_BALDUR_PREFIX)
        ):
            continue
        for target in targets:
            if isinstance(target, ast.Name):
                constants[target.id] = value.value
    return constants


def _resolve_key(node: ast.expr, lookup: Callable[[str], str | None]) -> str | None:
    """Resolve an env-key expression to a ``BALDUR_*`` name, if it is static.

    Handles a string literal, a bare constant name, and an attribute access on
    an imported module (``_models.WAL_DIR_ENV_VAR``) — the attribute leaf goes
    through the same lookup.
    """
    if isinstance(node, ast.Constant):
        if isinstance(node.value, str) and node.value.startswith(_BALDUR_PREFIX):
            return node.value
        return None
    if isinstance(node, ast.Name):
        return lookup(node.id)
    if isinstance(node, ast.Attribute):
        return lookup(node.attr)
    return None


def _iter_baldur_reads(
    tree: ast.Module, lookup: Callable[[str], str | None]
) -> list[tuple[str, ast.AST]]:
    """Yield ``(var, node)`` for every static-key ``BALDUR_*`` os.environ READ.

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
                if not node.args:
                    continue
                var = _resolve_key(node.args[0], lookup)
                if var is not None:
                    reads.append((var, node))
        elif (
            isinstance(node, ast.Subscript)
            and _is_os_environ(node.value)
            and isinstance(node.ctx, ast.Load)
        ):
            var = _resolve_key(node.slice, lookup)
            if var is not None:
                reads.append((var, node))
    return reads


def _module_name(path: Path) -> str:
    """Dotted module name for a file under ``src`` (``__init__`` → its package)."""
    relative = path.relative_to(PROJECT_ROOT / "src").with_suffix("")
    parts = list(relative.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _imported_names(tree: ast.Module, module: str) -> dict[str, str]:
    """Map each ``from X import NAME`` binding to the dotted module ``X``.

    Relative imports are resolved against ``module`` so a package-internal
    re-export (``from ._models import WAL_DIR_ENV_VAR``) is followable.
    """
    imports: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        if node.level:
            base = module.split(".")
            trimmed = base[: len(base) - node.level + 1] if node.level > 1 else base
            source = ".".join([*trimmed, node.module] if node.module else trimmed)
        else:
            source = node.module or ""
        for alias in node.names:
            if alias.name != "*":
                imports[alias.asname or alias.name] = source
    return imports


def _resolve_through_imports(
    name: str,
    module: str,
    constants: dict[str, dict[str, str]],
    imports: dict[str, dict[str, str]],
    seen: frozenset[str] = frozenset(),
) -> str | None:
    """Resolve ``name`` in ``module``, following re-export chains.

    A reader imports the canonical name from the module that owns the setting,
    which is often a package ``__init__`` re-exporting from a private sibling,
    so a single hop is not enough.
    """
    if module in seen:
        return None
    local = constants.get(module, {})
    if name in local:
        return local[name]
    source = imports.get(module, {}).get(name)
    if source is None:
        return None
    return _resolve_through_imports(name, source, constants, imports, seen | {module})


def _discover_direct_reads() -> dict[str, list[tuple[Path, int | None, str]]]:
    """Map each discovered ``BALDUR_*`` read to its ``(file, line, symbol)`` sites.

    Two passes: index every module's top-level ``BALDUR_*`` constants and its
    ``from X import NAME`` bindings, then resolve each read's key against the
    reading module — its own constants first, then the import chain. Import
    following is what makes a centralized name work: the same constant
    *identifier* is reused across surfaces for different variables
    (``WAL_DIR_ENV_VAR`` names the audit WAL dir in one module and the
    resilient-storage WAL dir in another), so resolving by bare name would be
    ambiguous, while resolving through the import edge is exact.
    """
    parsed: list[tuple[Path, ast.Module, str]] = []
    constants: dict[str, dict[str, str]] = {}
    imports: dict[str, dict[str, str]] = {}

    for path in walk_src([_SRC_BALDUR]):
        tree = parse_ast(path)
        if tree is None:
            continue
        module = _module_name(path)
        parsed.append((path, tree, module))
        constants[module] = _module_level_baldur_constants(tree)
        imports[module] = _imported_names(tree, module)

    locations: dict[str, list[tuple[Path, int | None, str]]] = defaultdict(list)
    for path, tree, module in parsed:

        def _lookup(name: str, _module: str = module) -> str | None:
            return _resolve_through_imports(name, _module, constants, imports)

        for var, node in _iter_baldur_reads(tree, _lookup):
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
