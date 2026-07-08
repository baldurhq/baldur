"""G50 — tests/ may not reference a gated tier at RUNTIME without an importorskip.

The public OSS mirror ships ``src/baldur/`` only — BOTH private tiers
``baldur_pro`` AND ``baldur_dormant`` are absent. Two reference forms break a
test there but slip past the existing boundary gates (impl doc 659 G4):

  (a) ``patch("baldur_pro…")`` / ``patch("baldur_dormant…")`` — ``mock.patch``
      imports its string target at ``__enter__``, so the test errors when it runs.
      G20 is import-graph only and never reads ``patch`` string literals.
  (b) a function-body ``from baldur_pro… import …`` / ``import baldur_dormant…`` —
      runs only when the test runs, so it does not break collection (G19 keys on
      module-level imports) but errors that test.

The skip mechanism that works on the mirror is ``pytest.importorskip(...)`` (the
``requires_pro`` MARKER alone does NOT skip — the mirror runs every test). Because
both tiers are absent together on the mirror, an ``importorskip`` for *either*
gated package fires there and skips the test before any gated reference runs.

**Detection (per-file).** A file is flagged when it contains a gated runtime ref
(NOT inside a ``try/except ImportError`` — that degrades gracefully) AND no
``importorskip("baldur_pro"|"baldur_dormant")`` is reachable: not in the file
(module-level, a test, a fixture, or a class hook) and not in any ancestor
``conftest.py`` (which can skip the whole subtree or supply a guarding fixture).

**Why per-file, not per-reference (659 D7 / known limitation).** Whether a given
ref executes PRO-absent depends on the fixture graph — a test skips via a fixture
it requests, a helper runs only through guarded callers — which is not statically
decidable across files. So G50 is a coarse-but-robust ratchet: it catches the
common regression (a new file with gated refs and zero guard) with near-zero
false positives, and delegates per-test precision to the public repo's native
``ci`` workflow, which runs `tests/oss/` PRO-absent. A file that
already carries a guard but adds a new unguarded ref is caught there, not here.

ENFORCED-EMPTY baseline (659 D7): an entry would whitelist a tests/oss file that
breaks on the mirror — the exact regression this gate blocks. Remediation is to
guard the seam (or relocate a PRO-SUT file to tests/pro/), never to baseline.

Architectural fitness function rule registry:
``ARCHITECTURE.md#g50-oss-test-pro-runtime-refs``
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
)

_RULE_KEY = "oss_test_pro_runtime_ref"
_RULE_ANCHOR = "#g50-oss-test-pro-runtime-refs"
_GATED: tuple[str, ...] = ("baldur_pro", "baldur_dormant")


def _gated_prefix(text: object) -> bool:
    if not isinstance(text, str) or not text:
        return False
    return text.split(".", 1)[0] in _GATED


def _func_name_chain(func: ast.AST) -> str:
    """Dotted name chain of a call target, e.g. ``mock.patch`` / ``pytest.importorskip``."""
    parts: list[str] = []
    node: ast.AST | None = func
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def _is_importorskip_call(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and _func_name_chain(node.func).split(".")[-1] == "importorskip"
        and any(
            isinstance(a, ast.Constant) and _gated_prefix(a.value) for a in node.args
        )
    )


def _is_gated_patch_call(node: ast.AST) -> bool:
    """A ``patch("baldur_pro…"|"baldur_dormant…")`` call — gated string is the target."""
    if not isinstance(node, ast.Call):
        return False
    chain = _func_name_chain(node.func).split(".")
    if (
        not chain or chain[-1] != "patch"
    ):  # patch(...) / mock.patch(...), NOT .object/.dict
        return False
    return any(
        isinstance(a, ast.Constant) and _gated_prefix(a.value) for a in node.args
    )


def _is_gated_import(node: ast.AST) -> bool:
    if isinstance(node, ast.ImportFrom):
        return _gated_prefix(node.module)
    if isinstance(node, ast.Import):
        return any(_gated_prefix(a.name) for a in node.names)
    return False


def _handler_catches(handler: ast.ExceptHandler) -> bool:
    if handler.type is None:
        return True
    names: list[str] = []
    if isinstance(handler.type, ast.Name):
        names = [handler.type.id]
    elif isinstance(handler.type, ast.Tuple):
        names = [e.id for e in handler.type.elts if isinstance(e, ast.Name)]
    return any(n in ("ImportError", "ModuleNotFoundError", "Exception") for n in names)


def _soft_lines(tree: ast.AST) -> set[int]:
    """Lines inside a ``try`` BODY whose handlers catch ImportError/Exception — a
    gated import/patch there degrades gracefully, so it does not break the mirror."""
    soft: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Try) and any(
            _handler_catches(h) for h in node.handlers
        ):
            for stmt in node.body:
                for n in ast.walk(stmt):
                    lineno = getattr(n, "lineno", None)
                    if lineno is not None:
                        soft.add(lineno)
    return soft


def _tree_has_importorskip(tree: ast.AST) -> bool:
    return any(
        _is_importorskip_call(n) for n in ast.walk(tree) if isinstance(n, ast.Call)
    )


def _ancestor_conftest_guards(path: Path) -> bool:
    """Any ``conftest.py`` in this file's directory or an ancestor (up to the OSS
    test root) that carries an ``importorskip`` — it can skip the subtree or
    supply a guarding fixture."""
    root = OSS_TESTS_ROOT.resolve()
    resolved = path.resolve()
    if resolved != root and root not in resolved.parents:
        return False
    current = resolved.parent
    while True:
        conftest = current / "conftest.py"
        if conftest != resolved and conftest.exists():
            try:
                if _tree_has_importorskip(ast.parse(conftest.read_text("utf-8"))):
                    return True
            except (SyntaxError, ValueError, OSError, UnicodeDecodeError):
                pass
        if current == root:
            break
        current = current.parent
    return False


def _gated_runtime_refs(tree: ast.Module, soft: set[int]) -> list[tuple[int, str]]:
    """``(lineno, message)`` for each gated patch-string and function-body gated
    import not inside a graceful ``try/except``."""
    refs: list[tuple[int, str]] = []

    def visit(node: ast.AST, in_function: bool) -> None:
        if (
            isinstance(node, ast.Call)
            and _is_gated_patch_call(node)
            and node.lineno not in soft
        ):
            refs.append((node.lineno, "unguarded patch('baldur_pro/baldur_dormant…')"))
        if (
            isinstance(node, (ast.ImportFrom, ast.Import))
            and _is_gated_import(node)
            and in_function
            and node.lineno not in soft
        ):
            refs.append((node.lineno, "unguarded function-body gated import"))
        child_in_function = in_function or isinstance(
            node, (ast.FunctionDef, ast.AsyncFunctionDef)
        )
        for child in ast.iter_child_nodes(node):
            visit(child, child_in_function)

    visit(tree, False)
    return refs


def _scan_module(path: Path) -> list[tuple[Path, int, str]]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (SyntaxError, ValueError, OSError, UnicodeDecodeError):
        return []
    refs = _gated_runtime_refs(tree, _soft_lines(tree))
    if not refs:
        return []
    if _tree_has_importorskip(tree) or _ancestor_conftest_guards(path):
        return []  # a reachable importorskip skips the test before any ref runs
    return [(path, lineno, message) for lineno, message in refs]


def _walk_tests_oss() -> Iterator[Path]:
    root = OSS_TESTS_ROOT
    if not root.exists():
        return
    for path in root.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        yield path


class TestOssTestProRuntimeRefs:
    """G50 — tests/ runtime gated refs MUST be importorskip-guarded."""

    def test_no_unguarded_pro_runtime_refs(self):
        raw: list[tuple[Path, int | None, str | None, str | None]] = []
        for path in _walk_tests_oss():
            for offender_path, line, extra in _scan_module(path):
                raw.append((offender_path, line, None, extra))
        violations = collect_violations(_RULE_KEY, raw, _RULE_ANCHOR)
        assert not violations, (
            f"G50: {len(violations)} unguarded gated runtime reference(s) in "
            "tests/. A patch('baldur_pro/baldur_dormant…') target or a "
            "function-body gated import breaks on the public mirror (both private "
            "tiers absent). Add `pytest.importorskip('baldur_pro')` (per-test, a "
            "class autouse fixture, a fixture, or module top) — or relocate a "
            "PRO-SUT file to tests/pro/.\n" + "\n".join(violations)
        )

    def test_baseline_is_enforced_empty(self):
        assert load_baseline(_RULE_KEY) == {}, (
            f"G50: baseline key '{_RULE_KEY}' must stay empty. A mirror break in "
            "tests/ is guarded or relocated, never baselined."
        )


class TestG50Scanner:
    """`_scan_module` flags only files with an UNGUARDED gated runtime ref."""

    def _write(self, tmp_path: Path, source: str) -> Path:
        path = tmp_path / "t.py"
        path.write_text(source, encoding="utf-8")
        return path

    def test_unguarded_patch_string_flagged(self, tmp_path: Path):
        src = (
            "from unittest.mock import patch\n"
            "def test_x():\n"
            "    with patch('baldur_pro.services.dlq.get_dlq_service'):\n"
            "        pass\n"
        )
        assert len(_scan_module(self._write(tmp_path, src))) == 1

    def test_unguarded_dormant_patch_string_flagged(self, tmp_path: Path):
        src = (
            "from unittest.mock import patch\n"
            "def test_x():\n"
            "    with patch('baldur_dormant.adapters.kafka.producer.get'):\n"
            "        pass\n"
        )
        assert len(_scan_module(self._write(tmp_path, src))) == 1

    def test_unguarded_function_body_import_flagged(self, tmp_path: Path):
        src = (
            "def test_x():\n"
            "    from baldur_pro.services.bulkhead.exceptions import BulkheadFullError\n"
            "    assert BulkheadFullError\n"
        )
        assert len(_scan_module(self._write(tmp_path, src))) == 1

    def test_in_file_importorskip_guards_all_refs(self, tmp_path: Path):
        # Per-file rule: any reachable importorskip marks the file guarded — a
        # sibling guard counts. Per-test precision is delegated to the public repo's native CI.
        src = (
            "import pytest\n"
            "from unittest.mock import patch\n"
            "def test_guarded():\n"
            "    pytest.importorskip('baldur_pro')\n"
            "    with patch('baldur_pro.x'):\n"
            "        pass\n"
            "def test_other():\n"
            "    with patch('baldur_pro.y'):\n"
            "        pass\n"
        )
        assert _scan_module(self._write(tmp_path, src)) == []

    def test_fixture_importorskip_guards_file(self, tmp_path: Path):
        src = (
            "import pytest\n"
            "from unittest.mock import patch\n"
            "@pytest.fixture\n"
            "def pro_thing():\n"
            "    pytest.importorskip('baldur_pro')\n"
            "    from baldur_pro.x import thing\n"
            "    return thing\n"
            "def test_x(pro_thing):\n"
            "    with patch('baldur_pro.x.y'):\n"
            "        pass\n"
        )
        assert _scan_module(self._write(tmp_path, src)) == []

    def test_soft_try_except_import_not_flagged(self, tmp_path: Path):
        src = (
            "def _reset():\n"
            "    try:\n"
            "        from baldur_pro.services.dlq import reset_dlq_service\n"
            "        reset_dlq_service()\n"
            "    except ImportError:\n"
            "        pass\n"
        )
        assert _scan_module(self._write(tmp_path, src)) == []

    def test_oss_patch_and_module_import_not_flagged(self, tmp_path: Path):
        src = (
            "from unittest.mock import patch\n"
            "from baldur.services.x import thing\n"
            "def test_x():\n"
            "    with patch('baldur.services.x.thing'):\n"
            "        pass\n"
        )
        assert _scan_module(self._write(tmp_path, src)) == []

    def test_patch_object_oss_attr_not_flagged(self, tmp_path: Path):
        src = (
            "from unittest.mock import patch\n"
            "def test_x():\n"
            "    with patch.object(SomeClass, 'method'):\n"
            "        pass\n"
        )
        assert _scan_module(self._write(tmp_path, src)) == []

    def test_gated_string_literal_not_a_ref(self, tmp_path: Path):
        # An architecture test carrying a gated module path as TEST DATA (a string
        # literal, not a real patch/import) is not a runtime ref.
        src = (
            "import pytest\n"
            "@pytest.mark.parametrize('s', ['from baldur_pro.x import _y'])\n"
            "def test_scanner(s):\n"
            "    assert 'baldur_pro' in s\n"
        )
        assert _scan_module(self._write(tmp_path, src)) == []

    def test_unparseable_returns_empty(self, tmp_path: Path):
        assert _scan_module(self._write(tmp_path, "def f(:\n")) == []


class TestG50BaselineEnforcedEmpty:
    """The meta-assertion goes red if the enforced-empty baseline key gains an entry."""

    def test_meta_assertion_fails_when_baseline_key_nonempty(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        path = tmp_path / "baseline.yaml"
        path.write_text(
            f"{_RULE_KEY}:\n"
            '  - {file: "tests/unit/leaky.py", reason: "x", ticket: "659"}\n',
            encoding="utf-8",
        )
        monkeypatch.setattr(arch_helpers, "BASELINE_PATH", path)
        arch_helpers._load_baseline_document.cache_clear()
        try:
            with pytest.raises(AssertionError):
                TestOssTestProRuntimeRefs().test_baseline_is_enforced_empty()
        finally:
            arch_helpers._load_baseline_document.cache_clear()


__all__ = [
    "TestG50BaselineEnforcedEmpty",
    "TestG50Scanner",
    "TestOssTestProRuntimeRefs",
]
