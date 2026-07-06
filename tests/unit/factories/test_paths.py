"""Unit tests for ``tests/factories/paths.py`` — location-robust repo/src root (663 D1).

The helper replaces the fixed-depth ``Path(__file__).resolve().parents[N]`` path
computation that broke on the published OSS mirror, where ``publish_mirror.sh``
renames ``tests/`` -> ``tests/`` (shortening the tree by one level so a fixed
depth climbs one level too high). These tests assert the walk-to-marker contract
holds in BOTH layouts: the result holds ``pyproject.toml`` and ``src/baldur``
regardless of depth, keyed on a marker present in both layouts rather than a count.

Test plan source: 663 `## Test Assessment`.
"""

from __future__ import annotations

from pathlib import Path

from tests.factories import repo_root, src_root
from tests.factories.paths import repo_root as repo_root_direct


class TestRepoRootHelper:
    """663 D1 — repo_root()/src_root() resolve by marker, not by fixed depth."""

    def test_repo_root_holds_pyproject(self):
        # The walk-to-marker invariant: the returned dir IS the one with the
        # marker file, so the depth math can never drift.
        assert (repo_root() / "pyproject.toml").is_file()

    def test_src_root_holds_baldur(self):
        # baldur ships in BOTH layouts (monorepo + the PRO-absent mirror), so this
        # assertion is layout-robust — the load-bearing reason the helper exists.
        assert (src_root() / "baldur").is_dir()

    def test_src_root_is_repo_root_src(self):
        assert src_root() == repo_root() / "src"

    def test_repo_root_returns_absolute_path(self):
        assert isinstance(repo_root(), Path)
        assert isinstance(src_root(), Path)
        assert repo_root().is_absolute()

    def test_repo_root_is_ancestor_of_this_module(self):
        # Locality: the marker found is a genuine ancestor of this test module, so
        # the walk terminated at a real parent — not the defensive depth fallback.
        here = Path(__file__).resolve()
        assert repo_root() in here.parents

    def test_repo_root_is_idempotent(self):
        # Pure: repeated calls return the same path (no hidden state).
        assert repo_root() == repo_root()

    def test_reexport_is_the_same_callable(self):
        # The tests.factories re-export is the same object as the module's helper.
        assert repo_root is repo_root_direct
