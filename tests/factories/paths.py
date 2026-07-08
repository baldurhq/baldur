"""Location-robust repo-root / src-root resolution for test path computation.

Test files that read source files (AST / contract checks) historically hardcoded
the repo root as a fixed-depth ``Path(__file__).resolve().parents[N]``. That
depth is wrong on the public repo, where the OSS test tree ships as ``tests/``
(renamed from ``tests/oss/``, removing one path segment): a file drops one level,
so ``parents[N]`` climbs one level too high and the derived ``src/baldur/...``
path misresolves to a ``FileNotFoundError``.

These helpers walk up to the nearest ancestor holding ``pyproject.toml`` instead
of counting levels, so they resolve correctly in BOTH layouts. ``pyproject.toml``
ships to the public repo, and ``tests/factories/`` itself is NOT renamed by the public repo
(only ``tests/`` is), so this module's own location is stable. Mirrors the
self-contained ``_locate_project_root()`` in
``tests/architecture/_helpers.py``.
"""

from __future__ import annotations

from pathlib import Path

__all__ = ["repo_root", "src_root"]


def repo_root() -> Path:
    """Return the repository root — the nearest ancestor holding ``pyproject.toml``.

    Layout-agnostic: walks up from this module (``tests/factories/paths.py``,
    whose location is stable across the public repo's ``tests/`` -> ``tests/``
    rename) to the marker, never counting a fixed depth.
    """
    here = Path(__file__).resolve()
    for candidate in here.parents:
        if (candidate / "pyproject.toml").is_file():
            return candidate
    # Defensive fallback: tests/factories/paths.py -> repo root is parents[2].
    return here.parents[2]


def src_root() -> Path:
    """Return the ``src/`` directory under the repository root."""
    return repo_root() / "src"
