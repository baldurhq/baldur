"""G65 — hand-rolled cooldown / debounce idioms may not re-drift.

The "suppress a repeat action within N seconds" mechanism — a locked
last-fired-timestamp map plus a ``now - last < cooldown`` compare — was forked
across the tree, each copy diverging on time representation, rollback semantics,
and eviction. It is now consolidated onto ``baldur.core.rate_limiting``'s
``CooldownGate``. A hand-rolled copy outside the primitive re-opens that
duplication: a cooldown-semantics fix would again land in one copy and miss the
siblings.

Like the rate-limit-primitive copies, cooldown forks are invisible to every
other fitness gate — they do not import each other, each is locally clean, and
each lives in the correct tier — so this gate scans for the idiom by AST.

Detected idiom — a ``Compare`` whose operand is an *elapsed* expression
``<now-like> - <map-get-sourced>`` (a direct ``Sub``, wrapped in
``.total_seconds()``, or via an assigned elapsed-name resolved per function
scope), gated by an *honest-name* signal: the ``.get`` receiver's name contains
``last_`` / ``cooldown``, OR another comparand's identifier tokens contain
``cooldown`` / ``debounce``. ``<now-like>`` is a float-time call
(``.time()`` / ``.monotonic()`` / ``.timestamp()``), a datetime-now call
(``utc_now()`` / ``.now()`` / ``.utcnow()``), or a name anchored to one.

The honest-name signal is load-bearing, not decoration: cooldown arithmetic is
structurally identical to TTL-cache / staleness / stuck checks, so a
structural-only detector cannot reach zero false positives — the name is the
only in-AST discriminator. Elapsed-name resolution is per-function-scope (a
file-global fixpoint empirically produced a false positive where a self-CB timer
reused the name ``elapsed`` in another function).

By construction the scanner does NOT flag: the absolute-deadline form
(``cooldown_until < now`` — no ``now - get`` subtraction), dishonestly-renamed
maps whose names carry no cooldown signal, and cross-file dataflow. This gate
does NOT import G53's scanner: the anchoring predicate differs (datetime-now
extension), and coupling would let a G53 edit silently shift G65's semantics.

ENFORCED-EMPTY: there is no baseline budget. The one allowed origin is the
primitive module; the one allowlisted fork is ``meta/escalation.py`` (a
settings-routed, per-component escalation gate whose authoritative dedup is a
cross-worker Redis SETNX layer — not a fork to fold). A new hand-rolled cooldown
is migrated to compose ``CooldownGate``, never baselined.

Architectural fitness function rule registry:
``ARCHITECTURE.md#g65-cooldown-gate-drift``
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from tests.architecture.conftest import PROJECT_ROOT

# The one module allowed to host the raw idiom (repo-relative, POSIX).
_ALLOWED_ORIGIN = "core/rate_limiting.py"
# Locked, non-fork sites allowlisted for the OSS half (repo-relative, POSIX).
_OSS_ALLOWED = (_ALLOWED_ORIGIN, "meta/escalation.py")

# Attribute names of a float-time source call and a datetime-now call, plus the
# bare ``utc_now()`` name-call.
_FLOAT_TIME_ATTRS = frozenset({"time", "monotonic", "timestamp"})
_DATETIME_NOW_ATTRS = frozenset({"now", "utcnow"})
_NOW_NAME_CALLS = frozenset({"utc_now"})

_SRC_ROOT = PROJECT_ROOT / "src" / "baldur"


# ---------------------------------------------------------------------------
# Scanner (pure AST, self-contained). Reused as the single source of truth by
# the private PRO-half gate (which points it at src/baldur_pro), and exercised
# directly on planted source strings by the scanner tests.
# ---------------------------------------------------------------------------


def _is_now_call(node: ast.AST) -> bool:
    """True for a float-time or datetime-now call, or a bare ``utc_now()``."""
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Attribute) and func.attr in (
        _FLOAT_TIME_ATTRS | _DATETIME_NOW_ATTRS
    ):
        return True
    return isinstance(func, ast.Name) and func.id in _NOW_NAME_CALLS


def _has_now_call(node: ast.AST) -> bool:
    return any(_is_now_call(n) for n in ast.walk(node))


def _refs_any(node: ast.AST, names: set[str]) -> bool:
    return any(isinstance(n, ast.Name) and n.id in names for n in ast.walk(node))


def _now_anchored_names(tree: ast.AST) -> set[str]:
    """Names assigned from a now-derived expression (file-global fixpoint).

    ``now = time.time()`` anchors ``now``; ``deadline = now - w`` then anchors
    ``deadline``. File-global is safe for the now-anchor (over-approximation
    only adds recall); only *elapsed-name* resolution is per-function.
    """
    names: set[str] = set()
    changed = True
    while changed:
        changed = False
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            if not (_has_now_call(node.value) or _refs_any(node.value, names)):
                continue
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id not in names:
                    names.add(target.id)
                    changed = True
    return names


def _is_get_call(node: ast.AST) -> bool:
    """True for a ``<receiver>.get(...)`` call."""
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "get"
    )


def _receiver_name(getcall: ast.Call) -> str:
    """Return the trailing name of a ``.get`` receiver (``a.b.get`` -> ``b``)."""
    recv = getcall.func.value  # type: ignore[attr-defined]
    if isinstance(recv, ast.Attribute):
        return recv.attr
    if isinstance(recv, ast.Name):
        return recv.id
    return ""


def _get_sourced_map(scope: ast.AST) -> dict[str, str]:
    """Map each name assigned from ``<recv>.get(...)`` to its receiver name."""
    out: dict[str, str] = {}
    for node in ast.walk(scope):
        if isinstance(node, ast.Assign) and _is_get_call(node.value):
            recv = _receiver_name(node.value)
            for target in node.targets:
                if isinstance(target, ast.Name):
                    out[target.id] = recv
    return out


def _elapsed_receiver(
    node: ast.AST, now_names: set[str], get_map: dict[str, str]
) -> str | None:
    """If ``node`` is ``<now-like> - <map-get>`` (optionally ``.total_seconds()``),
    return the map-get receiver name; else ``None``."""
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "total_seconds"
    ):
        node = node.func.value
    if not (isinstance(node, ast.BinOp) and isinstance(node.op, ast.Sub)):
        return None
    left, right = node.left, node.right
    if not (
        _is_now_call(left) or (isinstance(left, ast.Name) and left.id in now_names)
    ):
        return None
    if isinstance(right, ast.Name) and right.id in get_map:
        return get_map[right.id]
    if _is_get_call(right):
        return _receiver_name(right)
    return None


def _elapsed_names(
    scope: ast.AST, now_names: set[str], get_map: dict[str, str]
) -> dict[str, str]:
    """Per-function map: elapsed-name -> the map-get receiver it derived from."""
    out: dict[str, str] = {}
    for node in ast.walk(scope):
        if isinstance(node, ast.Assign):
            recv = _elapsed_receiver(node.value, now_names, get_map)
            if recv is not None:
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        out[target.id] = recv
    return out


def _identifier_tokens(node: ast.AST) -> set[str]:
    """Lowercased Name ids + Attribute attrs anywhere under ``node``."""
    tokens: set[str] = set()
    for inner in ast.walk(node):
        if isinstance(inner, ast.Name):
            tokens.add(inner.id.lower())
        elif isinstance(inner, ast.Attribute):
            tokens.add(inner.attr.lower())
    return tokens


def _is_honest_receiver(receiver: str) -> bool:
    lowered = receiver.lower()
    return "last_" in lowered or "cooldown" in lowered


def _is_cooldown_compare(
    cmp_node: ast.Compare,
    now_names: set[str],
    get_map: dict[str, str],
    elapsed_map: dict[str, str],
) -> bool:
    """True for an elapsed-vs-cooldown compare with the honest-name signal."""
    receiver: str | None = None
    for operand in [cmp_node.left, *cmp_node.comparators]:
        recv = _elapsed_receiver(operand, now_names, get_map)
        if recv is not None:
            receiver = recv
            break
        if isinstance(operand, ast.Name) and operand.id in elapsed_map:
            receiver = elapsed_map[operand.id]
            break
    if receiver is None:
        return False
    if _is_honest_receiver(receiver):
        return True
    tokens = _identifier_tokens(cmp_node)
    return any("cooldown" in token or "debounce" in token for token in tokens)


def scan_source(source: str, filename: str = "<planted>") -> list[tuple[int, str]]:
    """Return ``(lineno, kind)`` cooldown-idiom hits. Pure AST; imports nothing."""
    try:
        tree = ast.parse(source, filename=filename)
    except SyntaxError:
        return []
    now_names = _now_anchored_names(tree)
    hits: set[tuple[int, str]] = set()
    for scope in ast.walk(tree):
        if not isinstance(scope, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        get_map = _get_sourced_map(scope)
        elapsed_map = _elapsed_names(scope, now_names, get_map)
        for node in ast.walk(scope):
            if isinstance(node, ast.Compare) and _is_cooldown_compare(
                node, now_names, get_map, elapsed_map
            ):
                hits.add((node.lineno, "cooldown-compare"))
    return sorted(hits)


def scan_tree(root: Path, allowed: tuple[str, ...] = ()) -> list[tuple[Path, int, str]]:
    """Scan every ``*.py`` under ``root``; skip ``allowed`` (repo-relative POSIX)."""
    out: list[tuple[Path, int, str]] = []
    if not root.exists():
        return out
    allowed_set = set(allowed)
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        if path.relative_to(root).as_posix() in allowed_set:
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


class TestCooldownGateDrift:
    """G65 — no hand-rolled cooldown / debounce idiom outside the primitive."""

    def test_no_hand_rolled_cooldown_outside_primitive(self):
        hits = scan_tree(_SRC_ROOT, _OSS_ALLOWED)
        assert not hits, (
            f"G65: {len(hits)} hand-rolled cooldown idiom(s) outside "
            "baldur.core.rate_limiting. Compose CooldownGate (try_reserve / "
            "release / is_suppressed) instead of re-implementing the locked "
            "last-fired map + cooldown compare — a cooldown-semantics fix must "
            "land in one place, never N.\n" + _format(hits)
        )


class TestG65Scanner:
    """`scan_source` recognizes the idiom and the by-construction exclusions."""

    @pytest.mark.parametrize(
        ("source", "expected", "note"),
        [
            pytest.param(
                "import time\n"
                "def f(self, k):\n"
                "    last = self._last_alert_time.get(k, 0)\n"
                "    now = time.time()\n"
                "    return now - last < self._cooldown_seconds\n",
                1,
                "inline now - last < cooldown",
                id="pos-inline-sub",
            ),
            pytest.param(
                "from baldur.utils.time import utc_now\n"
                "def f(self, k):\n"
                "    lt = self._last_alert_times.get(k)\n"
                "    elapsed = (utc_now() - lt).total_seconds()\n"
                "    return elapsed >= self._cd\n",
                1,
                "assigned elapsed-name via .total_seconds()",
                id="pos-elapsed-name",
            ),
            pytest.param(
                "from baldur.utils.time import utc_now\n"
                "def f(self, k):\n"
                "    now = utc_now()\n"
                "    prev = self._last_alert_times.get(k)\n"
                "    return (now - prev).total_seconds() < self.cooldown\n",
                1,
                "inline .total_seconds() with utc_now anchor",
                id="pos-total-seconds-inline",
            ),
            pytest.param(
                "import time\n"
                "def f(self, k):\n"
                "    prev = self._emit_times.get(k, 0)\n"
                "    now = time.time()\n"
                "    return now - prev < self._debounce_window\n",
                1,
                "honest-name signal from a 'debounce' token (receiver clean)",
                id="pos-signal-b-debounce",
            ),
            pytest.param(
                "from datetime import datetime\n"
                "def f(self, k):\n"
                "    last = self._last_seen.get(k)\n"
                "    now = datetime.now()\n"
                "    return (now - last).total_seconds() < self.cooldown_seconds\n",
                1,
                "datetime.now() anchor",
                id="pos-datetime-now",
            ),
            pytest.param(
                "import time\n"
                "def f(self, k):\n"
                "    seen = self._seen_at.get(k, 0)\n"
                "    now = time.time()\n"
                "    return now - seen > self._stale_threshold\n",
                0,
                "TTL / staleness shape — no cooldown/debounce name signal",
                id="neg-ttl-cache",
            ),
            pytest.param(
                "import time\n"
                "def f(self, k):\n"
                "    cooldown_until = self._cooldown_until.get(k, 0)\n"
                "    return cooldown_until < time.time()\n",
                0,
                "absolute-deadline form — no now - get subtraction",
                id="neg-absolute-deadline",
            ),
            pytest.param(
                "import time\n"
                "def a(self, k):\n"
                "    last_cooldown = self._last_cooldown.get(k)\n"
                "    gap = time.time() - last_cooldown\n"
                "    return gap\n"
                "def b(self):\n"
                "    gap = self._cb_elapsed\n"
                "    return gap > self._max\n",
                0,
                "cross-function elapsed-name reuse must not bleed (per-scope)",
                id="neg-cross-function-bleed",
            ),
            pytest.param(
                "def f(items, threshold):\n"
                "    return any(x > threshold for x in items)\n",
                0,
                "non-time threshold filter",
                id="neg-non-time-filter",
            ),
        ],
    )
    def test_scan_source_flags_expected(self, source: str, expected: int, note: str):
        assert len(scan_source(source)) == expected, note

    def test_unparseable_source_returns_empty(self):
        assert scan_source("def f(:\n") == []

    def test_allowed_paths_are_skipped(self, tmp_path: Path):
        pkg = tmp_path / "core"
        pkg.mkdir()
        (pkg / "rate_limiting.py").write_text(
            "import time\n"
            "def f(self, k):\n"
            "    last = self._last_alert_time.get(k, 0)\n"
            "    now = time.time()\n"
            "    return now - last < self._cooldown_seconds\n",
            encoding="utf-8",
        )
        assert scan_tree(tmp_path, ("core/rate_limiting.py",)) == []
        assert len(scan_tree(tmp_path)) == 1


__all__ = [
    "TestCooldownGateDrift",
    "TestG65Scanner",
    "scan_source",
    "scan_tree",
]
