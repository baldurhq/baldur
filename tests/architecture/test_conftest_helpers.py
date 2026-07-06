"""Unit tests for shared fitness-function helpers in `_helpers.py`.

Each helper is pure (path/string inputs, return values) and was authored under
impl doc 506. These tests cover the helper contracts/behaviors enumerated in
that doc's ``Test Assessment`` section:

- ``walk_src`` filter variants (TestWalkSrcContract)
- ``parse_ast`` lru_cache + syntax-error fallback (TestParseAstBehavior)
- ``load_baseline`` symbol/count parsing + ``collect_violations`` count-threshold
  matching (TestBaselineBehavior) — impl doc 534
- ``symbol_of`` / ``_symbol_index`` qualname resolution (TestSymbolOfBehavior)
- ``resolve_callsites`` direct / aliased / attribute receivers
  (TestResolveCallsitesBehavior)
- ``optional_extras_modules`` + ``core_dependency_modules`` recursive flatten
  and core subtraction (TestOptionalExtrasContract)
- ``format_violation`` anchor URL composition (TestFormatViolationContract)
- ``resolve_all_chain_files`` own-``__init__`` + ``__all__``-member-file
  resolution, ``src_root`` filter, unimportable/empty skip
  (TestResolveAllChainFilesContract) — impl doc 557
- ``_locate_project_root`` marker-climb + ``OSS_TESTS_ROOT`` layout-agnostic
  resolution under both the monorepo (``tests/architecture/``) and the
  renamed public-mirror (``tests/architecture/``) layouts
  (TestLayoutAgnosticRoots) — impl doc 642 D2
- ``oss_src_root`` find_spec-hit vs in-tree fallback branches + injectability
  (TestOssSrcRoot), ``consumer_src_roots`` 3-root composition tracking
  ``oss_src_root`` (TestConsumerSrcRoots), and ``collect_long_form_flag_reads``
  resolving its ``roots=None`` default lazily through ``consumer_src_roots``
  (TestCollectLongFormFlagReadsRootDefault) — impl doc 664 D8
"""

from __future__ import annotations

import ast
import importlib
import importlib.util
import sys
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tests.architecture import _helpers as arch_helpers
from tests.architecture._helpers import (
    MODULE_SYMBOL,
    RULE_REGISTRY_DOC,
    _symbol_index,
    baselined_count,
    collect_long_form_flag_reads,
    collect_violations,
    core_dependency_modules,
    format_violation,
    load_baseline,
    optional_extras_modules,
    parse_ast,
    resolve_all_chain_files,
    resolve_callsites,
    symbol_of,
    walk_src,
)


@pytest.fixture
def src_tree(tmp_path: Path) -> Path:
    """Build a miniature source tree for walk_src/resolve_callsites tests."""
    root = tmp_path / "src" / "fakepkg"
    (root / "sub").mkdir(parents=True)
    (root / "__init__.py").write_text("", encoding="utf-8")
    (root / "module_a.py").write_text("VALUE = 1\n", encoding="utf-8")
    (root / "_private.py").write_text("VALUE = 2\n", encoding="utf-8")
    (root / "sub" / "__init__.py").write_text("", encoding="utf-8")
    (root / "sub" / "module_b.py").write_text("VALUE = 3\n", encoding="utf-8")
    return root


class TestWalkSrcContract:
    """`walk_src` enumerates `.py` files with the three filter knobs (D2)."""

    def test_default_walk_yields_every_python_file_recursively(self, src_tree: Path):
        # Given: a tree with init files, public modules, and a private helper
        # When: walking with no filters
        files = sorted(p.name for p in walk_src(roots=[src_tree]))
        # Then: every `.py` file is yielded
        assert files == [
            "__init__.py",
            "__init__.py",
            "_private.py",
            "module_a.py",
            "module_b.py",
        ]

    def test_walk_with_exclude_init_skips_init_files_only(self, src_tree: Path):
        # When: filtering out __init__.py
        files = sorted(p.name for p in walk_src(roots=[src_tree], exclude_init=True))
        # Then: __init__.py files are dropped, private and public modules remain
        assert "__init__.py" not in files
        assert "_private.py" in files
        assert "module_a.py" in files

    def test_walk_with_exclude_underscore_skips_underscore_prefixed_modules(
        self, src_tree: Path
    ):
        # When: filtering out _-prefixed modules
        files = sorted(
            p.name for p in walk_src(roots=[src_tree], exclude_underscore=True)
        )
        # Then: private helper is dropped but __init__.py still appears
        # (`exclude_underscore` explicitly does NOT touch __init__.py per docstring)
        assert "_private.py" not in files
        assert "__init__.py" in files
        assert "module_a.py" in files

    def test_walk_with_both_exclusions_yields_only_public_modules(self, src_tree: Path):
        # When: applying both filters (D7 G9 scope)
        files = sorted(
            p.name
            for p in walk_src(
                roots=[src_tree], exclude_underscore=True, exclude_init=True
            )
        )
        # Then: only public, non-init modules survive
        assert files == ["module_a.py", "module_b.py"]

    def test_walk_skips_missing_root_silently(self, tmp_path: Path):
        # Given: a non-existent root path
        missing = tmp_path / "does_not_exist"
        # When/Then: walking yields nothing, no exception
        assert list(walk_src(roots=[missing])) == []

    def test_walk_is_idempotent_across_repeated_calls(self, src_tree: Path):
        # Idempotency: calling walk_src N times with same args yields same set
        run_1 = {p.as_posix() for p in walk_src(roots=[src_tree])}
        run_2 = {p.as_posix() for p in walk_src(roots=[src_tree])}
        run_3 = {p.as_posix() for p in walk_src(roots=[src_tree])}
        assert run_1 == run_2 == run_3


class TestParseAstBehavior:
    """`parse_ast` caches successful parses and returns None on syntax error."""

    def test_parse_returns_module_for_valid_source(self, tmp_path: Path):
        path = tmp_path / "ok.py"
        path.write_text("def foo():\n    return 1\n", encoding="utf-8")
        tree = parse_ast(path)
        assert tree is not None
        assert any(node.name == "foo" for node in tree.body if hasattr(node, "name"))

    def test_parse_returns_none_for_syntax_error(self, tmp_path: Path):
        path = tmp_path / "broken.py"
        path.write_text("def foo(:\n", encoding="utf-8")  # invalid syntax
        assert parse_ast(path) is None

    def test_parse_returns_none_for_missing_file(self, tmp_path: Path):
        # OSError path: the file does not exist
        assert parse_ast(tmp_path / "ghost.py") is None

    def test_parse_is_lru_cached_returning_same_object(self, tmp_path: Path):
        path = tmp_path / "cached.py"
        path.write_text("x = 1\n", encoding="utf-8")
        first = parse_ast(path)
        # Mutating the file on disk SHOULD NOT change cached result —
        # confirms lru_cache hit on second call with same Path key.
        path.write_text("y = 2\n", encoding="utf-8")
        second = parse_ast(path)
        assert first is second


class TestBaselineBehavior:
    """`load_baseline` parses symbol/count; `collect_violations` thresholds (534 D1/D4)."""

    @pytest.fixture
    def baseline_yaml(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> Iterator[Path]:
        """Point _helpers.BASELINE_PATH at a fixture file and clear the cache."""
        path = tmp_path / "baseline.yaml"
        path.write_text(
            """
file_level_rule:
  - {file: "src/baldur/example/whole_file.py", reason: "legacy", ticket: "506"}
symbol_level_rule:
  - {file: "src/baldur/example/pin.py", symbol: "C.method", count: 2, reason: "legacy", ticket: "506"}
  - {file: "src/baldur/example/pin.py", symbol: "lone_func", reason: "legacy", ticket: "506"}
windows_path_rule:
  - {file: "src\\\\baldur\\\\example\\\\winpath.py", reason: "windows", ticket: "506"}
""",
            encoding="utf-8",
        )
        monkeypatch.setattr(arch_helpers, "BASELINE_PATH", path)
        arch_helpers._load_baseline_document.cache_clear()
        yield path
        arch_helpers._load_baseline_document.cache_clear()

    def test_load_baseline_file_level_entry_keys_on_none_symbol(
        self, baseline_yaml: Path
    ):
        # A symbol-less entry is a whole-file waiver, keyed (file, None).
        entries = load_baseline("file_level_rule")
        assert ("src/baldur/example/whole_file.py", None) in entries

    def test_load_baseline_symbol_entry_maps_to_count(self, baseline_yaml: Path):
        entries = load_baseline("symbol_level_rule")
        # Explicit count is preserved; omitted count defaults to 1.
        assert entries[("src/baldur/example/pin.py", "C.method")] == 2
        assert entries[("src/baldur/example/pin.py", "lone_func")] == 1

    def test_load_baseline_normalizes_windows_backslash_to_posix(
        self, baseline_yaml: Path
    ):
        # Windows-authored entries MUST normalize so cross-platform tests match
        entries = load_baseline("windows_path_rule")
        assert ("src/baldur/example/winpath.py", None) in entries

    def test_load_baseline_returns_empty_dict_for_unknown_rule(
        self, baseline_yaml: Path
    ):
        assert load_baseline("not_a_rule") == {}

    def test_load_baseline_returns_empty_when_yaml_file_missing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        missing = tmp_path / "no_baseline.yaml"
        monkeypatch.setattr(arch_helpers, "BASELINE_PATH", missing)
        arch_helpers._load_baseline_document.cache_clear()
        try:
            assert load_baseline("anything") == {}
        finally:
            arch_helpers._load_baseline_document.cache_clear()

    def test_baselined_count_returns_zero_for_absent_pair(self, baseline_yaml: Path):
        baseline = load_baseline("symbol_level_rule")
        assert baselined_count("src/baldur/example/pin.py", "C.method", baseline) == 2
        # A brand-new symbol has baselined count 0 -> regresses on first sight.
        assert baselined_count("src/baldur/example/pin.py", "absent", baseline) == 0

    @pytest.mark.parametrize(
        ("symbol", "observed", "regresses"),
        [
            ("C.method", 1, False),  # count 2, observed 1 -> pass
            ("C.method", 2, False),  # observed == count -> pass (ceiling)
            ("C.method", 3, True),  # observed > count -> regress
            ("lone_func", 1, False),  # default count 1, observed 1 -> pass
            ("lone_func", 2, True),  # observed > 1 -> regress
            ("brand_new", 1, True),  # absent (count 0) -> regress on first
        ],
    )
    def test_collect_violations_count_threshold(
        self,
        baseline_yaml: Path,
        symbol: str,
        observed: int,
        regresses: bool,
    ):
        path = arch_helpers.PROJECT_ROOT / "src" / "baldur" / "example" / "pin.py"
        raw = [(path, i + 1, symbol, "v") for i in range(observed)]
        violations = collect_violations("symbol_level_rule", raw, "#anchor")
        assert bool(violations) is regresses
        # When a symbol regresses, ALL of its live occurrences are listed.
        if regresses:
            assert len(violations) == observed

    def test_collect_violations_whole_file_absorbs_every_symbol(
        self, baseline_yaml: Path
    ):
        path = (
            arch_helpers.PROJECT_ROOT / "src" / "baldur" / "example" / "whole_file.py"
        )
        raw = [(path, i + 1, f"sym{i}", "v") for i in range(5)]
        assert collect_violations("file_level_rule", raw, "#anchor") == []


class TestSymbolOfBehavior:
    """`symbol_of` resolves CPython __qualname__ scopes; `_symbol_index` caches (534 D3)."""

    @staticmethod
    def _find(tree: ast.AST, node_type: type, pred=None) -> ast.AST:
        for node in ast.walk(tree):
            if isinstance(node, node_type) and (pred is None or pred(node)):
                return node
        raise AssertionError(f"no {node_type.__name__} matched")

    def test_symbol_of_module_level_statement_is_module_sentinel(self):
        tree = ast.parse("x = datetime.now()\n")
        call = self._find(tree, ast.Call)
        assert symbol_of(tree, call) == MODULE_SYMBOL

    def test_symbol_of_module_level_def_is_bare_name(self):
        tree = ast.parse("def foo():\n    pass\n")
        fn = self._find(tree, ast.FunctionDef)
        assert symbol_of(tree, fn) == "foo"

    def test_symbol_of_method_is_class_qualified(self):
        tree = ast.parse("class C:\n    def m(self):\n        pass\n")
        fn = self._find(tree, ast.FunctionDef, lambda n: n.name == "m")
        assert symbol_of(tree, fn) == "C.m"

    def test_symbol_of_async_method_is_class_qualified(self):
        # Guards against the AsyncFunctionDef-omission bug (D3): async defs are a
        # DISTINCT node type that MUST open a scope.
        tree = ast.parse("class C:\n    async def m(self):\n        pass\n")
        fn = self._find(tree, ast.AsyncFunctionDef)
        assert symbol_of(tree, fn) == "C.m"

    def test_symbol_of_nested_class_is_dotted(self):
        tree = ast.parse("class Outer:\n    class Inner:\n        pass\n")
        inner = self._find(tree, ast.ClassDef, lambda n: n.name == "Inner")
        assert symbol_of(tree, inner) == "Outer.Inner"

    def test_symbol_of_function_nested_def_uses_locals_segment(self):
        tree = ast.parse("def outer():\n    def inner():\n        pass\n")
        inner = self._find(tree, ast.FunctionDef, lambda n: n.name == "inner")
        assert symbol_of(tree, inner) == "outer.<locals>.inner"

    def test_symbol_of_class_body_statement_has_no_trailing_dot(self):
        # A class-level default (e.g. a class-body call) resolves to ClassName,
        # never "ClassName." with an empty trailing segment.
        tree = ast.parse("class C:\n    x = datetime.now()\n")
        call = self._find(tree, ast.Call)
        assert symbol_of(tree, call) == "C"

    def test_symbol_of_def_returns_own_call_returns_enclosing(self):
        tree = ast.parse("class C:\n    def m(self):\n        print('x')\n")
        fn = self._find(tree, ast.FunctionDef, lambda n: n.name == "m")
        call = self._find(tree, ast.Call)
        assert symbol_of(tree, fn) == "C.m"  # the def node -> its OWN qualname
        assert symbol_of(tree, call) == "C.m"  # a nested node -> ENCLOSING qualname

    def test_symbol_of_module_level_try_is_transparent(self):
        # The hedging fold case: a Call inside a module-level try/except resolves
        # to <module>, not to an intervening construct.
        tree = ast.parse("try:\n    print('x')\nexcept Exception:\n    pass\n")
        call = self._find(tree, ast.Call)
        assert symbol_of(tree, call) == MODULE_SYMBOL

    def test_symbol_of_if_within_method_is_transparent(self):
        tree = ast.parse(
            "class C:\n    def m(self):\n        if True:\n            print('x')\n"
        )
        call = self._find(tree, ast.Call)
        assert symbol_of(tree, call) == "C.m"

    def test_symbol_of_lambda_within_method_is_transparent(self):
        # CPython gives a lambda its own <lambda> code scope, but it is NOT in
        # the opener set, so a violation inside converges to the named method.
        tree = ast.parse("class C:\n    def m(self):\n        f = lambda: print('x')\n")
        call = self._find(
            tree,
            ast.Call,
            lambda n: isinstance(n.func, ast.Name) and n.func.id == "print",
        )
        assert symbol_of(tree, call) == "C.m"

    def test_symbol_of_comprehension_within_method_is_transparent(self):
        tree = ast.parse(
            "class C:\n    def m(self):\n        return [print(i) for i in range(3)]\n"
        )
        call = self._find(
            tree,
            ast.Call,
            lambda n: isinstance(n.func, ast.Name) and n.func.id == "print",
        )
        assert symbol_of(tree, call) == "C.m"

    def test_symbol_of_unindexed_node_is_module_sentinel(self):
        tree = ast.parse("x = 1\n")
        orphan = ast.parse("y = 2\n").body[0]  # node not present in `tree`
        assert symbol_of(tree, orphan) == MODULE_SYMBOL

    def test_symbol_index_is_cached_returning_same_dict(self):
        # Mirrors the parse_ast cache test: same tree object -> same index dict.
        tree = ast.parse("class C:\n    def m(self):\n        pass\n")
        assert _symbol_index(tree) is _symbol_index(tree)


class TestResolveCallsitesBehavior:
    """`resolve_callsites` follows direct, aliased, and attribute call shapes (D5)."""

    @pytest.fixture
    def callsite_tree(self, tmp_path: Path) -> Path:
        root = tmp_path / "src" / "callers"
        root.mkdir(parents=True)
        # Direct call: from x import setup_foo; setup_foo()
        (root / "direct.py").write_text(
            "from baldur.x import setup_foo\nsetup_foo()\n", encoding="utf-8"
        )
        # Aliased call: from x import setup_bar as _bar; _bar()
        (root / "aliased.py").write_text(
            "from baldur.x import setup_bar as _bar\n_bar()\n", encoding="utf-8"
        )
        # Attribute receiver: module.setup_baz()
        (root / "attribute.py").write_text(
            "import baldur.x as mod\nmod.setup_baz()\n", encoding="utf-8"
        )
        # Unrelated file: name appears but is not invoked
        (root / "noop.py").write_text(
            "from baldur.x import setup_quux\n# never called\n", encoding="utf-8"
        )
        return root

    def test_direct_call_is_detected(self, callsite_tree: Path):
        invoked = resolve_callsites([callsite_tree], ["setup_foo"])
        assert invoked == {"setup_foo"}

    def test_aliased_import_resolves_back_to_original_name(self, callsite_tree: Path):
        # `setup_bar as _bar; _bar()` MUST be tracked as `setup_bar` per D5
        invoked = resolve_callsites([callsite_tree], ["setup_bar"])
        assert invoked == {"setup_bar"}

    def test_attribute_call_on_module_alias_is_detected(self, callsite_tree: Path):
        # `mod.setup_baz()` — ast.Attribute with target attr
        invoked = resolve_callsites([callsite_tree], ["setup_baz"])
        assert invoked == {"setup_baz"}

    def test_imported_but_never_called_name_is_not_reported(self, callsite_tree: Path):
        # Imports without an ast.Call: not invoked
        invoked = resolve_callsites([callsite_tree], ["setup_quux"])
        assert invoked == set()

    def test_multi_target_query_returns_only_invoked_subset(self, callsite_tree: Path):
        invoked = resolve_callsites(
            [callsite_tree],
            ["setup_foo", "setup_bar", "setup_baz", "setup_quux", "setup_never"],
        )
        assert invoked == {"setup_foo", "setup_bar", "setup_baz"}


class TestOptionalExtrasContract:
    """`optional_extras_modules` / `core_dependency_modules` resolve pyproject (D6)."""

    @pytest.fixture
    def fake_pyproject(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> Iterator[dict[str, Any]]:
        """Override `_pyproject_data` to a controlled fixture and clear caches."""
        data: dict[str, Any] = {
            "project": {
                "dependencies": [
                    "redis>=4.0",
                    "structlog>=23.0",
                ],
                "optional-dependencies": {
                    "django": [
                        "django>=4.2",
                        "djangorestframework>=3.14",
                    ],
                    "celery": [
                        # `redis` is core — must be subtracted out
                        "celery>=5.3",
                        "redis>=4.0",
                    ],
                    "prometheus": [
                        "prometheus-client>=0.17",
                    ],
                    "ml-deep": [
                        # Recursive extras: pulls in everything from `django`
                        "baldur[django]",
                        "scikit-learn>=1.0",
                    ],
                },
            }
        }

        monkeypatch.setattr(arch_helpers, "_pyproject_data", lambda: data)
        arch_helpers.core_dependency_modules.cache_clear()
        arch_helpers.optional_extras_modules.cache_clear()
        yield data
        arch_helpers.core_dependency_modules.cache_clear()
        arch_helpers.optional_extras_modules.cache_clear()

    def test_core_dependency_modules_contract_includes_listed_packages(
        self, fake_pyproject
    ):
        # Contract: every distribution in [project.dependencies] maps to a module
        assert core_dependency_modules() == frozenset({"redis", "structlog"})

    def test_django_extra_applies_distribution_name_overrides(self, fake_pyproject):
        # Contract: `djangorestframework` → `rest_framework` via D6 override map
        extras = optional_extras_modules()
        assert extras["django"] == frozenset({"django", "rest_framework"})

    def test_celery_extra_subtracts_core_dependencies(self, fake_pyproject):
        # Contract: `redis` is in [project.dependencies], so it must NOT appear in
        # the `celery` extra's module set even though the spec re-lists it
        extras = optional_extras_modules()
        assert extras["celery"] == frozenset({"celery"})
        assert "redis" not in extras["celery"]

    def test_prometheus_distribution_dash_becomes_underscore_in_module(
        self, fake_pyproject
    ):
        # Contract: `prometheus-client` → `prometheus_client` via D6 override
        extras = optional_extras_modules()
        assert extras["prometheus"] == frozenset({"prometheus_client"})

    def test_ml_deep_recursive_extra_pulls_django_modules_plus_sklearn(
        self, fake_pyproject
    ):
        # Contract: `baldur[django]` self-reference is flattened, then merged with
        # sibling specs (scikit-learn → sklearn override)
        extras = optional_extras_modules()
        assert extras["ml-deep"] == frozenset({"django", "rest_framework", "sklearn"})


class TestFormatViolationContract:
    """`format_violation` composes anchor URL and optional context."""

    def test_format_includes_file_line_and_anchor_url(self):
        out = format_violation(
            "#g5-time-handling", "src/baldur/foo.py", 42, "datetime.utcnow"
        )
        # Contract: location, separator, extra, then rule link in brackets
        assert "src/baldur/foo.py:42" in out
        assert "datetime.utcnow" in out
        assert f"{RULE_REGISTRY_DOC}#g5-time-handling" in out

    def test_format_omits_line_when_none(self):
        out = format_violation("#g6-health-check-naming", "src/baldur/x.py", None)
        # `:None` MUST NOT appear; just the bare file path
        assert "src/baldur/x.py" in out
        assert ":None" not in out
        assert "src/baldur/x.py:" not in out

    def test_format_accepts_anchor_without_hash_prefix(self):
        # Contract: anchor without leading '#' MUST be normalized to '#anchor'
        out = format_violation("g11-state-backend-ttl", "src/baldur/x.py", 1)
        assert f"{RULE_REGISTRY_DOC}#g11-state-backend-ttl" in out

    def test_format_handles_pathlib_input_by_normalizing_to_posix(self, tmp_path: Path):
        path = tmp_path / "sub" / "file.py"
        path.parent.mkdir(parents=True)
        path.write_text("", encoding="utf-8")
        out = format_violation("#g14-no-print", path, 7)
        # Path inputs get _to_posix() — backslashes MUST NOT appear on output
        assert "\\" not in out
        assert "file.py:7" in out

    def test_format_skips_em_dash_when_extra_is_none(self):
        out = format_violation("#g9-all-declaration", "src/baldur/x.py", 1)
        # No extra text -> no " — " separator before the link
        assert " — " not in out


class TestResolveAllChainFilesContract:
    """`resolve_all_chain_files` resolves the published-reference source set (557 D4/D5).

    The promoted shared primitive backing G23/G24/G26/G27. It mirrors
    mkdocstrings reachability for a whole-package ``:::`` directive: each
    package contributes its OWN ``__init__`` module file PLUS the defining file
    of every ``__all__`` re-export (via ``obj.__module__``), dropping anything
    outside ``src_root``. Exercised here over a synthetic importable package so
    the contract is verified without depending on any real package's layout.
    """

    @pytest.fixture
    def synthetic_ref_package(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> Iterator[tuple[str, Path]]:
        """Build + import a real package, yield ``(name, resolved_src_root)``.

        Layout (``src_root`` = ``<tmp>/src``)::

            src/synthref/__init__.py   re-exports LocalThing, OrderedDict, ghost
            src/synthref/_impl.py      defines LocalThing

        - ``LocalThing`` defines under ``src_root`` -> its file must be resolved.
        - ``OrderedDict`` (stdlib ``collections``) is re-exported but lives
          OUTSIDE ``src_root`` -> must be dropped by the root filter.
        - ``ghost`` is declared in ``__all__`` but never bound ->
          ``getattr(..., None)`` -> skipped without raising.
        """
        src = tmp_path / "src"
        pkg = src / "synthref"
        pkg.mkdir(parents=True)
        (pkg / "__init__.py").write_text(
            "from collections import OrderedDict\n"
            "from synthref._impl import LocalThing\n"
            '__all__ = ["LocalThing", "OrderedDict", "ghost"]\n',
            encoding="utf-8",
        )
        (pkg / "_impl.py").write_text("class LocalThing:\n    pass\n", encoding="utf-8")
        monkeypatch.syspath_prepend(str(src))
        importlib.invalidate_caches()
        yield "synthref", src.resolve()
        for name in [
            m for m in sys.modules if m == "synthref" or m.startswith("synthref.")
        ]:
            del sys.modules[name]

    def test_resolves_own_init_and_member_defining_files(
        self, synthetic_ref_package: tuple[str, Path]
    ):
        # Given: a package whose __all__ has one in-root member + one stdlib member
        name, src_root = synthetic_ref_package
        # When: resolving the chain
        files = resolve_all_chain_files([name], src_root)
        # Then: exactly the own __init__ and the in-root member's defining file
        rels = {p.relative_to(src_root).as_posix() for p in files}
        assert rels == {"synthref/__init__.py", "synthref/_impl.py"}

    def test_package_own_init_file_is_included(
        self, synthetic_ref_package: tuple[str, Path]
    ):
        # D4: own-__init__ inclusion is load-bearing — a member-only walk never
        # reaches a package whose module docstring defines no __all__ symbol.
        name, src_root = synthetic_ref_package
        files = resolve_all_chain_files([name], src_root)
        assert any(
            p.name == "__init__.py" and p.parent.name == "synthref" for p in files
        )

    def test_out_of_src_root_member_is_dropped(
        self, synthetic_ref_package: tuple[str, Path]
    ):
        # OrderedDict's defining file (stdlib collections) is outside src_root.
        name, src_root = synthetic_ref_package
        files = resolve_all_chain_files([name], src_root)
        # Every resolved file is strictly under src_root — the filter invariant.
        assert files
        assert all(src_root in p.parents for p in files)

    def test_unrelated_src_root_drops_every_file(
        self, synthetic_ref_package: tuple[str, Path], tmp_path: Path
    ):
        # Boundary: when src_root is not an ancestor of the package, both the
        # own-__init__ branch AND the member branch filter out -> empty set.
        name, _ = synthetic_ref_package
        unrelated_root = (tmp_path / "elsewhere").resolve()
        assert resolve_all_chain_files([name], unrelated_root) == set()

    def test_unimportable_package_is_skipped_without_raising(
        self, synthetic_ref_package: tuple[str, Path]
    ):
        # A bogus package name hits the `except Exception: continue` branch; the
        # importable package alongside it still resolves fully.
        name, src_root = synthetic_ref_package
        files = resolve_all_chain_files([name, "no_such_pkg_557_xyz"], src_root)
        rels = {p.relative_to(src_root).as_posix() for p in files}
        assert rels == {"synthref/__init__.py", "synthref/_impl.py"}

    def test_unbound_all_member_is_skipped(
        self, synthetic_ref_package: tuple[str, Path]
    ):
        # "ghost" is in __all__ but unbound -> getattr returns None -> skipped;
        # the result is exactly the two real in-root files, no AttributeError.
        name, src_root = synthetic_ref_package
        files = resolve_all_chain_files([name], src_root)
        assert len(files) == 2

    def test_empty_packages_yields_empty_set(self, tmp_path: Path):
        # Boundary: empty input -> empty set (the gate would fail anti-vacuous).
        assert resolve_all_chain_files([], (tmp_path / "src").resolve()) == set()

    def test_resolution_is_idempotent_across_repeated_calls(
        self, synthetic_ref_package: tuple[str, Path]
    ):
        # Idempotency: N identical calls yield an identical set.
        name, src_root = synthetic_ref_package
        run_1 = resolve_all_chain_files([name], src_root)
        run_2 = resolve_all_chain_files([name], src_root)
        run_3 = resolve_all_chain_files([name], src_root)
        assert run_1 == run_2 == run_3


def _load_helpers_copy(helpers_path: Path, label: str):
    """Import a copy of ``_helpers.py`` from ``helpers_path`` as a fresh module.

    The module-level ``PROJECT_ROOT`` / ``OSS_TESTS_ROOT`` constants are computed
    at import time from the copy's own ``__file__``, so loading a copy planted in
    a synthetic ``tests/``-rooted tree is how the layout-agnostic resolution is
    exercised without DI (per the doc 642 Testability Notes: "imports the helper
    from a copied path"). A unique module name per call avoids cross-parametrize
    ``sys.modules`` reuse.
    """
    module_name = f"baldur_arch_helpers_shim_{label}"
    spec = importlib.util.spec_from_file_location(module_name, helpers_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestLayoutAgnosticRoots:
    """`_locate_project_root` + `OSS_TESTS_ROOT` resolve in both layouts (642 D2).

    The published mirror renames ``tests/`` -> ``tests/`` (``--path-rename``),
    so ``_helpers.py`` moves from ``tests/architecture/`` (root four levels up)
    to ``tests/architecture/`` (root three levels up). The pre-642 code hardcoded
    a fixed ``parents[3]`` PROJECT_ROOT and ``PROJECT_ROOT/"tests"/"oss"`` walk
    root: in the renamed mirror that climbed one level too high (the whole
    ``architecture/`` suite vacuous-passed) and walked a nonexistent dir (G19/20/21
    silently no-op'd). The fix climbs to the ``pyproject.toml`` marker for
    PROJECT_ROOT and derives ``OSS_TESTS_ROOT`` from ``parents[1]``; both must hold
    under the monorepo AND the mirror layout (the gates run in both).
    """

    SOURCE_HELPERS = Path(arch_helpers.__file__).resolve()

    @pytest.fixture(params=["monorepo", "mirror"])
    def shim_layout(
        self, request: pytest.FixtureRequest, tmp_path: Path
    ) -> tuple[str, Path, Path, Path]:
        """Plant a copy of ``_helpers.py`` in a synthetic repo of the given layout.

        Returns ``(layout, repo_root, expected_tests_root, helpers_copy_path)``.
        Both layouts carry a ``pyproject.toml`` marker at the root, a stub
        ``src/baldur`` package, and a ``test_*.py`` under the tests root so the
        walk is non-empty (the SC #6 anti-vacuous check).
        """
        layout: str = request.param
        root = tmp_path / "repo"
        if layout == "monorepo":
            arch_dir = root / "tests" / "oss" / "architecture"
            expected_tests_root = root / "tests" / "oss"
        else:  # mirror — tests/ renamed to tests/
            arch_dir = root / "tests" / "architecture"
            expected_tests_root = root / "tests"
        arch_dir.mkdir(parents=True)
        # pyproject.toml marker — present in both layouts (it ships to the mirror).
        (root / "pyproject.toml").write_text(
            '[project]\nname = "shim"\n', encoding="utf-8"
        )
        # Stub src tree so PROJECT_ROOT/"src"/"baldur" resolves (the whole-suite fix).
        src_baldur = root / "src" / "baldur"
        src_baldur.mkdir(parents=True)
        (src_baldur / "__init__.py").write_text("", encoding="utf-8")
        # A test file under the tests root so the OSS_TESTS_ROOT walk is non-empty.
        unit_dir = expected_tests_root / "unit"
        unit_dir.mkdir(parents=True)
        (unit_dir / "test_sample.py").write_text(
            "def test_x():\n    pass\n", encoding="utf-8"
        )
        helpers_copy = arch_dir / "_helpers.py"
        helpers_copy.write_text(
            self.SOURCE_HELPERS.read_text(encoding="utf-8"), encoding="utf-8"
        )
        return layout, root, expected_tests_root, helpers_copy

    def test_project_root_climbs_to_pyproject_marker_ancestor(
        self, shim_layout: tuple[str, Path, Path, Path]
    ):
        # Given: a synthetic repo with a pyproject.toml marker at its root
        layout, root, _expected_tests_root, helpers_copy = shim_layout
        # When: the copied module computes PROJECT_ROOT at import time
        module = _load_helpers_copy(helpers_copy, layout)
        # Then: PROJECT_ROOT is the marker-bearing root, NOT a fixed parents[N]
        assert module.PROJECT_ROOT == root.resolve()

    def test_oss_tests_root_resolves_to_tests_root_in_both_layouts(
        self, shim_layout: tuple[str, Path, Path, Path]
    ):
        # OSS_TESTS_ROOT == tests/oss (monorepo) / tests (mirror) — parents[1].
        layout, _root, expected_tests_root, helpers_copy = shim_layout
        module = _load_helpers_copy(helpers_copy, layout)
        assert module.OSS_TESTS_ROOT == expected_tests_root.resolve()

    def test_oss_tests_root_exists_and_walk_is_non_empty(
        self, shim_layout: tuple[str, Path, Path, Path]
    ):
        # SC #6 anti-vacuous: the gates walk OSS_TESTS_ROOT — it MUST point at a
        # real, non-empty dir in both layouts (the mirror bug was an empty scan).
        layout, _root, _expected_tests_root, helpers_copy = shim_layout
        module = _load_helpers_copy(helpers_copy, layout)
        assert module.OSS_TESTS_ROOT.exists()
        assert list(module.OSS_TESTS_ROOT.rglob("*.py"))  # non-empty -> not vacuous

    def test_project_root_src_baldur_resolves_in_both_layouts(
        self, shim_layout: tuple[str, Path, Path, Path]
    ):
        # The architecture-wide fix: a misresolved PROJECT_ROOT silently no-ops
        # EVERY gate that reads DEFAULT_SRC_ROOTS. Marker-climb keeps src/baldur
        # reachable under both layouts.
        layout, _root, _expected_tests_root, helpers_copy = shim_layout
        module = _load_helpers_copy(helpers_copy, layout)
        assert (module.PROJECT_ROOT / "src" / "baldur").is_dir()

    def test_mirror_layout_old_hardcoded_walk_root_would_be_vacuous(
        self, shim_layout: tuple[str, Path, Path, Path]
    ):
        # Regression contrast: in the mirror, the pre-642 hardcoded
        # PROJECT_ROOT/"tests"/"oss" walk root does NOT exist (a vacuous pass),
        # while OSS_TESTS_ROOT does. This is exactly the silent no-op the fix kills.
        layout, _root, _expected_tests_root, helpers_copy = shim_layout
        if layout != "mirror":
            pytest.skip("contrast is mirror-specific (monorepo path still exists)")
        module = _load_helpers_copy(helpers_copy, layout)
        old_hardcoded = module.PROJECT_ROOT / "tests" / "oss"
        assert not old_hardcoded.exists()
        assert module.OSS_TESTS_ROOT.exists()
        assert module.OSS_TESTS_ROOT != old_hardcoded


class TestOssSrcRoot:
    """`oss_src_root` resolves via find_spec, with an in-tree fallback (664 D8).

    The OSS source root is located through ``importlib.util.find_spec("baldur")``
    so the consumer-reachability scan finds ``baldur`` whether it is in-tree
    (``src/baldur``) or an installed dependency (the private-repo case, where OSS
    is a pip dependency). The fallback branch (no spec / no search locations)
    never runs in the monorepo — where ``baldur`` is always importable — so it is
    exercised here by patching ``find_spec``. The resolver is intentionally
    patchable so a test can point the scan at a fixture tree without a real
    install.
    """

    @staticmethod
    def _patch_find_spec(monkeypatch: pytest.MonkeyPatch, value: Any) -> None:
        """Patch the ``importlib.util.find_spec`` _helpers calls to return ``value``."""
        monkeypatch.setattr(importlib.util, "find_spec", lambda *a, **k: value)

    def test_returns_spec_search_location_when_baldur_found(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        # Given: find_spec resolves baldur to a package with a search location
        fake_root = tmp_path / "site-packages" / "baldur"
        fake_root.mkdir(parents=True)
        self._patch_find_spec(
            monkeypatch, SimpleNamespace(submodule_search_locations=[str(fake_root)])
        )
        # When/Then: the resolved (absolute) search location is returned
        assert arch_helpers.oss_src_root() == fake_root.resolve()

    def test_returns_first_search_location_when_namespace_package(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        # A namespace package exposes multiple search locations; the first wins
        # (`next(iter(...))`).
        first = tmp_path / "a" / "baldur"
        second = tmp_path / "b" / "baldur"
        first.mkdir(parents=True)
        second.mkdir(parents=True)
        self._patch_find_spec(
            monkeypatch,
            SimpleNamespace(submodule_search_locations=[str(first), str(second)]),
        )
        assert arch_helpers.oss_src_root() == first.resolve()

    def test_falls_back_to_project_root_when_no_spec(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        # find_spec returns None (package not importable) -> in-tree fallback
        self._patch_find_spec(monkeypatch, None)
        assert (
            arch_helpers.oss_src_root() == arch_helpers.PROJECT_ROOT / "src" / "baldur"
        )

    def test_falls_back_when_spec_has_no_search_locations(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        # A non-package module spec has submodule_search_locations=None -> fallback
        self._patch_find_spec(
            monkeypatch, SimpleNamespace(submodule_search_locations=None)
        )
        assert (
            arch_helpers.oss_src_root() == arch_helpers.PROJECT_ROOT / "src" / "baldur"
        )

    def test_falls_back_when_spec_has_empty_search_locations(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        # An empty location list is falsy -> the `and spec.submodule_search_locations`
        # guard short-circuits to the fallback.
        self._patch_find_spec(
            monkeypatch, SimpleNamespace(submodule_search_locations=[])
        )
        assert (
            arch_helpers.oss_src_root() == arch_helpers.PROJECT_ROOT / "src" / "baldur"
        )

    def test_falls_back_when_find_spec_raises_value_error(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        # find_spec raises ValueError (e.g. a module with a missing __spec__) ->
        # caught by the `except (ImportError, ValueError)` arm -> fallback.
        def _raise(*a: Any, **k: Any):
            raise ValueError("no __spec__")

        monkeypatch.setattr(importlib.util, "find_spec", _raise)
        assert (
            arch_helpers.oss_src_root() == arch_helpers.PROJECT_ROOT / "src" / "baldur"
        )

    def test_falls_back_when_find_spec_raises_import_error(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        # A parent-package ImportError (ModuleNotFoundError) is caught -> fallback
        def _raise(*a: Any, **k: Any):
            raise ModuleNotFoundError("no parent package")

        monkeypatch.setattr(importlib.util, "find_spec", _raise)
        assert (
            arch_helpers.oss_src_root() == arch_helpers.PROJECT_ROOT / "src" / "baldur"
        )

    def test_live_resolution_points_at_real_baldur_package(self):
        # Un-patched: in the monorepo `baldur` is importable, so the find_spec-hit
        # branch returns the real package dir (named `baldur`, holding __init__.py).
        root = arch_helpers.oss_src_root()
        assert root.name == "baldur"
        assert (root / "__init__.py").is_file()

    def test_result_is_absolute(self):
        # The spec branch applies `.resolve()`; the fallback is PROJECT_ROOT-rooted
        # (itself absolute). Either way the result is an absolute path.
        assert arch_helpers.oss_src_root().is_absolute()


class TestConsumerSrcRoots:
    """`consumer_src_roots` = OSS root (via `oss_src_root`) + both private tiers (664 D8)."""

    def test_returns_three_roots(self):
        # OSS + baldur_pro + baldur_dormant — the mandatory cross-tier scan set.
        assert len(arch_helpers.consumer_src_roots()) == 3

    def test_first_root_is_oss_src_root(self):
        # The OSS root tracks `oss_src_root()` so the scan finds baldur whether
        # in-tree or installed.
        assert arch_helpers.consumer_src_roots()[0] == arch_helpers.oss_src_root()

    def test_private_roots_are_project_root_relative(self):
        roots = arch_helpers.consumer_src_roots()
        assert roots[1] == arch_helpers.PROJECT_ROOT / "src" / "baldur_pro"
        assert roots[2] == arch_helpers.PROJECT_ROOT / "src" / "baldur_dormant"

    def test_patching_oss_src_root_redirects_first_root_only(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        # Injectability: patching `oss_src_root` redirects the OSS scan root while
        # the two private roots stay PROJECT_ROOT-relative — the documented
        # contract `collect_long_form_flag_reads` relies on.
        fake_oss = tmp_path / "installed" / "baldur"
        fake_oss.mkdir(parents=True)
        monkeypatch.setattr(arch_helpers, "oss_src_root", lambda: fake_oss)
        roots = arch_helpers.consumer_src_roots()
        assert roots[0] == fake_oss
        assert roots[1] == arch_helpers.PROJECT_ROOT / "src" / "baldur_pro"
        assert roots[2] == arch_helpers.PROJECT_ROOT / "src" / "baldur_dormant"


class TestCollectLongFormFlagReadsRootDefault:
    """`collect_long_form_flag_reads` resolves `roots=None` lazily via `consumer_src_roots` (664 D8).

    The 575 default was a module-level constant; 664 D8 changed it to a lazy
    ``consumer_src_roots()`` call so a test patching the resolver redirects the
    scan. ``synthprobe_enabled`` is a synthetic flag name absent from real source,
    so the fixture's read is the only one in play.
    """

    @pytest.fixture
    def oss_fixture_root(self, tmp_path: Path) -> Path:
        root = tmp_path / "baldur"
        root.mkdir()
        (root / "svc.py").write_text(
            "def f(s):\n    if s.synthprobe_enabled:\n        return 1\n",
            encoding="utf-8",
        )
        return root

    def test_default_roots_resolved_lazily_via_consumer_src_roots(
        self, monkeypatch: pytest.MonkeyPatch, oss_fixture_root: Path
    ):
        # Given: consumer_src_roots redirected to a one-file fixture tree
        monkeypatch.setattr(
            arch_helpers, "consumer_src_roots", lambda: (oss_fixture_root,)
        )
        # When: scanning with roots omitted (-> None -> consumer_src_roots())
        result = collect_long_form_flag_reads({"synthprobe_enabled"})
        # Then: the fixture's gate read is found via the lazy default
        assert result == {"synthprobe_enabled": {"gate"}}

    def test_explicit_roots_bypass_consumer_src_roots(
        self, monkeypatch: pytest.MonkeyPatch, oss_fixture_root: Path
    ):
        # An explicit `roots` argument must NOT consult the lazy default.
        def _must_not_call() -> tuple[Path, ...]:
            raise AssertionError("consumer_src_roots() called despite explicit roots")

        monkeypatch.setattr(arch_helpers, "consumer_src_roots", _must_not_call)
        result = collect_long_form_flag_reads(
            {"synthprobe_enabled"}, roots=[oss_fixture_root]
        )
        assert result == {"synthprobe_enabled": {"gate"}}


__all__ = [
    "TestBaselineBehavior",
    "TestCollectLongFormFlagReadsRootDefault",
    "TestConsumerSrcRoots",
    "TestFormatViolationContract",
    "TestLayoutAgnosticRoots",
    "TestOptionalExtrasContract",
    "TestOssSrcRoot",
    "TestParseAstBehavior",
    "TestResolveAllChainFilesContract",
    "TestResolveCallsitesBehavior",
    "TestSymbolOfBehavior",
    "TestWalkSrcContract",
]
