"""Unit test for the tests/pro/ collection guard (533 D13).

``tests/pro/conftest.py`` skips the whole private subtree when ``baldur_pro`` is
absent, so a private-repo checkout without the PRO package collects clean instead of
crashing with ``ModuleNotFoundError`` on the ~340 moved files. This test loads
that conftest by file path and exercises ``pytest_ignore_collect`` under both
availability branches (533 D13 Test Assessment row).

The conftest under test lives in the private tree (not published to the public repo), so in a public OSS checkout the file is absent — the module-level
skipif keeps this test public-repo-safe. The test references ``baldur_pro`` only as a
``find_spec`` string, never importing a PRO symbol, so it leaks no PRO shape.
"""

from __future__ import annotations

import importlib.util
from types import ModuleType

import pytest

from tests.architecture.conftest import PROJECT_ROOT

_PRO_CONFTEST = PROJECT_ROOT / "tests" / "pro" / "conftest.py"

pytestmark = pytest.mark.skipif(
    not _PRO_CONFTEST.exists(),
    reason="tests/pro/conftest.py absent (public OSS repo checkout); guard is private-tree only",
)


def _load_pro_conftest() -> ModuleType:
    """Load tests/pro/conftest.py as a standalone module by file path.

    A unique module name avoids colliding with pytest's own conftest loading.
    """
    spec = importlib.util.spec_from_file_location(
        "baldur_pro_conftest_under_test", _PRO_CONFTEST
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestProConftestCollectionGuard:
    """`pytest_ignore_collect` skips the subtree iff baldur_pro is unavailable (533 D13)."""

    def test_pro_available_flag_reflects_find_spec(self):
        # Behavior: the module derives availability from importlib.util.find_spec,
        # not a hardcoded constant.
        module = _load_pro_conftest()
        expected = importlib.util.find_spec("baldur_pro") is not None
        assert module._PRO_AVAILABLE is expected

    def test_ignore_collect_skips_subtree_when_pro_absent(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        # Given: baldur_pro is not installed
        module = _load_pro_conftest()
        monkeypatch.setattr(module, "_PRO_AVAILABLE", False)
        # When/Then: every path under tests/pro/ is ignored (True == skip collection)
        assert module.pytest_ignore_collect(_PRO_CONFTEST, None) is True

    def test_ignore_collect_defers_to_default_when_pro_present(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        # Given: baldur_pro is installed
        module = _load_pro_conftest()
        monkeypatch.setattr(module, "_PRO_AVAILABLE", True)
        # When/Then: returns None → defer to pytest's default collection (do not ignore)
        assert module.pytest_ignore_collect(_PRO_CONFTEST, None) is None


__all__ = ["TestProConftestCollectionGuard"]
