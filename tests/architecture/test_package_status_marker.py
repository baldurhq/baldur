"""G16 — Sub-package `__init__.py` MUST declare a `Status:` marker.

Per impl doc 508 (Wave 6A API surface freeze, decision D14) every sub-package
under `src/baldur/` (excluding the top-level package itself, which is Public
by definition) and the entire `src/baldur_pro/` tree MUST carry exactly one
``Status: Public`` or ``Status: Internal`` line inside its module docstring.

The marker is the freeze contract — it tells operators and adapter authors
whether the package is covered by SemVer guarantees, and forces the package
author to make the classification decision before shipping. The Public /
Internal *value* is governed by the runbook
(`docs/runbooks/api_surface_v1.md`); this rule only enforces marker presence.

Detection: parse `__init__.py` AST, extract module-level docstring, count
matches of ``Status: Public`` and ``Status: Internal`` lines. Exactly one
match across both is required.

Public-surface shape (impl doc 521 D12): when the marker is ``Status: Public``,
the package's module-level ``__all__`` MUST NOT contain entries beginning with
``_``. Public ``__all__`` shapes freeze into v1.x SemVer; underscore-prefixed
exports are private/deprecated by Python convention and would lock that
private-by-convention shape into the freeze contract.

Rule registry: ``ARCHITECTURE.md#g16-package-status-marker``
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from tests.architecture.conftest import (
    PROJECT_ROOT,
    collect_violations,
    parse_ast,
)

_RULE_KEY = "package_status_marker"
_RULE_ANCHOR = "#g16-package-status-marker"

_STATUS_PATTERN = re.compile(r"^Status:\s+(Public|Internal)\s*$", re.MULTILINE)

_BALDUR_ROOT = PROJECT_ROOT / "src" / "baldur"
_BALDUR_PRO_ROOT = PROJECT_ROOT / "src" / "baldur_pro"

# Explicit allowlist of OSS depth-3 Public sub-packages (impl doc 546 D9).
# Source of truth: docs/reference/index.md § Public Packages — framework
# integrations row + adapter-author-advanced row + api.admin + replay_service.
# Allowlist rather than generic recursion because internal depth-3+ packages
# (services/replay_service/handlers/, audit/wal/, bridges/tenacity/, etc.)
# intentionally stay marker-free under the D14 contract — generic recursion
# would expand absorbed scope by 4-5x.
_BALDUR_PUBLIC_DEPTH3 = (
    _BALDUR_ROOT / "adapters" / "django" / "__init__.py",
    _BALDUR_ROOT / "adapters" / "fastapi" / "__init__.py",
    _BALDUR_ROOT / "adapters" / "flask" / "__init__.py",
    _BALDUR_ROOT / "adapters" / "sql" / "__init__.py",
    _BALDUR_ROOT / "adapters" / "gunicorn" / "__init__.py",
    _BALDUR_ROOT / "adapters" / "memory" / "__init__.py",
    _BALDUR_ROOT / "adapters" / "cache" / "__init__.py",
    _BALDUR_ROOT / "api" / "admin" / "__init__.py",
    _BALDUR_ROOT / "services" / "replay_service" / "__init__.py",
)


def _is_top_level_init(path: Path) -> bool:
    return path == _BALDUR_ROOT / "__init__.py"


def _iter_sub_package_inits() -> list[Path]:
    """Return every ``__init__.py`` covered by the marker contract.

    Scope per D14 (+ impl doc 546 D9 depth-3 OSS Public extension):
    - Every direct sub-package under ``src/baldur/`` (depth=2 — e.g.
      ``adapters/__init__.py``, ``services/__init__.py``).
    - The 9 OSS depth-3 Public sub-packages listed in
      ``_BALDUR_PUBLIC_DEPTH3`` (546 D9 — closes the classification-vs-
      enforcement gap exposed by ``docs/reference/index.md`` § Public Packages).
    - The PRO root ``src/baldur_pro/__init__.py`` AND every
      ``src/baldur_pro/services/*/__init__.py`` (every PRO service package).
      Deeper PRO sub-packages stay in the runbook, mirroring the baldur scope.

    Internal depth-3+ packages (services/replay_service/handlers/, audit/wal/,
    bridges/tenacity/, etc.) intentionally stay marker-free under D14 — the
    explicit allowlist preserves that boundary.

    Excludes the top-level ``src/baldur/__init__.py`` (Public by definition).
    """
    paths: list[Path] = []
    if _BALDUR_ROOT.exists():
        for entry in sorted(_BALDUR_ROOT.iterdir()):
            if not entry.is_dir():
                continue
            init = entry / "__init__.py"
            if init.exists():
                paths.append(init)
    for depth3 in _BALDUR_PUBLIC_DEPTH3:
        if depth3.exists():
            paths.append(depth3)
    if _BALDUR_PRO_ROOT.exists():
        pro_init = _BALDUR_PRO_ROOT / "__init__.py"
        if pro_init.exists():
            paths.append(pro_init)
        services_root = _BALDUR_PRO_ROOT / "services"
        if services_root.exists():
            services_init = services_root / "__init__.py"
            if services_init.exists():
                paths.append(services_init)
            for entry in sorted(services_root.iterdir()):
                if not entry.is_dir():
                    continue
                init = entry / "__init__.py"
                if init.exists():
                    paths.append(init)
    return paths


def _audit_marker(path: Path) -> tuple[int | None, str] | None:
    tree = parse_ast(path)
    if tree is None:
        return None
    docstring = ast.get_docstring(tree)
    if docstring is None:
        return (1, "missing module docstring (no Status: marker possible)")
    matches = _STATUS_PATTERN.findall(docstring)
    if not matches:
        return (
            1,
            "module docstring missing 'Status: Public' or 'Status: Internal' line",
        )
    if len(matches) > 1:
        return (
            1,
            f"module docstring carries {len(matches)} Status: markers — "
            "exactly one is required",
        )
    return None


def _underscore_all_entries(tree: ast.Module) -> list[str]:
    """Return module-level ``__all__`` string entries starting with ``_``.

    Returns an empty list when ``__all__`` is missing or not an inline list /
    tuple of string constants (dynamic ``__all__`` shapes are out of scope —
    the contract assumes the static, AST-readable form used by every PRO
    sub-package today).
    """
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name) or target.id != "__all__":
            continue
        value = node.value
        if not isinstance(value, (ast.List, ast.Tuple)):
            continue
        return [
            elt.value
            for elt in value.elts
            if isinstance(elt, ast.Constant)
            and isinstance(elt.value, str)
            and elt.value.startswith("_")
        ]
    return []


def _audit_public_underscore_exports(path: Path) -> tuple[int | None, str] | None:
    """D12 — Public packages MUST NOT export underscore-prefixed names.

    Returns a violation only when the marker is exactly ``Status: Public`` and
    ``__all__`` contains one or more underscore-prefixed entries. Marker
    presence/uniqueness is the responsibility of ``_audit_marker``.
    """
    tree = parse_ast(path)
    if tree is None:
        return None
    docstring = ast.get_docstring(tree)
    if docstring is None:
        return None
    matches = _STATUS_PATTERN.findall(docstring)
    if matches != ["Public"]:
        return None
    leaked = _underscore_all_entries(tree)
    if not leaked:
        return None
    return (
        1,
        f"Status: Public package exposes underscore-prefixed __all__ entries "
        f"{sorted(leaked)} — these would freeze into v1.x SemVer. "
        "Either drop the entries or downgrade the marker to Status: Internal.",
    )


class TestPackageStatusMarkerContract:
    """G16 — every sub-package `__init__.py` declares its Public/Internal status."""

    def test_every_package_has_status_marker(self):
        raw: list[tuple[Path, int | None, str | None, str | None]] = []
        for path in _iter_sub_package_inits():
            audit = _audit_marker(path)
            if audit is None:
                continue
            line, extra = audit
            raw.append((path, line, None, extra))

        violations = collect_violations(_RULE_KEY, raw, _RULE_ANCHOR)
        assert not violations, (
            f"G16: package Status: marker regressions ({len(violations)}). "
            "Add exactly one 'Status: Public' or 'Status: Internal' line to the "
            "module docstring per docs/runbooks/api_surface_v1.md classification, "
            "or add a baseline entry under `package_status_marker:` with reason+ticket.\n"
            + "\n".join(violations)
        )

    def test_public_packages_have_no_underscore_exports(self):
        raw: list[tuple[Path, int | None, str | None, str | None]] = []
        for path in _iter_sub_package_inits():
            audit = _audit_public_underscore_exports(path)
            if audit is None:
                continue
            line, extra = audit
            raw.append((path, line, None, extra))

        violations = collect_violations(_RULE_KEY, raw, _RULE_ANCHOR)
        assert not violations, (
            f"G16: Public packages with underscore-prefixed __all__ entries "
            f"({len(violations)}). Public surfaces freeze into v1.x SemVer so "
            "private/deprecated leakage is forbidden — either rename the export "
            "to drop the underscore prefix, remove it from __all__, or downgrade "
            "the package to Status: Internal.\n" + "\n".join(violations)
        )
