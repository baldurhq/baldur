"""Scanner-layer tests for the clone-recurrence counter (791).

``_clone_scan`` decides, for the whole tree, which function bodies are copies
of each other. G87 then ratchets the member total of at-threshold families
against an inline budget — which means a normalizer that quietly stops seeing
a difference (or starts seeing one that is not there) moves the total in a
direction the exact-match gate cannot tell apart from a real refactor. The
counting rules are pinned here instead, each on a synthetic pair of bodies
that differ in exactly one place, so every test fails for its own reason.

Contract classes assert the design's literal values: the token-stream shape
(one token per node plus one closer), the floor and threshold, the reference
token counts the floor was placed between, and the family / verdict text.
Behavior classes drive the alpha-renaming, the constant collapse, the
docstring skip, the fail-closed walk and the per-root scan against the source
constants.
"""

from __future__ import annotations

import ast
import os
import textwrap
from pathlib import Path

import pytest

from tests.architecture._clone_scan import (
    CLONE_TOKEN_FLOOR,
    RECURRENCE_THRESHOLD,
    CloneMember,
    CloneScan,
    cluster_functions,
    format_family,
    normalized_body_tokens,
    scan_roots,
    walk_python_files,
)
from tests.architecture.test_clone_recurrence_ratchet import (
    clone_budget_verdict,
    fail_closed_reasons,
)

# -- Reference shapes ------------------------------------------------------------
#
# The three bodies the scanner module cites as its measured reference points:
# the crash-capture wrapper that founded the rule, the service-singleton
# getter the mandated pair ships, and a settings getter that must stay below
# the floor.
_CRASH_CAPTURE = """\
class Worker:
    def _{name}_loop_with_crash_capture(self) -> None:
        try:
            self._{name}_loop()
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException as e:
            if self._{handle} is not None:
                self._{handle}.record_crash(e)
            raise
"""

_SERVICE_GETTER = """\
def get_{name}() -> object:
    global _{name}
    if _{name} is None:
        with _{name}_lock:
            if _{name} is None:
                _{name} = object()
    return _{name}
"""

_SETTINGS_GETTER = '''\
def get_{name}_settings() -> object:
    """Get the cached settings instance."""
    from baldur.runtime import get_runtime

    return get_runtime().get_settings(object)
'''


def _function(
    source: str, name: str | None = None
) -> ast.FunctionDef | ast.AsyncFunctionDef:
    """The named (or outermost) function definition in ``source``."""
    tree = ast.parse(textwrap.dedent(source))
    functions = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and (name is None or node.name == name)
    ]
    assert functions, f"no function {name or '<any>'} in source"
    return functions[0]


def _stream(source: str, name: str | None = None) -> tuple[str, ...]:
    return normalized_body_tokens(_function(source, name))


def _body_of(*statements: str) -> str:
    """A ``def f`` whose body is the given statements, one per line."""
    return "def f(self):\n" + "".join(f"    {line}\n" for line in statements)


def _write_tree(base: Path, files: dict[str, str]) -> Path:
    """Write ``files`` (relative POSIX paths -> source) under ``base``."""
    base.mkdir(parents=True, exist_ok=True)
    for name, source in files.items():
        target = base / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source, encoding="utf-8")
    return base


def _member(
    root: str = "pkg",
    file: str = "a.py",
    line: int = 1,
    qualname: str = "f",
    tokens: int = CLONE_TOKEN_FLOOR,
) -> CloneMember:
    return CloneMember(root, file, line, qualname, tokens)


def _deny_listing(monkeypatch: pytest.MonkeyPatch, denied: Path, error: OSError):
    """Make ``os.scandir`` raise ``error`` for exactly ``denied``."""
    real_scandir = os.scandir

    def denying_scandir(path=".", *args):
        if Path(path) == denied:
            raise error
        return real_scandir(path, *args)

    monkeypatch.setattr(os, "scandir", denying_scandir)


class _ReversedListing:
    """A ``scandir`` result whose entries come back in reverse name order.

    NTFS hands ``os.scandir`` its entries already sorted, so on the authoring
    platform a walk that forgot to sort would still look sorted; this wrapper
    makes the native order adversarial on every platform.
    """

    def __init__(self, listing):
        with listing:
            self._entries = sorted(listing, key=lambda e: e.name, reverse=True)

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return None

    def __iter__(self):
        return self

    def __next__(self):
        if not self._entries:
            raise StopIteration
        return self._entries.pop(0)


def _reverse_native_order(monkeypatch: pytest.MonkeyPatch) -> None:
    real_scandir = os.scandir
    monkeypatch.setattr(
        os,
        "scandir",
        lambda path=".", *args: _ReversedListing(real_scandir(path, *args)),
    )


# -- normalized_body_tokens ------------------------------------------------------


class TestNormalizedBodyTokensBehavior:
    """Alpha-renaming, constant collapse and docstring skip, one site per case."""

    @pytest.mark.parametrize(
        ("left", "right"),
        [
            (_body_of("return alpha"), _body_of("return beta")),
            (_body_of("return self.alpha"), _body_of("return self.beta")),
            (
                _body_of("return lambda alpha: alpha"),
                _body_of("return lambda beta: beta"),
            ),
            (_body_of("return f(alpha=1)"), _body_of("return f(beta=1)")),
            (
                _body_of("def alpha():", "    pass", "return alpha"),
                _body_of("def beta():", "    pass", "return beta"),
            ),
            (
                _body_of("async def alpha():", "    pass", "return alpha"),
                _body_of("async def beta():", "    pass", "return beta"),
            ),
            (
                _body_of("class Alpha:", "    pass", "return Alpha"),
                _body_of("class Beta:", "    pass", "return Beta"),
            ),
            (
                _body_of("try:", "    pass", "except E as alpha:", "    raise"),
                _body_of("try:", "    pass", "except E as beta:", "    raise"),
            ),
            (_body_of("from alpha import x"), _body_of("from beta import x")),
            (_body_of("from m import alpha"), _body_of("from m import beta")),
            (_body_of("import m as alpha"), _body_of("import m as beta")),
            (_body_of("global alpha"), _body_of("global beta")),
            (_body_of("nonlocal alpha"), _body_of("nonlocal beta")),
            (_body_of("return f(**alpha)"), _body_of("return f(**beta)")),
        ],
        ids=[
            "name",
            "attribute",
            "arg",
            "keyword",
            "nested_def",
            "nested_async_def",
            "nested_class",
            "except_handler",
            "import_from_module",
            "alias_name",
            "alias_asname",
            "global",
            "nonlocal",
            "double_star_keyword",
        ],
    )
    def test_normalized_body_tokens_renames_identifier_at_each_site(
        self, left: str, right: str
    ):
        """Two bodies that differ only in one identifier normalize to one stream."""
        assert _stream(left) == _stream(right)

    def test_normalized_body_tokens_renames_in_first_seen_order(self):
        """The rename is a bijection over first sight, not a wildcard collapse."""
        pattern = _stream(_body_of("return alpha + beta + alpha"))

        assert pattern == _stream(_body_of("return beta + alpha + beta"))
        assert pattern != _stream(_body_of("return alpha + beta + beta"))

    def test_normalized_body_tokens_double_star_keyword_differs_from_named(self):
        """``f(**kw)`` (keyword arg None) is not the same token as ``f(x=kw)``."""
        assert _stream(_body_of("return f(**kw)")) != _stream(
            _body_of("return f(x=kw)")
        )

    def test_normalized_body_tokens_except_handler_without_name_differs(self):
        """``except E:`` and ``except E as e:`` are different shapes."""
        bare = _body_of("try:", "    pass", "except E:", "    raise")
        named = _body_of("try:", "    pass", "except E as e:", "    raise")

        assert _stream(bare) != _stream(named)

    @pytest.mark.parametrize(
        ("left", "right"),
        [
            (_body_of('return "alpha"'), _body_of('return "beta"')),
            (_body_of('return b"alpha"'), _body_of('return b"beta"')),
            (_body_of('return f"{x}-alpha"'), _body_of('return f"{x}-beta"')),
        ],
        ids=["str", "bytes", "fstring_parts"],
    )
    def test_normalized_body_tokens_collapses_string_constants_to_type(
        self, left: str, right: str
    ):
        """A differing str / bytes literal is not a difference between copies."""
        assert _stream(left) == _stream(right)

    @pytest.mark.parametrize(
        ("left", "right"),
        [
            (_body_of('return "a"'), _body_of('return b"a"')),
            (_body_of("return 1"), _body_of("return 2")),
            (_body_of("return 1.0"), _body_of("return 2.0")),
            (_body_of("return True"), _body_of("return False")),
            (_body_of("return None"), _body_of("return 0")),
            (_body_of("return 1"), _body_of("return 1.0")),
            (_body_of("return 1"), _body_of("return True")),
            (_body_of("return ..."), _body_of("return None")),
        ],
        ids=[
            "str_vs_bytes",
            "int",
            "float",
            "bool",
            "none_vs_zero",
            "int_vs_float",
            "int_vs_bool",
            "ellipsis_vs_none",
        ],
    )
    def test_normalized_body_tokens_keeps_scalar_constants_by_repr(
        self, left: str, right: str
    ):
        """Numbers, booleans, None and Ellipsis stay distinct — and typed."""
        assert _stream(left) != _stream(right)

    @pytest.mark.parametrize(
        ("with_docstring", "without"),
        [
            (
                _body_of('"""Doc."""', "return 1"),
                _body_of("return 1"),
            ),
            (
                _body_of("def g():", '    """Doc."""', "    return 1", "return g"),
                _body_of("def g():", "    return 1", "return g"),
            ),
            (
                _body_of(
                    "async def g():", '    """Doc."""', "    return 1", "return g"
                ),
                _body_of("async def g():", "    return 1", "return g"),
            ),
            (
                _body_of("class C:", '    """Doc."""', "    x = 1", "return C"),
                _body_of("class C:", "    x = 1", "return C"),
            ),
        ],
        ids=["top_level", "nested_def", "nested_async_def", "nested_class"],
    )
    def test_normalized_body_tokens_skips_leading_docstring(
        self, with_docstring: str, without: str
    ):
        """A leading docstring in a def / async def / class body emits nothing."""
        assert _stream(with_docstring) == _stream(without)

    @pytest.mark.parametrize(
        ("with_string", "without"),
        [
            (
                _body_of("x = 1", '"""Not a docstring."""', "return x"),
                _body_of("x = 1", "return x"),
            ),
            (
                _body_of("if x:", '    """Not a docstring."""', "    return 1"),
                _body_of("if x:", "    return 1"),
            ),
            (
                _body_of('b"""Bytes are not a docstring."""', "return 1"),
                _body_of("return 1"),
            ),
        ],
        ids=["non_leading", "if_body", "leading_bytes"],
    )
    def test_normalized_body_tokens_keeps_string_expression_that_is_not_a_docstring(
        self, with_string: str, without: str
    ):
        """Only a leading str expression in a scope body is a docstring."""
        assert _stream(with_string) != _stream(without)

    def test_normalized_body_tokens_docstring_only_body_is_empty_stream(self):
        assert _stream(_body_of('"""Only a docstring."""')) == ()


class TestNormalizedBodyTokensContract:
    """The stream shape and the design's measured reference points."""

    def test_floor_and_threshold_design_values(self):
        """Floor 40 normalized tokens; the rule of three."""
        assert CLONE_TOKEN_FLOOR == 40
        assert RECURRENCE_THRESHOLD == 3

    def test_normalized_body_tokens_emits_one_token_per_node_plus_closer(self):
        """``return x`` is three nodes (Return, Name, Load) — six tokens."""
        assert _stream(_body_of("return x")) == (
            "Return",
            "Name:v0",
            "Load",
            ")",
            ")",
            ")",
        )

    def test_normalized_body_tokens_crash_capture_body_is_74_tokens(self):
        """The founding shape: 37 AST nodes, 74 tokens (the unit is tokens)."""
        func = _function(_CRASH_CAPTURE.format(name="update", handle="handle"))
        nodes = sum(1 for statement in func.body for _ in ast.walk(statement))

        assert nodes == 37
        assert len(normalized_body_tokens(func)) == 74

    def test_normalized_body_tokens_service_getter_is_52_tokens(self):
        """The mandated singleton getter sits above the floor — it is counted."""
        assert len(_stream(_SERVICE_GETTER.format(name="alpha"))) == 52

    def test_normalized_body_tokens_settings_getter_is_22_tokens(self):
        """A settings getter sits below the floor — it stays out."""
        assert len(_stream(_SETTINGS_GETTER.format(name="alpha"))) == 22


# -- walk_python_files -----------------------------------------------------------


class TestWalkPythonFilesBehavior:
    """The fail-closed walk: what is listed, in what order, and what is reported."""

    def test_walk_python_files_prunes_pycache(self, tmp_path):
        root = _write_tree(
            tmp_path / "root",
            {"a.py": "", "__pycache__/a.cpython-312.py": ""},
        )

        files, unlistable = walk_python_files(root)

        assert files == [root / "a.py"]
        assert unlistable == []

    def test_walk_python_files_skips_non_py_files(self, tmp_path):
        root = _write_tree(
            tmp_path / "root",
            {"a.py": "", "b.pyi": "", "c.txt": "", "d.py.bak": ""},
        )

        files, _ = walk_python_files(root)

        assert files == [root / "a.py"]

    def test_walk_python_files_orders_parent_files_then_sorted_subdirectories(
        self, tmp_path, monkeypatch
    ):
        """Per directory: files sorted, then subdirectories in sorted order.

        The native listing order is reversed for the test so the sort is
        what produces the order, not the filesystem.
        """
        root = _write_tree(
            tmp_path / "root",
            {"b/y.py": "", "b/x.py": "", "a/z.py": "", "top.py": ""},
        )
        _reverse_native_order(monkeypatch)

        files, _ = walk_python_files(root)

        assert files == [
            root / "top.py",
            root / "a" / "z.py",
            root / "b" / "x.py",
            root / "b" / "y.py",
        ]

    def test_walk_python_files_records_unlistable_subdirectory_and_continues(
        self, tmp_path, monkeypatch
    ):
        """One denied subdirectory is reported; its listable sibling is still walked."""
        root = _write_tree(tmp_path / "root", {"open/a.py": "", "locked/b.py": ""})
        denied = root / "locked"
        _deny_listing(
            monkeypatch, denied, PermissionError(13, "listing denied", str(denied))
        )

        files, unlistable = walk_python_files(root)

        assert files == [root / "open" / "a.py"]
        assert unlistable == [str(denied)]

    def test_walk_python_files_unlistable_root_yields_no_files_and_one_entry(
        self, tmp_path, monkeypatch
    ):
        """A denied root records the root itself, even when the error names no file."""
        root = _write_tree(tmp_path / "root", {"a.py": ""})
        _deny_listing(monkeypatch, root, PermissionError("listing denied"))

        files, unlistable = walk_python_files(root)

        assert files == []
        assert unlistable == [str(root)]

    def test_walk_python_files_missing_root_is_reported_not_empty(self, tmp_path):
        """A root that does not exist is an unlistable directory, not zero files."""
        missing = tmp_path / "missing"

        files, unlistable = walk_python_files(missing)

        assert files == []
        assert unlistable == [str(missing)]


# -- cluster_functions -----------------------------------------------------------


class TestClusterFunctionsContract:
    """Identical streams group; the rule of three decides what is a family."""

    @pytest.mark.parametrize(
        ("count", "threshold", "expected_families"),
        [
            (2, None, 0),
            (3, None, 1),
            (2, 2, 1),
            (3, 4, 0),
        ],
        ids=["two_default", "three_default", "two_at_threshold_2", "three_below_4"],
    )
    def test_cluster_functions_threshold_boundary(
        self, count: int, threshold: int | None, expected_families: int
    ):
        """Below the threshold no family forms; at it, one does."""
        stream = ("Pass", ")")
        members = [(_member(file=f"{i}.py"), stream) for i in range(count)]
        kwargs = {} if threshold is None else {"threshold": threshold}

        families = cluster_functions(members, **kwargs)

        assert len(families) == expected_families
        if expected_families:
            assert families[0] == tuple(member for member, _ in members)

    def test_cluster_functions_one_token_difference_splits_the_cluster(self):
        members = [
            (_member(file="a.py"), ("Pass", ")")),
            (_member(file="b.py"), ("Pass", ")")),
            (_member(file="c.py"), ("Return", ")")),
        ]

        assert cluster_functions(members) == ()

    def test_cluster_functions_groups_identical_streams_across_roots(self):
        """Roots are one population: two copies in one root plus one in another."""
        stream = ("Pass", ")")
        members = [
            (_member(root="alpha", file="a.py"), stream),
            (_member(root="alpha", file="b.py"), stream),
            (_member(root="beta", file="c.py"), stream),
        ]

        families = cluster_functions(members)

        assert families == ((members[0][0], members[1][0], members[2][0]),)

    def test_cluster_functions_orders_families_by_size_then_first_member(self):
        """Smallest first, ties broken by the first member — not insertion order."""
        four = [(_member(file=f"x{i}.py"), ("A", ")")) for i in range(4)]
        three_late = [(_member(file=f"y{i}.py"), ("B", ")")) for i in range(3)]
        three_early = [(_member(file=f"w{i}.py"), ("C", ")")) for i in range(3)]

        families = cluster_functions([*four, *three_late, *three_early])

        assert [family[0].file for family in families] == ["w0.py", "y0.py", "x0.py"]
        assert [len(family) for family in families] == [3, 3, 4]


# -- scan_roots ------------------------------------------------------------------


class TestScanRootsBehavior:
    """Floor, nesting, per-root attribution, path form and qualname of a member."""

    @pytest.mark.parametrize(
        ("floor_offset", "expected_members"),
        [(-1, 1), (0, 1), (1, 0)],
        ids=["floor_one_below_length", "floor_at_length", "floor_one_above_length"],
    )
    def test_scan_roots_floor_is_inclusive_at_exact_length(
        self, tmp_path, floor_offset: int, expected_members: int
    ):
        """A body of exactly ``floor`` tokens takes part; one token short does not."""
        # Given — a body of exactly CLONE_TOKEN_FLOOR tokens (``pass`` is two)
        source = _body_of(*["pass"] * (CLONE_TOKEN_FLOOR // 2))
        assert len(_stream(source)) == CLONE_TOKEN_FLOOR
        pkg = _write_tree(tmp_path / "pkg", {"a.py": source})

        # When — every body is its own family so the floor is the only filter
        scan = scan_roots(
            {"pkg": pkg},
            relative_to=tmp_path,
            floor=CLONE_TOKEN_FLOOR + floor_offset,
            threshold=1,
        )

        # Then
        assert scan.members_per_root().get("pkg", 0) == expected_members

    @pytest.mark.parametrize(
        ("pass_count", "expected_members"),
        [(CLONE_TOKEN_FLOOR // 2, 1), (CLONE_TOKEN_FLOOR // 2 - 1, 0)],
        ids=["at_default_floor", "below_default_floor"],
    )
    def test_scan_roots_default_floor_excludes_the_body_just_under_it(
        self, tmp_path, pass_count: int, expected_members: int
    ):
        """Streams are always even-length, so the step below the floor is two tokens."""
        pkg = _write_tree(tmp_path / "pkg", {"a.py": _body_of(*["pass"] * pass_count)})

        scan = scan_roots({"pkg": pkg}, relative_to=tmp_path, threshold=1)

        assert scan.members_per_root().get("pkg", 0) == expected_members

    def test_scan_roots_counts_nested_defs_as_their_own_members(self, tmp_path):
        """A nested def above the floor is a member with a ``<locals>`` qualname."""
        inner = ["    pass"] * (CLONE_TOKEN_FLOOR // 2)
        source = _body_of("def inner():", *inner, "return inner")
        pkg = _write_tree(tmp_path / "pkg", {"a.py": source})

        scan = scan_roots({"pkg": pkg}, relative_to=tmp_path, threshold=1)

        qualnames = sorted(
            member.qualname for family in scan.families for member in family
        )
        assert qualnames == ["f", "f.<locals>.inner"]

    def test_scan_roots_attributes_each_member_to_its_root(self, tmp_path):
        """One family across two roots; the per-root totals follow the directories."""
        alpha = _write_tree(
            tmp_path / "alpha",
            {
                "a.py": _CRASH_CAPTURE.format(name="update", handle="handle"),
                "b.py": _CRASH_CAPTURE.format(name="refresh", handle="sender"),
            },
        )
        beta = _write_tree(
            tmp_path / "beta",
            {"c.py": _CRASH_CAPTURE.format(name="writer", handle="health")},
        )

        scan = scan_roots({"alpha": alpha, "beta": beta}, relative_to=tmp_path)

        assert len(scan.families) == 1
        assert scan.members_per_root() == {"alpha": 2, "beta": 1}
        assert [member.root for member in scan.families[0]] == [
            "alpha",
            "alpha",
            "beta",
        ]

    def test_scan_roots_member_fields_use_relative_posix_path_and_qualname(
        self, tmp_path
    ):
        """``file`` is checkout-relative POSIX, ``line`` the def line, qualname via symbol_of."""
        source = _CRASH_CAPTURE.format(name="update", handle="handle")
        pkg = _write_tree(tmp_path / "pkg", {"sub/a.py": source})

        scan = scan_roots({"pkg": pkg}, relative_to=tmp_path, threshold=1)

        assert scan.families == (
            (
                CloneMember(
                    "pkg",
                    "pkg/sub/a.py",
                    2,
                    "Worker._update_loop_with_crash_capture",
                    len(_stream(source)),
                ),
            ),
        )

    def test_scan_roots_unparsed_names_relative_posix_path(self, tmp_path):
        """A file the parser rejects is reported by its checkout-relative POSIX path."""
        pkg = _write_tree(
            tmp_path / "pkg",
            {"sub/broken.py": "def not_python(:\n    pass\n", "ok.py": ""},
        )

        scan = scan_roots({"pkg": pkg}, relative_to=tmp_path)

        assert scan.unparsed == ("pkg/sub/broken.py",)
        assert scan.unlistable == ()


# -- CloneScan.members_per_root ---------------------------------------------------


class TestCloneScanContract:
    """The per-root total the gate compares against its budget."""

    def test_members_per_root_empty_scan_returns_empty_dict(self):
        assert (
            CloneScan(families=(), unparsed=(), unlistable=()).members_per_root() == {}
        )

    def test_members_per_root_omits_roots_without_a_family(self):
        """A root with no at-threshold member has no key (the gate reads it as 0)."""
        family = tuple(_member(root="pkg", file=f"{i}.py") for i in range(3))

        totals = CloneScan(
            families=(family,), unparsed=(), unlistable=()
        ).members_per_root()

        assert totals == {"pkg": 3}
        assert "other" not in totals

    def test_members_per_root_sums_members_across_families(self):
        own = tuple(_member(root="alpha", file=f"a{i}.py") for i in range(3))
        shared = (
            _member(root="alpha", file="s0.py"),
            _member(root="alpha", file="s1.py"),
            _member(root="beta", file="s2.py"),
        )

        totals = CloneScan(
            families=(own, shared), unparsed=(), unlistable=()
        ).members_per_root()

        assert totals == {"alpha": 5, "beta": 1}


# -- clone_budget_verdict / fail_closed_reasons / format_family --------------------


class TestCloneBudgetVerdictContract:
    """Exact match passes; either direction fails with its own instruction."""

    @pytest.mark.parametrize(
        ("actual", "expected_phrase"),
        [
            (4, "clone mass shrank (4 < budget 5) — lower the budget to 4"),
            (5, None),
            (6, "clone mass grew (6 > budget 5)"),
        ],
        ids=["one_below", "exact", "one_above"],
    )
    def test_clone_budget_verdict_exact_match_boundary(
        self, actual: int, expected_phrase: str | None
    ):
        verdict = clone_budget_verdict(actual, 5, root="pkg", families=())

        if expected_phrase is None:
            assert verdict is None
        else:
            assert verdict is not None
            assert verdict.startswith("pkg: ")
            assert expected_phrase in verdict

    def test_clone_budget_verdict_above_budget_without_families_still_fails(self):
        verdict = clone_budget_verdict(1, 0, root="pkg", families=())

        assert verdict is not None
        assert "raise the budget with a justification comment" in verdict

    def test_clone_budget_verdict_lists_only_families_touching_the_root(self):
        """A family entirely in another root is not in this root's listing."""
        own = tuple(_member(root="alpha", file=f"own{i}.py") for i in range(3))
        foreign = tuple(_member(root="beta", file=f"foreign{i}.py") for i in range(3))
        shared = (
            _member(root="beta", file="shared0.py"),
            _member(root="beta", file="shared1.py"),
            _member(root="alpha", file="shared2.py"),
        )

        verdict = clone_budget_verdict(
            4, 0, root="alpha", families=(own, foreign, shared)
        )

        assert verdict is not None
        assert "own0.py" in verdict
        assert "shared0.py" in verdict
        assert "foreign0.py" not in verdict

    def test_clone_budget_verdict_lists_smallest_family_first(self):
        """The clustering order (size, then first member) carries into the text."""
        four = [(_member(file=f"big{i}.py"), ("A", ")")) for i in range(4)]
        three = [(_member(file=f"small{i}.py"), ("B", ")")) for i in range(3)]
        families = cluster_functions([*four, *three])

        verdict = clone_budget_verdict(7, 0, root="pkg", families=families)

        assert verdict is not None
        assert verdict.index("small0.py") < verdict.index("big0.py")


class TestFailClosedReasonsContract:
    """Every uncounted file or directory is one reason; a family line names every site."""

    def test_fail_closed_reasons_empty_scan_returns_no_reasons(self):
        assert (
            fail_closed_reasons(CloneScan(families=(), unparsed=(), unlistable=()))
            == []
        )

    def test_fail_closed_reasons_lists_unparsed_files_then_unlistable_directories(self):
        scan = CloneScan(
            families=(),
            unparsed=("pkg/a.py", "pkg/b.py"),
            unlistable=("/checkout/pkg/locked",),
        )

        reasons = fail_closed_reasons(scan)

        assert len(reasons) == 3
        assert reasons[0].startswith("unparsed file")
        assert reasons[0].endswith("pkg/a.py")
        assert reasons[1].endswith("pkg/b.py")
        assert reasons[2].startswith("unlistable directory")
        assert reasons[2].endswith("/checkout/pkg/locked")

    def test_format_family_names_size_tokens_and_every_site(self):
        family = (
            CloneMember("pkg", "pkg/a.py", 2, "Worker._update", 74),
            CloneMember("pkg", "pkg/b.py", 9, "Worker._refresh", 74),
            CloneMember("pkg", "pkg/c.py", 14, "Worker._writer", 74),
        )

        assert format_family(family) == (
            "3 members x 74 tokens: pkg/a.py:2 Worker._update, "
            "pkg/b.py:9 Worker._refresh, pkg/c.py:14 Worker._writer"
        )
