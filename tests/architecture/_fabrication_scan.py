"""Detector for the one authoring move that manufactures a fabricated metric.

A number an operator reads during an incident is either measured or it is a
lie, and the two are indistinguishable on screen. Twice now the lie arrived the
same way: a measurement-named field was assembled into a response payload with
a numeric literal standing in for the measurement — ``failure_rate_5m: 0.0``
built inside a loop, ``resolution_rate_percent=overview.get(…, 0.0)`` passed to
a DTO constructor. Both shipped, both rendered, and neither was caught by any
inventory gate, because neither added a new *surface* — they added a new leaf
inside a surface that already had a verdict.

This module is the syntactic detector for that move. It does not try to prove
every rendered slot is real; spread assembly (``{**stats}``) makes the slot
population a runtime property no static pass can enumerate. It flags the
authoring move instead, on three axes that have to coincide:

**Name class.** The field name carries a measurement token — ``_rate``,
``_percent``, ``_ratio``, ``_score``, ``_utilization`` — as a suffix or an
infix, so ``failure_rate_5m`` counts. Names ending ``_threshold``, ``_limit``,
``_multiplier``, ``_min`` or ``_max`` are excluded: those are configuration,
their numeric defaults are legitimate, and measurement showed they otherwise
dominate the population. ``0`` is the *correct* default for "how many happened",
whereas for "what is the level" unmeasured is neither 0 nor 100, so counters are
mostly out of scope by name alone.

*Mostly*, not by construction — the earlier wording overstated it. The token
matches as an infix, so a counter whose name merely contains a measurement word
does match: ``pending_rate_limit_resets`` is a count of pending resets and is
flagged, because ``_rate_`` sits inside ``rate_limit``. The exclusion list is
suffix-anchored and does not catch it. Those land in the baseline as
``out-of-surface`` rows with evidence, which is the intended handling of a
false positive here — narrowing the token rule instead would move the
population and needs its own decision.

**Shape.** Four, symmetric across dict assembly and constructor assembly:
``"<key>": <num>``, ``<x>.get("<key>", <num>)``, ``<kwarg>=<num>``, and
``<kwarg>=<x>.get(…, <num>)``. The two historical exemplars use one from each
family.

**Context.** Only functions that assemble a payload are scanned — a function
that returns a dict literal (directly, nested in the return expression, or via
a local it assigned one to), returns a constructor call, or feeds arguments to
a ``.json(…)`` sink. Eligibility is decided per function and then the *whole*
body is scanned, which is load-bearing: an expression-level rule that tracked
only the returned dict missed the founding loop-assembled exemplar entirely.

This is a non-test helper (no ``test_`` prefix, so pytest does not collect it).
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "CONFIG_NAME_SUFFIXES",
    "FabricationHit",
    "is_measurement_name",
    "is_payload_assembly",
    "scan_file",
    "scan_roots",
]

# A measurement token, as a suffix or an infix on a snake_case field name.
_MEASUREMENT_TOKEN_RE = re.compile(r"_(rate|percent|ratio|score|utilization)(?:_|$)")

# Names whose numeric default is configuration, not a measurement.
CONFIG_NAME_SUFFIXES = ("_threshold", "_limit", "_multiplier", "_min", "_max")

_FUNCTION_TYPES = (ast.FunctionDef, ast.AsyncFunctionDef)


@dataclass(frozen=True)
class FabricationHit:
    """One flagged field, with every site inside its function that produced it.

    ``symbol`` is the enclosing function's qualname, so a row keyed on
    (file, symbol, field) survives edits above it; the sites are evidence, never
    identity. A field assembled at several sites in the same function is one
    row carrying several sites — the count is asserted, so a *new* site inside
    an already-flagged function still regresses the gate, including one added to
    a line that already holds another.
    """

    file: str
    symbol: str
    field: str
    shape: str
    sites: tuple[tuple[int, int], ...]

    @property
    def key(self) -> str:
        return f"{self.file}::{self.symbol}::{self.field}"

    @property
    def occurrences(self) -> int:
        return len(self.sites)

    @property
    def lines(self) -> tuple[int, ...]:
        """The distinct lines the sites fall on — the human-readable evidence."""
        return tuple(sorted({line for line, _ in self.sites}))

    @property
    def evidence(self) -> str:
        return f"{self.file}:{','.join(str(n) for n in self.lines)}"


def is_measurement_name(name: str) -> bool:
    """Whether ``name`` names a measurement rather than a config value."""
    if name.endswith(CONFIG_NAME_SUFFIXES):
        return False
    return bool(_MEASUREMENT_TOKEN_RE.search(name))


def _is_number(node: ast.AST | None) -> bool:
    """A numeric literal — ``True``/``False`` are ints in Python and excluded."""
    return (
        isinstance(node, ast.Constant)
        and isinstance(node.value, (int, float))
        and not isinstance(node.value, bool)
    )


def _is_constructor_call(node: ast.AST) -> bool:
    """A call whose callee reads as a type name (leading capital)."""
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
    return bool(name) and name[0].isupper()


def _own_nodes(fn: ast.AST) -> Iterator[ast.AST]:
    """Every node inside ``fn`` except those belonging to a nested function.

    A nested function gets its own eligibility decision rather than inheriting
    its parent's, so it is skipped here and visited as a candidate in its own
    right. The skip is decided when the node is popped, not when it is pushed:
    a nested ``def`` is usually a direct statement of the enclosing body, and
    filtering only on the way in lets exactly that shape through, which both
    lends the parent an eligibility it did not earn and re-attributes the
    child's sites to the parent's qualname as a second row.
    """
    stack: list[ast.AST] = list(getattr(fn, "body", []))
    while stack:
        node = stack.pop()
        if isinstance(node, _FUNCTION_TYPES):
            continue
        yield node
        stack.extend(ast.iter_child_nodes(node))


def _str_key_of(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def is_payload_assembly(fn: ast.AST) -> bool:
    """Whether ``fn`` assembles a response payload (the scan's eligibility rule)."""
    dict_locals: set[str] = set()
    for node in _own_nodes(fn):
        value = getattr(node, "value", None)
        if isinstance(node, ast.Assign) and isinstance(value, ast.Dict):
            dict_locals.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(value, ast.Dict):
            if isinstance(node.target, ast.Name):
                dict_locals.add(node.target.id)

    for node in _own_nodes(fn):
        if isinstance(node, ast.Call):
            func = node.func
            if (
                isinstance(func, ast.Attribute)
                and func.attr == "json"
                and (node.args or node.keywords)
            ):
                return True
        if isinstance(node, ast.Return) and node.value is not None:
            returned = node.value
            if isinstance(returned, ast.Name) and returned.id in dict_locals:
                return True
            if _is_constructor_call(returned):
                return True
            if any(isinstance(sub, ast.Dict) for sub in ast.walk(returned)):
                return True
    return False


def _qualname(tree: ast.Module) -> dict[int, str]:
    """``id(node)`` -> enclosing qualname, following ``__qualname__`` semantics."""
    index: dict[int, str] = {}

    def descend(node: ast.AST, scope: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (*_FUNCTION_TYPES, ast.ClassDef)):
                separator = ".<locals>." if isinstance(node, _FUNCTION_TYPES) else "."
                inner = f"{scope}{separator}{child.name}" if scope else child.name
                index[id(child)] = inner
                descend(child, inner)
            else:
                descend(child, scope)

    descend(tree, "")
    return index


def _position(node: ast.AST) -> tuple[int, int]:
    """A site's identity: line AND column.

    Line alone is not identity. Two shapes can describe one site — a kwarg whose
    value is a ``.get`` fallback matches both the kwarg rule and the ``.get``
    rule — and those must merge, which they do because both resolve to the same
    node and therefore the same position. Two *distinct* sites can also share a
    line (``{"a": {"r": 0.0}, "b": {"r": 0.0}}``), and those must not merge: the
    occurrence count is what makes a second fabricated site inside an
    already-flagged function regress, and keying on the line alone silently
    counted that pair as one.
    """
    return node.lineno, node.col_offset


def _sites_in_function(fn: ast.AST) -> Iterator[tuple[str, str, tuple[int, int]]]:
    """``(field, shape, position)`` for every flagged assembly site inside ``fn``."""
    for node in _own_nodes(fn):
        if isinstance(node, ast.Dict):
            for key_node, value_node in zip(node.keys, node.values, strict=False):
                key = _str_key_of(key_node)
                if key and is_measurement_name(key) and _is_number(value_node):
                    yield key, "dict-literal", _position(key_node)
        elif isinstance(node, ast.Call):
            func = node.func
            if (
                isinstance(func, ast.Attribute)
                and func.attr == "get"
                and len(node.args) == 2
                and _is_number(node.args[1])
            ):
                key = _str_key_of(node.args[0])
                if key and is_measurement_name(key):
                    yield key, "get-fallback", _position(node)
        elif isinstance(node, ast.keyword) and node.arg:
            if not is_measurement_name(node.arg):
                continue
            if _is_number(node.value):
                yield node.arg, "kwarg", _position(node.value)
            elif (
                isinstance(node.value, ast.Call)
                and isinstance(node.value.func, ast.Attribute)
                and node.value.func.attr == "get"
                and len(node.value.args) == 2
                and _is_number(node.value.args[1])
            ):
                yield node.arg, "kwarg-get-fallback", _position(node.value)


def scan_source(source: str, *, file: str = "<source>") -> list[FabricationHit]:
    """Flagged fields in one module's source text, one record per row key.

    Two shapes can describe one site — ``resolution_rate_percent=x.get(…, 0.0)``
    is both a kwarg fallback and a ``.get`` fallback — so a field's sites are
    merged and deduplicated by *position*, which both shapes share. A field
    assembled at genuinely distinct sites keeps an occurrence per site, even
    when two of them sit on one line.
    """
    try:
        tree = ast.parse(source, filename=file)
    except SyntaxError:
        return []
    names = _qualname(tree)
    shapes: dict[tuple[str, str], str] = {}
    sites: dict[tuple[str, str], set[tuple[int, int]]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, _FUNCTION_TYPES) or not is_payload_assembly(node):
            continue
        symbol = names.get(id(node), node.name)
        for field, shape, position in _sites_in_function(node):
            slot = (symbol, field)
            shapes.setdefault(slot, shape)
            sites.setdefault(slot, set()).add(position)
    return sorted(
        (
            FabricationHit(
                file=file,
                symbol=symbol,
                field=field,
                shape=shapes[(symbol, field)],
                sites=tuple(sorted(sites[(symbol, field)])),
            )
            for symbol, field in sites
        ),
        key=lambda h: (h.file, h.symbol, h.field),
    )


def scan_file(path: Path, *, relative_to: Path) -> list[FabricationHit]:
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    try:
        name = path.resolve().relative_to(relative_to.resolve()).as_posix()
    except ValueError:
        name = path.as_posix()
    return scan_source(source, file=name)


def scan_roots(roots: Iterable[Path], *, relative_to: Path) -> list[FabricationHit]:
    """Every flagged site under ``roots``, sorted and deduplicated by row key."""
    found: dict[str, FabricationHit] = {}
    for root in roots:
        if not root.exists():
            continue
        for path in sorted(root.rglob("*.py")):
            for hit in scan_file(path, relative_to=relative_to):
                found.setdefault(hit.key, hit)
    return sorted(found.values(), key=lambda h: (h.file, h.symbol, h.field))
