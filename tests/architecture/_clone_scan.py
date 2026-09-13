"""Type-2 clone scan over function bodies — the count behind the rule of three.

Two copies of a function body are the cheap, correct, reversible state: at two
it is undecidable whether they are the same thing or merely alike. The third
copy is the extraction trigger. That rule is only checkable if something counts
the copies, and the count cannot come from recall — the founding family reached
forty-two members, each authored by a session that correctly judged its own
copy to be "this service's worker", not "a utility". This module is the
counter.

**Normalization.** A function body is rendered as a token stream from a
read-only walk of its AST: one token per node, one token naming each child
field that holds anything (so the ``body`` / ``orelse`` boundary of an ``if``
and the ``lower`` / ``upper`` of a slice are in the stream — two bodies that
differ only in where a statement sits relative to an ``else:`` are different
code), a placeholder for a positional hole inside a list (the ``**`` key of a
dict display, a keyword-only parameter without a default), and one closer per
node; the leading docstring is skipped at emission. Identifiers (names,
attributes, arguments, keywords, nested def/class names, import targets,
match captures, type parameters) are alpha-renamed in first-seen order per
function, so the method name, the delegated call, and the handle attribute all
collapse. String and bytes constants collapse to their type — the copies of a
shape differ on a log event name, a metric name, or a thread ``name=`` far
more often than on structure, and a scan that keeps those literals is blind to
the second real family in the tree. Numbers, booleans and ``None`` are kept:
``daemon=True`` and ``daemon=False`` are different code. Scalar flags that
carry no structure — a string's ``u`` prefix, an f-string conversion, a
relative-import level, a type comment — are dropped; ``async for`` inside a
comprehension is kept.

**Floor and threshold.** Bodies under ``CLONE_TOKEN_FLOOR`` normalized tokens
are ignored — below it every getter and every ``return self._x`` looks alike.
The unit is the *normalized token count* (roughly three times the ``ast.walk``
node count under this encoding); a floor stated in nodes does not reproduce. A
cluster with at least ``RECURRENCE_THRESHOLD`` members is an *at-threshold
family*; the gate's budget unit is the total member count across those
families, per source root.

**Read-only contract.** ``parse_ast`` is ``lru_cache``d and every architecture
gate in the process reads the same tree objects. Nothing here assigns to a
node, strips a docstring from a body, renames in place, or caches on a node
attribute — the stream is emitted, never written back. Normalizing the same
tree twice yields the same stream and leaves ``ast.dump`` unchanged.

**Fail-closed walk.** ``Path.rglob`` swallows the ``PermissionError`` of a
directory it cannot list, and ``parse_ast`` returns ``None`` for a file it
cannot read or parse. Under an exact-match ratchet either silently lowers the
total and the gate would then instruct the developer to lower the budget,
baking the loss in. The walk here surfaces unlistable directories through
``os.walk(onerror=...)`` and the scan reports unparsed files; the gate fails on
either before it compares a single number.

**Scope.** Clusters form over the roots present in one checkout, and every
member is attributed to its root. A family split across the public and the
private checkout so that neither side alone holds three copies is invisible to
both gates — the concrete shape of that blind spot is recorded in the gate.

This is a non-test helper (no ``test_`` prefix, so pytest does not collect it).
"""

from __future__ import annotations

import ast
import os
from collections import defaultdict
from collections.abc import Iterable, Iterator, Mapping
from pathlib import Path
from typing import NamedTuple

from tests.architecture._helpers import parse_ast, symbol_of

__all__ = [
    "CLONE_TOKEN_FLOOR",
    "RECURRENCE_THRESHOLD",
    "CloneMember",
    "CloneScan",
    "cluster_functions",
    "format_family",
    "iter_function_defs",
    "normalized_body_tokens",
    "scan_roots",
    "walk_python_files",
]

# Minimum normalized-token count for a body to take part in clustering. Measured
# reference points (tokens): crash-capture wrapper 107, service-singleton getter
# 75, thread spawner 133; a settings-singleton getter is 31 and stays out.
CLONE_TOKEN_FLOOR = 56

# The rule of three: a cluster of this many members is an at-threshold family.
RECURRENCE_THRESHOLD = 3

_FUNCTION_TYPES = (ast.FunctionDef, ast.AsyncFunctionDef)
# Nodes whose ``body`` may open with a docstring (skipped at emission).
_DOCSTRING_SCOPE_TYPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)

# Node type -> the attribute holding its single identifier, alpha-renamed.
_IDENT_ATTR: dict[type[ast.AST], str] = {
    ast.Name: "id",
    ast.Attribute: "attr",
    ast.arg: "arg",
    ast.keyword: "arg",
    ast.FunctionDef: "name",
    ast.AsyncFunctionDef: "name",
    ast.ClassDef: "name",
    ast.ExceptHandler: "name",
    ast.ImportFrom: "module",
    ast.MatchAs: "name",
    ast.MatchStar: "name",
    ast.MatchMapping: "rest",
}
# PEP 695 type parameters carry their name as a plain string (3.12+).
for _type_param in ("TypeVar", "ParamSpec", "TypeVarTuple"):
    _type_param_cls = getattr(ast, _type_param, None)
    if _type_param_cls is not None:
        _IDENT_ATTR[_type_param_cls] = "name"
# Node type -> a scalar attribute that IS structure and is kept by repr.
_SCALAR_TOKEN_ATTR: dict[type[ast.AST], str] = {
    ast.MatchSingleton: "value",
    ast.comprehension: "is_async",
}
# Scalar fields that never hold a child node and carry no clone signal.
_SCALAR_FIELDS = frozenset(
    {"kind", "type_comment", "level", "conversion", "is_async", "simple"}
)
# Per-type child-bearing field tuples, resolved lazily on first sight.
_CHILD_FIELDS: dict[type[ast.AST], tuple[str, ...]] = {}
_CLOSER = ")"
# A positional hole inside a list field: ``{**a, "k": b}`` has keys
# ``[None, Constant]``; without the placeholder it is the same stream as
# ``{"k": b, **a}``, which merges in the opposite order.
_HOLE = "None"


class CloneMember(NamedTuple):
    """One function in an at-threshold family."""

    root: str
    file: str
    line: int
    qualname: str
    tokens: int


class CloneScan(NamedTuple):
    """Result of ``scan_roots``: the at-threshold families plus the fail-closed halves."""

    families: tuple[tuple[CloneMember, ...], ...]
    unparsed: tuple[str, ...]
    unlistable: tuple[str, ...]

    def members_per_root(self) -> dict[str, int]:
        """Total at-threshold family members attributed to each root."""
        totals: dict[str, int] = defaultdict(int)
        for family in self.families:
            for member in family:
                totals[member.root] += 1
        return dict(totals)


def _child_fields(cls: type[ast.AST]) -> tuple[str, ...]:
    fields = _CHILD_FIELDS.get(cls)
    if fields is None:
        skip = set(_SCALAR_FIELDS)
        ident = _IDENT_ATTR.get(cls)
        if ident is not None:
            skip.add(ident)
        scalar = _SCALAR_TOKEN_ATTR.get(cls)
        if scalar is not None:
            skip.add(scalar)
        if cls is ast.alias:
            skip.update(("name", "asname"))
        if cls is ast.Global or cls is ast.Nonlocal:
            skip.add("names")
        fields = _CHILD_FIELDS[cls] = tuple(f for f in cls._fields if f not in skip)
    return fields


def _docstring_offset(body: list[ast.stmt]) -> int:
    """1 when ``body`` opens with a docstring expression, else 0."""
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        return 1
    return 0


def normalized_body_tokens(
    func: ast.FunctionDef | ast.AsyncFunctionDef,
) -> tuple[str, ...]:
    """Render ``func``'s body as an alpha-renamed, string-collapsed token stream.

    Pure over the tree: reads node fields, assigns nothing, caches nothing on
    a node. One token per node, one token naming each child field that holds
    anything, a placeholder for a ``None`` item inside a list field, and one
    closer per node; the leading docstring of the body (and of any nested def
    / class body) is skipped at emission rather than removed from the tree.
    """
    names: dict[str, str] = {}
    out: list[str] = []
    append = out.append

    def rename(value: str) -> str:
        alias = names.get(value)
        if alias is None:
            alias = names[value] = f"v{len(names)}"
        return alias

    def emit(node: ast.AST) -> None:
        cls = type(node)
        tname = cls.__name__
        ident_attr = _IDENT_ATTR.get(cls)
        if ident_attr is not None:
            value = getattr(node, ident_attr)
            append(tname if value is None else f"{tname}:{rename(value)}")
        elif cls is ast.Constant:
            value = node.value  # type: ignore[attr-defined]
            if isinstance(value, (str, bytes)):
                append(f"{tname}:{type(value).__name__}")
            else:
                append(f"{tname}:{value!r}")
        elif cls is ast.alias:
            parts = [tname]
            for value in (node.name, node.asname):  # type: ignore[attr-defined]
                if value is not None:
                    parts.append(rename(value))
            append(":".join(parts))
        elif cls is ast.Global or cls is ast.Nonlocal:
            append(":".join([tname, *(rename(v) for v in node.names)]))  # type: ignore[attr-defined]
        else:
            scalar_attr = _SCALAR_TOKEN_ATTR.get(cls)
            if scalar_attr is None:
                append(tname)
            else:
                append(f"{tname}:{getattr(node, scalar_attr)!r}")
        fields = node.__dict__
        for field in _child_fields(cls):
            value = fields.get(field)
            if value is None:
                continue
            if isinstance(value, list):
                start = 0
                if field == "body" and isinstance(node, _DOCSTRING_SCOPE_TYPES):
                    start = _docstring_offset(value)
                items = value[start:]
                if not items:
                    continue
                append(field)
                for item in items:
                    if item is None:
                        append(_HOLE)
                    elif isinstance(item, ast.AST):
                        emit(item)
            elif isinstance(value, ast.AST):
                append(field)
                emit(value)
        append(_CLOSER)

    body = func.body
    for statement in body[_docstring_offset(body) :]:
        emit(statement)
    return tuple(out)


def iter_function_defs(
    tree: ast.AST,
) -> Iterator[ast.FunctionDef | ast.AsyncFunctionDef]:
    """Yield every def / async def in ``tree``, nested ones included."""
    for node in ast.walk(tree):
        if isinstance(node, _FUNCTION_TYPES):
            yield node


def walk_python_files(root: Path) -> tuple[list[Path], list[str]]:
    """Return ``(python_files, unlistable_directories)`` under ``root``.

    ``os.walk`` with ``onerror`` set, because ``Path.rglob`` swallows the
    ``PermissionError`` of a directory it cannot list and the files under it
    would silently vanish from the count. ``__pycache__`` is pruned. Both lists
    are sorted for a deterministic member order.
    """
    files: list[Path] = []
    unlistable: list[str] = []

    def record(error: OSError) -> None:
        unlistable.append(str(error.filename or root))

    for dirpath, dirnames, filenames in os.walk(root, onerror=record):
        dirnames[:] = sorted(name for name in dirnames if name != "__pycache__")
        for name in sorted(filenames):
            if name.endswith(".py"):
                files.append(Path(dirpath) / name)
    return files, sorted(unlistable)


def cluster_functions(
    members: Iterable[tuple[CloneMember, tuple[str, ...]]],
    *,
    threshold: int = RECURRENCE_THRESHOLD,
) -> tuple[tuple[CloneMember, ...], ...]:
    """Group ``(member, token_stream)`` pairs by identical stream; keep the at-threshold ones.

    Families are ordered by size then by first member so the output is stable
    across runs and the smallest families — the ones a third copy just
    created — come first.
    """
    by_stream: dict[tuple[str, ...], list[CloneMember]] = defaultdict(list)
    for member, stream in members:
        by_stream[stream].append(member)
    families = [tuple(group) for group in by_stream.values() if len(group) >= threshold]
    families.sort(key=lambda family: (len(family), family[0]))
    return tuple(families)


def scan_roots(
    roots: Mapping[str, Path],
    *,
    relative_to: Path,
    floor: int = CLONE_TOKEN_FLOOR,
    threshold: int = RECURRENCE_THRESHOLD,
) -> CloneScan:
    """Cluster every function body under the given roots, attributing members per root.

    ``roots`` maps a budget key to a directory; the roots are clustered
    together (one checkout, one population). Files ``parse_ast`` cannot parse
    and directories the walk cannot list are reported, never dropped.
    """
    members: list[tuple[CloneMember, tuple[str, ...]]] = []
    unparsed: list[str] = []
    unlistable: list[str] = []
    for root_name, root in roots.items():
        files, blocked = walk_python_files(root)
        unlistable.extend(blocked)
        for path in files:
            tree = parse_ast(path)
            if tree is None:
                unparsed.append(path.relative_to(relative_to).as_posix())
                continue
            file_posix = path.relative_to(relative_to).as_posix()
            for func in iter_function_defs(tree):
                stream = normalized_body_tokens(func)
                if len(stream) < floor:
                    continue
                member = CloneMember(
                    root_name,
                    file_posix,
                    func.lineno,
                    symbol_of(tree, func),
                    len(stream),
                )
                members.append((member, stream))
    return CloneScan(
        families=cluster_functions(members, threshold=threshold),
        unparsed=tuple(sorted(unparsed)),
        unlistable=tuple(sorted(unlistable)),
    )


def format_family(family: tuple[CloneMember, ...]) -> str:
    """One line per family: size, token count, then every ``file:line qualname``."""
    sites = ", ".join(f"{m.file}:{m.line} {m.qualname}" for m in family)
    return f"{len(family)} members x {family[0].tokens} tokens: {sites}"
