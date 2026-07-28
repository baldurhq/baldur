"""G74 — a repository-issued DLQ entry id is an opaque string, and stays one.

``FailedOperationRepository.create()`` / ``get_by_id()`` publish the entry id as
an opaque ``str``: callers compare it, pass it and store it, but never parse it,
widen it, or assert int-ness about it. Numeric backends translate at the adapter
boundary — ``str(pk)`` on read, ``int(id)`` on bind — and that translation is the
only sanctioned place the two representations meet.

The contract lived in a private docstring for most of its life, and five call
sites plus one operator script violated it undetected: a numeric parse of an id
compiles, type-checks and passes every other gate, so the class is invisible at
review time. Publishing the contract changed its discoverability, not its
detectability. This gate is the ratchet.

Two halves, deliberately asymmetric:

- **Half A (parses)** — on the ``examples/`` tree, an ``int(...)`` whose argument
  denotes an entry id, and an argparse option of an entry id declared
  ``type=int``. Restricted to that tree because a parse and the sanctioned
  adapter-boundary translation are AST-identical, and this rule has no waiver
  seam to separate them.
- **Half B (declarations)** — on the OSS source tree, this repository's test
  tree and its ``examples/`` tree, an entry id *declared* ``int``: a parameter,
  variable or return annotation whose subtree names ``int``, or a ``cast`` to
  ``int``. A ``cast`` performs no conversion — it makes the type checker believe
  an opaque string is numeric, and the arithmetic it unblocks fails later at
  runtime — so it is a declaration, not a parse.

ENFORCED-EMPTY: there is no baseline key and no waiver seam. A true positive is
a contract violation (fix the code); a false positive is a detector bug (fix the
rule, in both repository copies, which the rule's parity check turns into a
single reviewed unit).

Architectural fitness function rule registry:
``ARCHITECTURE.md#g74-dlq-entry-id-opaque-contract``
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.architecture._helpers import oss_src_root
from tests.architecture.conftest import PROJECT_ROOT, parse_ast, walk_src

# --- g74 spec:begin ---
# The rule itself, byte-identical in both repository copies. Only import-free,
# root-free definitions live between these markers; imports, scan roots, floor
# constants and fixtures legitimately differ per repository and stay outside.
#
# What this rule deliberately does NOT cover — stated here so whoever widens or
# loosens it reads the boundary instead of assuming coverage:
#   (a) a numeric assumption that survives a data-flow rename. The surviving
#       names carry no dlq/entry id token, so no name-keyed AST scan can see it;
#       catching it needs taint analysis.
#   (b) an uppercase spelling. The name rule is case-sensitive by convention.
#   (c) an int() parse inside OSS source. The sanctioned adapter-boundary
#       translation is AST-identical to a violation and there is no waiver seam,
#       so that surface is policed for int *declarations* only.
#   (d) a helper that *returns* an entry id under a name this rule cannot see.
#       Widening the key to any dlq-named function was measured: it flags the
#       many legitimate count-returning helpers far more often than a real id.
#   (e) numeric *formatting* of an id — f"{entry_id:04d}", "{:d}".format(...),
#       "%d" % ... — which carries neither a parse nor a declaration. It raises
#       at runtime rather than corrupting data.
# The remedy for (c) and (d) is type-level (a distinct id type on the repository
# contract), which is a public-contract decision rather than a detector change.

_ID_NAME_RE = re.compile(r"(?:^|_)(?:dlq|entry)_id(?:_(?:str|raw|value|text|token))?$")

# Callees whose *arguments* can still denote the id: a field-name lookup, where
# the argument is the field name and the return value is the id. Restricted to
# string-constant arguments — with a variable argument the very same node is a
# container read, returning the value stored *under* the id, which is not an id.
_ARG_TRANSPARENT_CALLEES = frozenset({"get", "getlist", "pop"})

# Callees whose *receiver* carries the id; their arguments are strip characters
# and encodings, never the id.
_RECEIVER_TRANSPARENT_CALLEES = frozenset(
    {"decode", "encode", "lstrip", "rstrip", "strip"}
)

_OPAQUE_ID_REMEDY = (
    "a repository-issued DLQ entry id is an opaque str -- compare and pass it "
    "as a string; do not parse, widen, or cast it"
)

# The surfaces this rule knows about. Which arms run on which surface is decided
# by violations_in below, inside this block, so a copy cannot quietly stop
# enforcing a form on a surface without editing the text parity compares.
SCAN_ROLES: tuple[str, ...] = ("examples", "oss_source", "tests")


@dataclass(frozen=True)
class Violation:
    """One breach: where it is, which form it took, and what to do instead."""

    lineno: int
    form: str
    name: str

    def message(self, location: str) -> str:
        """Render a self-contained report — the reader needs no other document."""
        return (
            f"{location}:{self.lineno} {self.form} of '{self.name}' -- "
            f"{_OPAQUE_ID_REMEDY}"
        )


def _normalize(name: str) -> str:
    """Reduce an argparse option spelling to its identifier form."""
    return name.lstrip("-").replace("-", "_")


def _matches(name: str) -> bool:
    """True iff ``name`` denotes a repository-issued DLQ entry id.

    The trailing anchor plus the closed suffix list admit ``entry_id_str`` while
    rejecting the id-derived quantities (``entry_id_width``, ``dlq_id_count``).
    """
    return _ID_NAME_RE.search(_normalize(name)) is not None


def _id_positions(node: ast.AST) -> Iterator[str]:
    """Yield every name an expression can denote without selecting something else.

    The walk stops at any node that has already selected a *different* value, so
    a parse of some other field of an entry looked up by id is not reported.
    """
    if isinstance(node, ast.Name):
        yield node.id
    elif isinstance(node, ast.Attribute):
        yield node.attr
    elif isinstance(node, ast.Constant):
        if isinstance(node.value, str):
            yield node.value
    elif isinstance(node, ast.Subscript):
        yield from _id_positions(node.slice)
    elif isinstance(node, ast.IfExp):
        yield from _id_positions(node.body)
        yield from _id_positions(node.orelse)
    elif isinstance(node, ast.BoolOp):
        for value in node.values:
            yield from _id_positions(value)
    elif isinstance(node, ast.NamedExpr):
        yield from _id_positions(node.value)
    elif isinstance(node, ast.Call):
        yield from _call_id_positions(node)


def _call_id_positions(node: ast.Call) -> Iterator[str]:
    """Yield the identity-preserving positions of a transparent call."""
    func = node.func
    if isinstance(func, ast.Name) and func.id == "str":
        for argument in node.args:
            yield from _id_positions(argument)
        for keyword in node.keywords:
            yield from _id_positions(keyword.value)
        return
    if not isinstance(func, ast.Attribute):
        return
    if func.attr in _ARG_TRANSPARENT_CALLEES:
        for argument in node.args:
            if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
                yield argument.value
        return
    if func.attr in _RECEIVER_TRANSPARENT_CALLEES:
        yield from _id_positions(func.value)


def _first_id_name(node: ast.AST) -> str | None:
    """Return the first identity-preserving name of ``node`` matching the rule."""
    for name in _id_positions(node):
        if _matches(name):
            return name
    return None


def _annotates_int(annotation: ast.expr | None) -> bool:
    """True iff an annotation subtree names ``int`` (``int``, ``int | None``...)."""
    if annotation is None:
        return False
    return any(
        isinstance(node, ast.Name) and node.id == "int" for node in ast.walk(annotation)
    )


def int_parse_violations(module: ast.Module) -> list[Violation]:
    """Arm (a) — an ``int(...)`` whose first argument denotes an entry id."""
    violations: list[Violation] = []
    for node in ast.walk(module):
        if not isinstance(node, ast.Call):
            continue
        if not (isinstance(node.func, ast.Name) and node.func.id == "int"):
            continue
        if not node.args:
            continue
        name = _first_id_name(node.args[0])
        if name is not None:
            violations.append(Violation(node.lineno, "int() parse", name))
    return violations


def argparse_int_option_violations(module: ast.Module) -> list[Violation]:
    """Arm (b) — an argparse option named for an entry id declared ``type=int``."""
    violations: list[Violation] = []
    for node in ast.walk(module):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "add_argument"):
            continue
        if not any(
            keyword.arg == "type"
            and isinstance(keyword.value, ast.Name)
            and keyword.value.id == "int"
            for keyword in node.keywords
        ):
            continue
        for argument in node.args:
            if (
                isinstance(argument, ast.Constant)
                and isinstance(argument.value, str)
                and _matches(argument.value)
            ):
                violations.append(
                    Violation(node.lineno, "argparse int option", argument.value)
                )
                break
    return violations


def int_declaration_violations(module: ast.Module) -> list[Violation]:
    """Half B — an entry id *declared* ``int`` by annotation or by ``cast``."""
    violations: list[Violation] = []
    for node in ast.walk(module):
        if isinstance(node, ast.arg):
            if _matches(node.arg) and _annotates_int(node.annotation):
                violations.append(
                    Violation(node.lineno, "int parameter annotation", node.arg)
                )
        elif isinstance(node, ast.AnnAssign):
            name = _first_id_name(node.target)
            if name is not None and _annotates_int(node.annotation):
                violations.append(
                    Violation(node.lineno, "int variable annotation", name)
                )
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            if _matches(node.name) and _annotates_int(node.returns):
                violations.append(
                    Violation(node.lineno, "int return annotation", node.name)
                )
        elif isinstance(node, ast.Call):
            violations.extend(_cast_to_int_violations(node))
    return violations


def _cast_to_int_violations(node: ast.Call) -> Iterator[Violation]:
    """Yield a violation for ``cast(int, <entry id>)`` in either spelling."""
    func = node.func
    if not (isinstance(func, ast.Name) and func.id == "cast"):
        return
    if len(node.args) < 2:
        return
    target = node.args[0]
    declares_int = (isinstance(target, ast.Name) and target.id == "int") or (
        isinstance(target, ast.Constant) and target.value == "int"
    )
    if not declares_int:
        return
    name = _first_id_name(node.args[1])
    if name is not None:
        yield Violation(node.lineno, "cast to int", name)


def violations_in(module: ast.Module, role: str) -> list[Violation]:
    """Run every arm this rule applies to ``role`` — the single dispatch point.

    Arm selection lives inside the shared block on purpose: a copy that stopped
    invoking an arm from its own wiring would keep the arm's source verbatim,
    stay green on its own tree, and still pass a text comparison.
    """
    if role not in SCAN_ROLES:
        raise ValueError(f"unknown scan role: {role!r}")
    violations = int_declaration_violations(module)
    if role == "examples":
        violations.extend(int_parse_violations(module))
        violations.extend(argparse_int_option_violations(module))
    return sorted(violations, key=lambda item: (item.lineno, item.form, item.name))


# --- g74 spec:end ---

# Non-vacuity floors, counted on *successfully parsed* files: ``parse_ast``
# returns None on a syntax or read error, and a silently skipped file is the one
# exit that could hide a violation. Every floor keeps a wide margin over today's
# count (13 / 1,230 / 1,531) — it is a "the walk is broken" net, not a census.
_MIN_PARSED_EXAMPLES_FILES = 5
_MIN_PARSED_OSS_SOURCE_FILES = 400
_MIN_PARSED_TESTS_FILES = 500

_MIN_PARSED_FILES: dict[str, int] = {
    "examples": _MIN_PARSED_EXAMPLES_FILES,
    "oss_source": _MIN_PARSED_OSS_SOURCE_FILES,
    "tests": _MIN_PARSED_TESTS_FILES,
}

# A numeric floor proves the walk found files, not that it found the *right*
# ones: this tree's margins are wide enough for a whole subtree to go unscanned
# while the count still passes. Each named subtree must contribute >=1 parsed
# file, which no unrelated file count can satisfy.
_EXPECTED_SUBTREES: dict[str, tuple[str, ...]] = {
    "examples": (),
    "oss_source": (),
    "tests": ("unit", "architecture"),
}


def _scan_roots() -> dict[str, Path]:
    """Map each scan role to this repository's root for it."""
    return {
        "examples": PROJECT_ROOT / "examples",
        "oss_source": oss_src_root(),
        "tests": PROJECT_ROOT / "tests",
    }


def _parsed_modules(root: Path) -> list[tuple[Path, ast.Module]]:
    """Parse every ``.py`` file under ``root``, dropping the unparseable ones."""
    parsed: list[tuple[Path, ast.Module]] = []
    for path in sorted(walk_src((root,))):
        module = parse_ast(path)
        if module is not None:
            parsed.append((path, module))
    return parsed


def non_vacuity_errors(role: str, root: Path, parsed: list[Path]) -> list[str]:
    """Report why a scan of ``root`` cannot be trusted to have seen anything."""
    errors: list[str] = []
    floor = _MIN_PARSED_FILES[role]
    if len(parsed) < floor:
        errors.append(
            f"the {role} root {root} yielded {len(parsed)} parsed files, "
            f"below the floor of {floor} -- the scan is not reaching the tree"
        )
    for subtree in _EXPECTED_SUBTREES[role]:
        expected = root / subtree
        if not any(path.is_relative_to(expected) for path in parsed):
            errors.append(
                f"the {role} root {root} contributed no parsed file under "
                f"{subtree}/ -- the root resolves to the wrong directory"
            )
    return errors


def _format(role: str, root: Path, messages: list[str]) -> str:
    body = "\n".join(f"  - {message}" for message in messages)
    return f"G74 ({role} root {root}):\n{body}"


class TestDlqEntryIdOpaqueContract:
    """G74 — no live tree parses, widens or casts a DLQ entry id."""

    @pytest.mark.parametrize("role", SCAN_ROLES)
    def test_scan_is_not_vacuous(self, role):
        root = _scan_roots()[role]
        parsed = [path for path, _ in _parsed_modules(root)]
        errors = non_vacuity_errors(role, root, parsed)
        assert not errors, _format(role, root, errors)

    @pytest.mark.parametrize("role", SCAN_ROLES)
    def test_no_numeric_entry_id(self, role):
        root = _scan_roots()[role]
        messages = [
            violation.message(path.relative_to(root).as_posix())
            for path, module in _parsed_modules(root)
            for violation in violations_in(module, role)
        ]
        assert not messages, _format(role, root, messages)


# Synthetic sources. The gate's own file is scanned by half B (it lives under
# the tests tree), so every fixture stays a *string* written to tmp_path: a real
# annotated def here would make the gate report itself.
_RED_SOURCES: tuple[tuple[str, str], ...] = (
    ("parse_name", "value = int(entry_id_str)\n"),
    ("parse_attribute", "value = int(result.dlq_id)\n"),
    ("parse_field_lookup", 'value = int(request.POST.get("dlq_id", "1"))\n'),
    ("parse_receiver_strip", "value = int(entry_id.strip())\n"),
    ("parse_receiver_decode", 'value = int(dlq_id.decode("utf-8"))\n'),
    ("parse_receiver_encode", "value = int(entry_id.encode())\n"),
    ("parse_attribute_strip", "value = int(entry.dlq_id.strip())\n"),
    ("parse_str_call", "value = int(str(entry_id))\n"),
    ("argparse_bare", 'parser.add_argument("entry_id", type=int)\n'),
    ("argparse_dashed", 'parser.add_argument("--dlq-id", type=int)\n'),
    ("declare_parameter", "def load(entry_id: int) -> None: ...\n"),
    ("declare_parameter_optional", "def load(dlq_id: int | None) -> None: ...\n"),
    ("declare_parameter_migrating", "def load(entry_id: str | int) -> None: ...\n"),
    ("declare_annassign_name", "dlq_id: int = 1\n"),
    (
        "declare_annassign_attribute",
        "class E:\n    def __init__(self):\n        self.entry_id: int = 1\n",
    ),
    ("declare_return", "def dlq_id() -> int: ...\n"),
    ("declare_return_optional", "def entry_id() -> Optional[int]: ...\n"),
    ("cast_name_form", "value = cast(int, entry.dlq_id)\n"),
    ("cast_string_form", 'value = cast("int", entry_id)\n'),
)

_CLEAN_SOURCES: tuple[tuple[str, str], ...] = (
    ("other_field_of_entry", 'value = int(repo.get_by_id(entry_id)["retry_count"])\n'),
    ("id_as_helper_argument", 'value = int(_redis_cli("ZSCORE", key, entry_id))\n'),
    ("id_derived_option", 'parser.add_argument("--entry-id-width", type=int)\n'),
    ("id_derived_stat", 'value = int(stats["dlq_id_count"])\n'),
    ("container_read_by_id", "value = int(client.get(entry_id) or 0)\n"),
    ("unrelated_receiver", "value = int(response.text.strip())\n"),
    ("sub_token_arithmetic", 'value = int(entry_id.rsplit(":", 1)[1])\n'),
    ("unrelated_name", "value = int(order_id)\n"),
    ("id_sequence_element", "value = int(entry_ids[0])\n"),
    ("string_declaration", "def load(entry_id: str) -> None: ...\n"),
    ("cast_to_other_type", 'value = cast("HttpResponse", entry_id)\n'),
    ("unrelated_option", 'parser.add_argument("--dlq-domains", type=int)\n'),
)


def _violations_of(source: str, role: str = "examples") -> list[Violation]:
    return violations_in(ast.parse(source), role)


class TestDetectorSpec:
    """The rule reports every violation form and none of the look-alikes."""

    @pytest.mark.parametrize(
        ("label", "source"), _RED_SOURCES, ids=[label for label, _ in _RED_SOURCES]
    )
    def test_violation_form_is_reported(self, label, source):
        assert _violations_of(source), f"{label}: expected a violation"

    @pytest.mark.parametrize(
        ("label", "source"), _CLEAN_SOURCES, ids=[label for label, _ in _CLEAN_SOURCES]
    )
    def test_look_alike_is_not_reported(self, label, source):
        assert _violations_of(source) == [], f"{label}: expected no violation"

    def test_declaration_arm_runs_on_every_role(self):
        """Half B covers all three surfaces; half A only the examples tree."""
        declaration = "def load(entry_id: int) -> None: ...\n"
        for role in SCAN_ROLES:
            assert _violations_of(declaration, role), role

    def test_parse_arms_are_scoped_to_the_examples_tree(self):
        """The sanctioned adapter-boundary translation stays out of reach."""
        for source in (
            "value = int(entry_id_str)\n",
            'p.add_argument("dlq_id", type=int)\n',
        ):
            assert _violations_of(source, "examples")
            assert _violations_of(source, "oss_source") == []
            assert _violations_of(source, "tests") == []

    def test_dispatch_invokes_every_arm(self):
        """A dropped arm is invisible to the parity check it was added to protect."""
        source = (
            "def load(entry_id: int) -> None: ...\n"
            "value = int(result.dlq_id)\n"
            'parser.add_argument("--dlq-id", type=int)\n'
            "widened = cast(int, entry_id)\n"
        )
        forms = {violation.form for violation in _violations_of(source)}
        assert forms == {
            "int parameter annotation",
            "int() parse",
            "argparse int option",
            "cast to int",
        }

    def test_unknown_role_is_rejected(self):
        with pytest.raises(ValueError, match="unknown scan role"):
            violations_in(ast.parse(""), "src")

    @pytest.mark.parametrize(
        ("source", "expected"),
        [
            ("entry_id", ["entry_id"]),
            ("entry.dlq_id", ["dlq_id"]),
            ('"dlq_id"', ["dlq_id"]),
            ('mapping["dlq_id"]', ["dlq_id"]),
            ('mapping[entry_id]["retry_count"]', ["retry_count"]),
            ("dlq_id if flag else entry_id", ["dlq_id", "entry_id"]),
            ("entry_id or fallback", ["entry_id", "fallback"]),
            ('form.get("dlq_id", "1")', ["dlq_id", "1"]),
            ("form.get(entry_id)", []),
            ("entry_id.strip()", ["entry_id"]),
            ('entry_id.decode("utf-8")', ["entry_id"]),
            ("raw.strip(entry_id)", ["raw"]),
            ("str(entry_id)", ["entry_id"]),
            ("helper(entry_id)", []),
        ],
    )
    def test_id_positions_table(self, source, expected):
        """Both transparency directions, pinned separately from the arms."""
        node = ast.parse(source, mode="eval").body
        assert list(_id_positions(node)) == expected

    def test_violation_message_is_self_contained(self):
        """A red CI log states the remedy; it does not point at a doc anchor."""
        (violation,) = _violations_of("value = int(entry_id_str)\n")
        message = violation.message("examples/driver.py")
        assert "opaque str" in message
        assert "do not parse, widen, or cast it" in message
        assert "ARCHITECTURE.md#" not in message


class TestNonVacuityGuards:
    """The floors and the structural check red rather than pass an empty scan."""

    def test_empty_root_trips_the_floor(self, tmp_path):
        errors = non_vacuity_errors("oss_source", tmp_path, [])
        assert any("below the floor" in error for error in errors)

    def test_partial_root_trips_the_structural_check(self, tmp_path):
        """A tests root resolving to one subtree passes the count, not the shape."""
        parsed = [tmp_path / "unit" / f"test_{index}.py" for index in range(600)]
        errors = non_vacuity_errors("tests", tmp_path, parsed)
        assert errors == [
            f"the tests root {tmp_path} contributed no parsed file under "
            f"architecture/ -- the root resolves to the wrong directory"
        ]

    def test_complete_root_passes(self, tmp_path):
        parsed = [tmp_path / "unit" / f"test_{index}.py" for index in range(600)]
        parsed.append(tmp_path / "architecture" / "test_rule.py")
        assert non_vacuity_errors("tests", tmp_path, parsed) == []


class TestScanWiring:
    """The per-repository wiring: which roots are walked, and what survives it."""

    def test_unparseable_file_is_dropped_from_the_parsed_set(self, tmp_path):
        """The floors count *parsed* files, which is what closes the silent exit.

        A file the parser rejects is skipped without a word, so it could carry a
        violation past the rule. Counting the walk instead of the parse would let
        a tree of unparseable files clear every floor.
        """
        # Given: a tree the walk sees whole, one of whose files cannot be parsed
        readable = tmp_path / "readable.py"
        readable.write_text("value = 1\n", encoding="utf-8")
        broken = tmp_path / "broken.py"
        broken.write_text("def (\n", encoding="utf-8")

        # When
        parsed = _parsed_modules(tmp_path)

        # Then: the walk offered both; only the parsed one is there to be counted
        assert sorted(walk_src((tmp_path,))) == [broken, readable]
        assert [path for path, _ in parsed] == [readable]

    def test_every_scan_role_carries_a_root_a_floor_and_a_subtree_rule(self):
        """A role added without its wiring surfaces as a KeyError that names nothing.

        The three role-keyed tables live outside the shared rule text because
        each repository spells them differently, so nothing else compares them
        against the roles the rule declares.
        """
        assert set(SCAN_ROLES) == {"examples", "oss_source", "tests"}
        assert set(_scan_roots()) == set(SCAN_ROLES)
        assert set(_MIN_PARSED_FILES) == set(SCAN_ROLES)
        assert set(_EXPECTED_SUBTREES) == set(SCAN_ROLES)
