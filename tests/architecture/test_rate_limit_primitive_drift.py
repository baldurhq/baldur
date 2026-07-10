"""G53 — hand-rolled rate-limit window/bucket idioms may not re-drift.

The two in-process rate-limiting algorithms — an exact-timestamp sliding window
and a token bucket — are consolidated into one shared primitives module
(``baldur.core.rate_limiting``). Every in-process window/bucket policy composes
those primitives; a hand-rolled copy of either idiom outside the primitives
module re-opens the duplication this consolidation removed: a boundary or refill
fix would again have to land in N places and silently drift if one copy is missed.

The copies are invisible to every other fitness gate: they do not import each
other (the acyclic import-graph gate stays green), each is locally small and
clean (ruff / complexity green), and each lives in the correct tier. So this gate
scans for the two bespoke idioms by AST and fails on any occurrence outside the
allowed primitives module.

Detected idioms:

1. **Float-time-anchored prune comprehension** — a list comprehension whose
   single ``Name`` iteration target is compared, in an ``if`` clause, against a
   cutoff derived (inline or by same-file dataflow) from a ``time.time()`` /
   ``time.monotonic()`` / ``.timestamp()`` call.
2. **Token-bucket refill triad** — a ``min(capacity, tokens + elapsed * rate)``
   refill expression over instance (``self.``) attributes.

By construction the scanner does NOT flag: datetime-element windows (the cutoff
is ``datetime`` / ``timedelta`` arithmetic with no float-time call), tuple-target
value windows (the comprehension target is not a single ``Name``), read-only
generator counts (a generator expression, not a list comprehension), and generic
non-time threshold filters. A general clone detector was rejected because the
hundreds of mandated cross-service ``try/except`` clones would drown the handful
of true positives; only a bespoke idiom gate clears the 0-false-positive bar.

ENFORCED-EMPTY: there is no baseline budget. A new hand-rolled window or refill
is migrated to compose the shared primitive, never baselined.

Architectural fitness function rule registry:
``ARCHITECTURE.md#g53-rate-limit-primitive-drift``
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from tests.architecture.conftest import PROJECT_ROOT

# The one module allowed to host the raw idioms (repo-relative, POSIX).
_ALLOWED_ORIGIN = "core/rate_limiting.py"
# Attribute names of a float-time source call: time.time(), time.monotonic(),
# <dt>.timestamp().
_TIME_ATTRS = frozenset({"time", "monotonic", "timestamp"})

_SRC_ROOT = PROJECT_ROOT / "src" / "baldur"


# ---------------------------------------------------------------------------
# Scanner (pure AST). Reused as the single source of truth by the gate below,
# by the private PRO-half gate (which points it at src/baldur_pro), and
# exercised directly on planted source strings by the scanner tests.
# ---------------------------------------------------------------------------


def _is_time_call(node: ast.AST) -> bool:
    """True for ``<x>.time()`` / ``.monotonic()`` / ``.timestamp()`` calls."""
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in _TIME_ATTRS
    )


def _has_time_call(node: ast.AST) -> bool:
    return any(_is_time_call(n) for n in ast.walk(node))


def _refs_any(node: ast.AST, names: set[str]) -> bool:
    return any(isinstance(n, ast.Name) and n.id in names for n in ast.walk(node))


def _time_anchored_names(tree: ast.AST) -> set[str]:
    """Names assigned from a float-time-derived expression (transitive fixpoint).

    ``now = time.time()`` anchors ``now``; ``window_start = now - w`` then anchors
    ``window_start`` because it references an already-anchored name. File-global
    (not per-scope): the over-approximation only ever adds recall, and the gate's
    enforced-empty state makes any spurious hit visible immediately.
    """
    names: set[str] = set()
    changed = True
    while changed:
        changed = False
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            if not (_has_time_call(node.value) or _refs_any(node.value, names)):
                continue
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id not in names:
                    names.add(target.id)
                    changed = True
    return names


def _is_prune_comp(node: ast.ListComp, time_names: set[str]) -> bool:
    """True for ``[ts for ts in <it> if ts <op> <time-anchored>]`` (single Name target)."""
    if len(node.generators) != 1:
        return False
    generator = node.generators[0]
    if not isinstance(generator.target, ast.Name):
        return False
    var = generator.target.id
    for cond in generator.ifs:
        if not isinstance(cond, ast.Compare):
            continue
        operands = [cond.left, *cond.comparators]
        if not any(isinstance(o, ast.Name) and o.id == var for o in operands):
            continue
        for operand in operands:
            if isinstance(operand, ast.Name) and operand.id == var:
                continue
            if _has_time_call(operand) or _refs_any(operand, time_names):
                return True
    return False


def _is_refill_min(node: ast.Call) -> bool:
    """True for ``min(<cap>, <tokens> + <elapsed> * <rate>)`` over a ``self.`` attribute."""
    if not (isinstance(node.func, ast.Name) and node.func.id == "min"):
        return False
    if len(node.args) != 2:
        return False
    second = node.args[1]
    if not (isinstance(second, ast.BinOp) and isinstance(second.op, ast.Add)):
        return False
    if not any(
        isinstance(side, ast.BinOp) and isinstance(side.op, ast.Mult)
        for side in (second.left, second.right)
    ):
        return False
    return any(
        isinstance(n, ast.Attribute)
        and isinstance(n.value, ast.Name)
        and n.value.id == "self"
        for n in ast.walk(node)
    )


def scan_source(source: str, filename: str = "<planted>") -> list[tuple[int, str]]:
    """Return ``(lineno, kind)`` idiom hits. Pure AST; imports no scanned package."""
    try:
        tree = ast.parse(source, filename=filename)
    except SyntaxError:
        return []
    time_names = _time_anchored_names(tree)
    hits: set[tuple[int, str]] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ListComp) and _is_prune_comp(node, time_names):
            hits.add((node.lineno, "prune-comprehension"))
        elif isinstance(node, ast.Call) and _is_refill_min(node):
            hits.add((node.lineno, "token-bucket-refill-triad"))
    return sorted(hits)


def scan_tree(root: Path, allowed_origin: str | None) -> list[tuple[Path, int, str]]:
    """Scan every ``*.py`` under ``root``; skip ``allowed_origin`` (repo-relative)."""
    out: list[tuple[Path, int, str]] = []
    if not root.exists():
        return out
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        if allowed_origin and path.relative_to(root).as_posix() == allowed_origin:
            continue
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for lineno, kind in scan_source(source, str(path)):
            out.append((path, lineno, kind))
    return out


def _format(hits: list[tuple[Path, int, str]]) -> str:
    return "\n".join(f"  {p}:{ln} — {kind}" for p, ln, kind in hits)


class TestRateLimitPrimitiveDrift:
    """G53 — no hand-rolled window/bucket idiom outside the primitives module."""

    def test_no_hand_rolled_window_or_bucket_outside_primitive(self):
        hits = scan_tree(_SRC_ROOT, _ALLOWED_ORIGIN)
        assert not hits, (
            f"G53: {len(hits)} hand-rolled rate-limit idiom(s) outside "
            f"{_ALLOWED_ORIGIN}. Compose baldur.core.rate_limiting "
            "(SlidingWindowCounter / TokenBucket) instead of re-implementing the "
            "prune comprehension or the token-bucket refill triad — a boundary or "
            "refill fix must land in one place, never N.\n" + _format(hits)
        )


class TestG53Scanner:
    """`scan_source` recognizes the two idioms and the by-construction exclusions."""

    @pytest.mark.parametrize(
        ("source", "expected", "note"),
        [
            pytest.param(
                "import time\n"
                "def f(x, w):\n"
                "    return [ts for ts in x if ts > time.time() - w]\n",
                1,
                "inline time.time() cutoff",
                id="prune-inline-time",
            ),
            pytest.param(
                "import time\n"
                "def f(x, w):\n"
                "    now = time.time()\n"
                "    window_start = now - w\n"
                "    return [ts for ts in x if ts > window_start]\n",
                1,
                "same-file dataflow cutoff",
                id="prune-dataflow",
            ),
            pytest.param(
                "def f(x, w):\n"
                "    cutoff = x.last.timestamp() - w\n"
                "    return [ts for ts in x.items if ts > cutoff]\n",
                1,
                ".timestamp() cutoff",
                id="prune-timestamp",
            ),
            pytest.param(
                "def refill(self, elapsed):\n"
                "    self._tokens = min(self._capacity, "
                "self._tokens + elapsed * self._rate)\n",
                1,
                "token-bucket refill triad",
                id="refill-triad",
            ),
            pytest.param(
                "from baldur.utils.time import utc_now\n"
                "from datetime import timedelta\n"
                "def f(x, w):\n"
                "    cutoff = utc_now() - timedelta(seconds=w)\n"
                "    return [ts for ts in x if ts > cutoff]\n",
                0,
                "datetime cutoff — no float-time call",
                id="neg-datetime-window",
            ),
            pytest.param(
                "import time\n"
                "def f(x, w):\n"
                "    return [(ts, v) for (ts, v) in x if ts > time.time() - w]\n",
                0,
                "tuple target is not a single Name",
                id="neg-tuple-target",
            ),
            pytest.param(
                "import time\n"
                "def f(x, w):\n"
                "    return sum(1 for ts in x if ts > time.time() - w)\n",
                0,
                "generator expression, not a list comprehension",
                id="neg-generator-count",
            ),
            pytest.param(
                "def f(items, threshold):\n"
                "    return [x for x in items if x > threshold]\n",
                0,
                "non-time threshold filter",
                id="neg-non-time-filter",
            ),
            pytest.param(
                "def scale(self, elapsed):\n"
                "    return min(self._cap, self._base + self._rate)\n",
                0,
                "min without the elapsed*rate multiply is not a refill triad",
                id="neg-min-no-mult",
            ),
            pytest.param(
                "def f(a, b, c, d):\n    return min(a, b + c * d)\n",
                0,
                "min(add(mult)) without a self attribute is not the refill idiom",
                id="neg-min-no-self",
            ),
        ],
    )
    def test_scan_source_flags_expected(self, source: str, expected: int, note: str):
        assert len(scan_source(source)) == expected, note

    def test_unparseable_source_returns_empty(self):
        assert scan_source("def f(:\n") == []

    def test_allowed_origin_is_skipped(self, tmp_path: Path):
        pkg = tmp_path / "core"
        pkg.mkdir()
        (pkg / "rate_limiting.py").write_text(
            "import time\n"
            "def f(x, w):\n"
            "    return [ts for ts in x if ts > time.time() - w]\n",
            encoding="utf-8",
        )
        assert scan_tree(tmp_path, _ALLOWED_ORIGIN) == []
        assert len(scan_tree(tmp_path, None)) == 1


__all__ = [
    "TestG53Scanner",
    "TestRateLimitPrimitiveDrift",
    "scan_source",
    "scan_tree",
]
