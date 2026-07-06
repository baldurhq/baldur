"""G20 — tests/ may not import a baldur_pro / baldur_dormant private
symbol, private-module path, or wildcard.

CLAUDE.md § Test Location Rules + impl doc 533 D5/D6/D8. The 7.5G-1 release
step builds the public OSS mirror with a path-level allowlist that publishes
``tests/`` wholesale (renamed to ``tests/``). A test that imports a PRO
internal therefore leaks that internal's name and shape to public observers —
the marker/``importorskip`` gate of G19 solves collectability but does nothing
for the leak. 533 relocates the PRO-internal-importing tests to ``tests/pro/``
(absent from the mirror); G20 is the permanent ratchet so a new one cannot be
re-introduced with zero CI signal.

What is flagged (for ``baldur_pro`` / ``baldur_dormant`` only):

  1. A private imported symbol — ``from baldur_pro.x import _foo`` where the
     name starts with ``_`` and is not a dunder (``__all__`` and ``__x__`` are
     excluded; some ``_``-prefixed names are deliberately public via
     ``__all__`` but in the mirror the PRO source is absent so G20 cannot read
     ``__all__`` — it forbids all underscore symbols, 533 D5).
  2. A private-module path — any dotted component of the source module is
     ``_``-prefixed and non-dunder, e.g.
     ``from baldur_pro.services.throttle.adaptive._helpers import X`` (533 D6).
  3. A wildcard — ``from baldur_pro... import *`` / ``from baldur_dormant...
     import *``. A wildcard can pull private names the AST cannot enumerate, so
     it is banned outright from gated top-levels (533 D5).

Known limitation (533 D5, mirrors ``_helpers.resolve_callsites``): G20 is
import-graph only. It does NOT track attribute access on an aliased module
(``import baldur_pro as bp; bp._secret``) or
``importlib.import_module("baldur_pro...._x")`` — both are runtime
expressions, not imports. The backstop is the release-time raw-text grep over
the mirrored tree (533 SC #7).

Scope direction: ``tests/`` -> (``baldur_pro`` | ``baldur_dormant``). All
``.py`` files are scanned (including conftests) — a staying conftest leaking a
PRO private symbol is a real leak.

ENFORCED-EMPTY baseline (533 D7): a baseline entry would whitelist a private
import in a file that *stays* under ``tests/`` and ships the exact leak
533 prevents, so the baseline key has no valid use — the second test method
meta-asserts it stays empty.

Architectural fitness function rule registry:
``ARCHITECTURE.md#g20-oss-test-pro-private-import``
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.architecture import _helpers as arch_helpers
from tests.architecture.conftest import (
    OSS_TESTS_ROOT,
    collect_violations,
    load_baseline,
    parse_ast,
)

_RULE_KEY = "oss_test_pro_private_import"
_RULE_ANCHOR = "#g20-oss-test-pro-private-import"
_GATED_TOP_LEVELS: tuple[str, ...] = ("baldur_pro", "baldur_dormant")


def _top_level(name: str | None) -> str | None:
    if not name:
        return None
    return name.split(".", 1)[0]


def _is_gated(name: str | None) -> bool:
    return _top_level(name) in _GATED_TOP_LEVELS


def _is_private_component(component: str) -> bool:
    """True for a private dotted-path component: ``_x`` but not a dunder."""
    if not component.startswith("_"):
        return False
    if component.startswith("__") and component.endswith("__"):
        return False
    return True


def _module_private_components(module: str | None) -> list[str]:
    if not module:
        return []
    return [part for part in module.split(".") if _is_private_component(part)]


def _scan_module(path: Path) -> list[tuple[Path, int, str]]:
    """Return ``(path, lineno, extra)`` tuples for each private/wildcard gated import."""
    tree = parse_ast(path)
    if tree is None:
        return []
    offenders: list[tuple[Path, int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if not _is_gated(node.module):
                continue
            module = node.module or _GATED_TOP_LEVELS[0]
            for component in _module_private_components(module):
                offenders.append(
                    (
                        path,
                        node.lineno,
                        f"private-module path '{module}' (component '{component}')",
                    )
                )
            for alias in node.names:
                if alias.name == "*":
                    offenders.append(
                        (path, node.lineno, f"wildcard import 'from {module} import *'")
                    )
                elif _is_private_component(alias.name):
                    offenders.append(
                        (path, node.lineno, f"private symbol '{module}.{alias.name}'")
                    )
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if not _is_gated(alias.name):
                    continue
                for component in _module_private_components(alias.name):
                    offenders.append(
                        (
                            path,
                            node.lineno,
                            f"private-module path '{alias.name}' (component '{component}')",
                        )
                    )
    return offenders


def _walk_tests_oss() -> Iterator[Path]:
    """Walk the OSS test root's .py files, skipping ``__pycache__``."""
    root = OSS_TESTS_ROOT
    if not root.exists():
        return
    for path in root.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        yield path


class TestOssTestProPrivateImports:
    """G20 — tests/ may not import baldur_pro / baldur_dormant internals."""

    def test_no_private_gated_imports_in_oss_tests(self):
        raw: list[tuple[Path, int | None, str | None, str | None]] = []
        for path in _walk_tests_oss():
            for offender_path, line, extra in _scan_module(path):
                raw.append((offender_path, line, None, extra))

        violations = collect_violations(_RULE_KEY, raw, _RULE_ANCHOR)
        assert not violations, (
            f"G20: tests/ contains {len(violations)} private / wildcard "
            "import(s) of baldur_pro / baldur_dormant. These leak PRO internals "
            "to the public mirror (tests/ ships wholesale at 7.5G-1). Move "
            "the file (or its PRO-internal test methods) to tests/pro/, or "
            "rewrite to the public API.\n" + "\n".join(violations)
        )

    def test_baseline_is_enforced_empty(self):
        # 533 D7: a G20 baseline entry would whitelist a private import in a
        # file that stays under tests/ and ships the exact leak G20 exists
        # to prevent. There is no valid use for an entry — remediation is move,
        # never baseline.
        assert load_baseline(_RULE_KEY) == {}, (
            f"G20: baseline key '{_RULE_KEY}' must stay empty (533 D7). A "
            "private-importer is moved to tests/pro/, never baselined."
        )


class TestG20Scanner:
    """`_scan_module` flags private symbols, private-module paths, wildcards (533 D5/D6)."""

    @pytest.mark.parametrize(
        ("source", "expected_count", "extra_substring"),
        [
            pytest.param(
                "from baldur_pro.x import _foo\n",
                1,
                "private symbol",
                id="private-symbol",
            ),
            pytest.param(
                "from baldur_pro.a._helpers import PUBLIC\n",
                1,
                "private-module path",
                id="private-module-path",
            ),
            pytest.param("from baldur_pro.x import *\n", 1, "wildcard", id="wildcard"),
            pytest.param(
                "from baldur_dormant.x import _foo\n",
                1,
                "private symbol",
                id="dormant-private-symbol",
            ),
            pytest.param(
                "import baldur_pro.a._helpers\n",
                1,
                "private-module path",
                id="import-form-private-module",
            ),
            pytest.param(
                "from baldur_pro.x import __all__\n", 0, None, id="dunder-excluded"
            ),
            pytest.param(
                "from baldur_pro.x import PublicThing\n",
                0,
                None,
                id="public-symbol-public-module",
            ),
            pytest.param("import baldur_pro.x\n", 0, None, id="import-form-public"),
            pytest.param(
                "from baldur.x import _private\n",
                0,
                None,
                id="non-gated-oss-underscore-ignored",
            ),
        ],
    )
    def test_scan_flags_expected_imports(
        self,
        tmp_path: Path,
        source: str,
        expected_count: int,
        extra_substring: str | None,
    ):
        path = tmp_path / "t.py"
        path.write_text(source, encoding="utf-8")
        offenders = _scan_module(path)
        assert len(offenders) == expected_count
        if extra_substring is not None:
            assert all(offender[0] == path for offender in offenders)
            assert any(extra_substring in offender[2] for offender in offenders)

    def test_scan_flags_module_path_and_symbol_separately(self, tmp_path: Path):
        # `from baldur_pro.a._helpers import _x` leaks on BOTH axes: the
        # private-module path and the private symbol → two offenders.
        path = tmp_path / "t.py"
        path.write_text("from baldur_pro.a._helpers import _x\n", encoding="utf-8")
        offenders = _scan_module(path)
        assert len(offenders) == 2
        extras = " ".join(offender[2] for offender in offenders)
        assert "private-module path" in extras
        assert "private symbol" in extras

    def test_scan_returns_empty_for_unparseable_file(self, tmp_path: Path):
        path = tmp_path / "broken.py"
        path.write_text("def f(:\n", encoding="utf-8")
        assert _scan_module(path) == []

    def test_aliased_attribute_access_is_not_flagged(self, tmp_path: Path):
        # Known limitation (533 D5): `import baldur_pro as bp; bp._secret` is a
        # runtime attribute access, not an import — outside the import graph.
        # Backstopped by the release-time raw-text grep (SC #7).
        path = tmp_path / "t.py"
        path.write_text("import baldur_pro as bp\nx = bp._secret\n", encoding="utf-8")
        assert _scan_module(path) == []

    def test_importlib_dynamic_load_is_not_flagged(self, tmp_path: Path):
        # Known limitation (533 D5): a dynamic load is a string literal, not an
        # import node.
        path = tmp_path / "t.py"
        path.write_text(
            'import importlib\nm = importlib.import_module("baldur_pro.x._y")\n',
            encoding="utf-8",
        )
        assert _scan_module(path) == []


class TestG20BaselineEnforcedEmpty:
    """The G20 meta-assertion goes red if the enforced-empty baseline key gains an entry (533 D7)."""

    def test_meta_assertion_fails_when_baseline_key_nonempty(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # Given: a baseline document with a non-empty G20 key
        path = tmp_path / "baseline.yaml"
        path.write_text(
            f"{_RULE_KEY}:\n"
            '  - {file: "tests/unit/leaky.py", reason: "x", ticket: "533"}\n',
            encoding="utf-8",
        )
        monkeypatch.setattr(arch_helpers, "BASELINE_PATH", path)
        arch_helpers._load_baseline_document.cache_clear()
        try:
            # When/Then: an entry has no valid use (533 D7) → the guard must fail
            with pytest.raises(AssertionError):
                TestOssTestProPrivateImports().test_baseline_is_enforced_empty()
        finally:
            arch_helpers._load_baseline_document.cache_clear()

    def test_meta_assertion_passes_when_baseline_key_empty(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        path = tmp_path / "baseline.yaml"
        path.write_text(f"{_RULE_KEY}: []\n", encoding="utf-8")
        monkeypatch.setattr(arch_helpers, "BASELINE_PATH", path)
        arch_helpers._load_baseline_document.cache_clear()
        try:
            TestOssTestProPrivateImports().test_baseline_is_enforced_empty()
        finally:
            arch_helpers._load_baseline_document.cache_clear()
