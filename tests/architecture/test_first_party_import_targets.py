"""G38 — first-party import targets MUST resolve to a real module.

A ``from baldur....x import Y`` / ``from .x import Y`` whose **FROM-module**, or
an ``import baldur....x`` whose dotted target, does not exist on disk is a
latent broken import that the existing gates all miss:

- **ruff** does not resolve imports against the filesystem (by design — that is
  a type-checker's job), so a non-existent module is invisible to it.
- **pytest** misses it when the import is lazy (inside a function) AND guarded
  by ``try/except ImportError`` — the import never runs at collection time and
  the runtime ``ImportError`` is swallowed, silently disabling the feature.
- **mypy** would catch it conceptually, but ``pyproject.toml`` sets
  ``ignore_missing_imports = true`` globally (silencing ``import-not-found``),
  and mypy is CI-only + baseline-gated.

So the class surfaces only in-editor via Pylance ``reportMissingImports``. The
guarded form is the worst case: ``try: from X import Y; except Exception: pass``
disables a feature *forever* with zero signal — e.g. the chaos stop-conditions
/ TTL / dry-run checks were dead because ``chaos/base/experiment.py`` imported
``chaos.base.stop_conditions`` (one level too deep) instead of
``chaos.stop_conditions``.

Detection (filesystem, side-effect-free — imports nothing):
- AST-walk every ``.py`` under the first-party src roots.
- For each ``ast.ImportFrom``, resolve the FROM-module to an absolute dotted
  path (absolute imports as-is; relative imports anchored at the file's
  package, climbing ``level - 1`` parents).
- For each ``ast.Import`` (``import a.b.c`` / ``import a.b.c as d``), the dotted
  target is always a module (no symbol/submodule ambiguity), so it is checked
  directly.
- Keep only first-party targets (top-level in ``baldur`` / ``baldur_pro`` /
  ``baldur_dormant``); third-party imports are out of scope.
- Map the dotted path to ``src/<path>.py`` or ``src/<path>/__init__.py`` and
  flag when neither exists.

Known limitations (kept narrow to hold the false-positive rate at zero):
- For ``from pkg import sub``, only the **FROM-module** (``pkg``) is checked,
  not each imported name. A *missing submodule* ``sub`` is NOT flagged, because
  a symbol and a submodule are indistinguishable statically (``sub`` may be a
  class re-exported from ``pkg/__init__.py``). The FROM-module check caught
  every real instance of this bug class with no false positives. (Plain
  ``import a.b.c`` has no such ambiguity and IS checked in full.)
- Dynamic ``importlib.import_module(var)`` dispatch is not statically
  resolvable and is out of scope.
- On a public-OSS-only checkout the ``baldur_pro`` / ``baldur_dormant`` roots
  are absent; ``walk_src`` skips missing roots, so the gate narrows to
  ``baldur`` gracefully.

Enforced-empty: every violation is a real broken import to fix, never baselined.

Rule registry: ``ARCHITECTURE.md#g38-first-party-import-targets``
"""

from __future__ import annotations

import ast
from pathlib import Path

from tests.architecture.conftest import PROJECT_ROOT, parse_ast, walk_src

_SRC = PROJECT_ROOT / "src"
_FIRST_PARTY_ROOTS = ("baldur", "baldur_pro", "baldur_dormant")
_SCAN_ROOTS = tuple(_SRC / name for name in _FIRST_PARTY_ROOTS)


def _module_parts(path: Path) -> list[str]:
    """Dotted parts of the module ``path`` defines (``__init__`` stripped)."""
    rel = path.relative_to(_SRC).with_suffix("")
    parts = list(rel.parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return parts


def _package_parts(path: Path) -> list[str]:
    """Dotted parts of the package that *contains* the relative-import anchor.

    For ``__init__.py`` the package is the directory itself; for a regular
    module the package is its parent directory.
    """
    parts = _module_parts(path)
    if path.name == "__init__.py":
        return parts
    return parts[:-1]


def _resolve_from_module(path: Path, node: ast.ImportFrom) -> list[str] | None:
    """Resolve an ``ImportFrom`` to its absolute FROM-module dotted parts.

    Returns ``None`` for ``from . import x`` (module is ``None`` — the
    FROM-module is the package itself, which trivially exists) and for
    over-deep relative imports (which Python itself rejects at import time).
    """
    if node.level == 0:
        return node.module.split(".") if node.module else None
    pkg = _package_parts(path)
    up = node.level - 1
    if up > len(pkg):
        return None
    base = pkg[: len(pkg) - up]
    if node.module:
        base = base + node.module.split(".")
    return base or None


def _module_exists(dotted: list[str]) -> bool:
    """True when the dotted module resolves to a ``.py`` or package on disk."""
    base = _SRC.joinpath(*dotted)
    return base.with_suffix(".py").exists() or (base / "__init__.py").exists()


def _root_present(target: list[str]) -> bool:
    """True when the target's top-level first-party root exists on disk.

    On a public-OSS-only checkout (the mirror) ``baldur_pro`` / ``baldur_dormant``
    are absent; a deferred ``from baldur_pro… import …`` carried by OSS ``baldur``
    source is then a legitimately-absent cross-tier import, not a broken path, so
    its existence check is skipped. ``baldur`` is always present, so ``baldur.*``
    targets are always checked — a genuinely-broken published-source import path
    still fails the gate on the mirror. (In the monorepo every root is present, so
    this guard is a no-op and a missing PRO submodule is still flagged.)
    """
    return (_SRC / target[0]).is_dir()


def _broken_import_targets() -> list[str]:
    violations: list[str] = []
    for path in walk_src(_SCAN_ROOTS):
        tree = parse_ast(path)
        if tree is None:
            continue
        rel = path.relative_to(PROJECT_ROOT).as_posix()
        for node in ast.walk(tree):
            # `from X import ...` / `from .x import ...` — check the FROM-module.
            if isinstance(node, ast.ImportFrom):
                target = _resolve_from_module(path, node)
                if target and target[0] in _FIRST_PARTY_ROOTS and _root_present(target):
                    if not _module_exists(target):
                        violations.append(f"{rel}:{node.lineno} -> {'.'.join(target)}")
            # `import a.b.c` / `import a.b.c as d` — the dotted target is always
            # a module (no symbol/submodule ambiguity), so a missing first-party
            # target is unambiguously a broken import.
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    target = alias.name.split(".")
                    if (
                        target
                        and target[0] in _FIRST_PARTY_ROOTS
                        and _root_present(target)
                    ):
                        if not _module_exists(target):
                            violations.append(f"{rel}:{node.lineno} -> {alias.name}")
    return violations


class TestFirstPartyImportTargets:
    """G38 — first-party ``from`` import targets MUST exist on disk."""

    def test_no_broken_first_party_import_targets(self):
        violations = sorted(_broken_import_targets())
        assert not violations, (
            f"G38: {len(violations)} first-party `from ... import` target(s) do "
            "not resolve to a real module on disk. Fix the import path — a "
            "`try/except ImportError` around a wrong path silently disables a "
            "feature. Registry: ARCHITECTURE.md"
            "#g38-first-party-import-targets\n" + "\n".join(violations)
        )

    def test_relative_resolution_climbs_correct_parents(self):
        """The resolver math that distinguishes the bug from the fix.

        ``chaos/base/experiment.py`` doing ``from .stop_conditions`` (level 1)
        wrongly resolves to ``chaos.base.stop_conditions`` (the original bug);
        ``from ..stop_conditions`` (level 2) correctly resolves to
        ``chaos.stop_conditions``.
        """
        p = _SRC / "baldur_pro" / "services" / "chaos" / "base" / "experiment.py"
        level1 = ast.ImportFrom(module="stop_conditions", names=[], level=1)
        level2 = ast.ImportFrom(module="stop_conditions", names=[], level=2)
        assert _resolve_from_module(p, level1) == [
            "baldur_pro",
            "services",
            "chaos",
            "base",
            "stop_conditions",
        ]
        assert _resolve_from_module(p, level2) == [
            "baldur_pro",
            "services",
            "chaos",
            "stop_conditions",
        ]

    def test_module_exists_detects_missing(self):
        """Non-vacuity: the existence check flags a missing module and accepts
        a real one."""
        assert _module_exists(["baldur", "core", "exceptions"])
        assert not _module_exists(["baldur", "this_module_does_not_exist_xyz"])
        # The exact pre-fix bug target must read as missing.
        assert not _module_exists(
            ["baldur_pro", "services", "chaos", "base", "stop_conditions"]
        )

    def test_plain_import_target_is_checked(self):
        """The ``ast.Import`` branch resolves the dotted target directly.

        ``import baldur.<missing>`` has no symbol/submodule ambiguity, so a
        missing first-party target is unambiguously broken.
        """
        tree = ast.parse("import baldur.this_module_does_not_exist_xyz as x")
        node = next(n for n in ast.walk(tree) if isinstance(n, ast.Import))
        target = node.names[0].name.split(".")
        assert target[0] in _FIRST_PARTY_ROOTS
        assert not _module_exists(target)


class TestG38AbsentRootSkip:
    """663 D2 — an absent first-party root is skipped (not flagged) on the mirror.

    On a public-OSS-only checkout ``baldur_pro`` / ``baldur_dormant`` are absent;
    a deferred cross-tier ``from baldur_pro… import …`` carried by OSS source is a
    legitimately-absent import, not a broken path. ``baldur`` is always present so
    its targets are always checked — a genuinely-broken published-source import
    path still fails the gate on the mirror.
    """

    _MOD = "tests.architecture.test_first_party_import_targets"

    @staticmethod
    def _baldur_only_src(tmp_path):
        # A src/ holding ONLY baldur (the mirror layout) — no private tiers.
        (tmp_path / "baldur").mkdir()
        return tmp_path

    def test_baldur_root_always_checked(self, monkeypatch, tmp_path):
        monkeypatch.setattr(f"{self._MOD}._SRC", self._baldur_only_src(tmp_path))
        assert _root_present(["baldur", "core", "exceptions"]) is True

    def test_absent_pro_root_not_checked(self, monkeypatch, tmp_path):
        monkeypatch.setattr(f"{self._MOD}._SRC", self._baldur_only_src(tmp_path))
        assert _root_present(["baldur_pro", "services", "postmortem"]) is False

    def test_absent_dormant_root_not_checked(self, monkeypatch, tmp_path):
        monkeypatch.setattr(f"{self._MOD}._SRC", self._baldur_only_src(tmp_path))
        assert _root_present(["baldur_dormant", "adapters", "kafka"]) is False

    def test_present_private_root_is_checked(self, monkeypatch, tmp_path):
        # When the private tier IS present (the monorepo), the guard is a no-op:
        # the root reads present, so a missing submodule under it is still flagged.
        (tmp_path / "baldur").mkdir()
        (tmp_path / "baldur_pro").mkdir()
        monkeypatch.setattr(f"{self._MOD}._SRC", tmp_path)
        assert _root_present(["baldur_pro", "services", "postmortem"]) is True
