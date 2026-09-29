"""G55/G56/G57/G73/G88/G89/G90 — canonical-module adoption may not re-drift (OSS halves).

A recurring audit signature: a canonical implementation exists (exponential
backoff, client-IP extraction, the UTC time source), later code re-implemented
it inline instead of importing, and the copies drifted behaviorally —
jitterless backoff sitting exactly where retries storm, an X-Real-IP-only
proxy collapsing every client into one rate-limit bucket while audit resolves
the real client, and two parallel "now" modules. Each copy is locally clean
(ruff, import-cycle and tier gates all stay green), so the class is invisible
to every other fitness function; these gates scan for the bespoke idioms by
AST and fail on any occurrence outside the canonical module.

Detected idioms:

1. **G55 — inline exponential backoff** (canonical: ``baldur.core.backoff``):
   (a) an attempt-anchored power — ``<base> ** <exp>`` where the exponent
   references an attempt-like name (``attempt`` / ``retry`` / ``retries`` /
   ``resume`` / ``consecutive``); (b) a self-referential multiply-with-cap on
   a delay-like target — ``delay = min(delay * k, cap)`` (either argument
   order), where the rebound name is delay-like (``delay`` / ``backoff`` /
   ``interval`` / ``wait`` / ``sleep`` / ``cooldown``).
2. **G56 — quoted forwarded-header literal** (canonical:
   ``baldur.utils.network.extract_client_ip``): the WSGI META keys
   ``"HTTP_X_FORWARDED_FOR"`` / ``"HTTP_X_REAL_IP"`` as exact string
   constants anywhere outside the canonical module.
3. **G57 — parallel time-module reference** (canonical:
   ``baldur.utils.time.utc_now`` over the ``baldur.core.time_provider``
   seam): any import of, attribute access on, or non-docstring string
   reference to the retired ``baldur.core.timezone`` module.
4. **G73 — inline private-distribution presence probe** (canonical:
   ``baldur.utils.tier.is_pro_installed``): a ``find_spec`` call whose first
   positional argument is the constant ``"baldur_pro"`` / ``"baldur_dormant"``.
   Tier-resolved composition must key off one predicate, and tier simulation in
   tests must have one patch point; a re-inlined probe forks both.
5. **G88 — bare lock construction** (canonical:
   ``baldur.core.process_utils.fork_safe_lock`` / ``fork_safe_rlock``): a
   reference to ``threading.Lock`` / ``threading.RLock`` in a value position —
   the constructor call, or the callable passed as a value
   (``default_factory=threading.Lock``) — through any alias of the
   ``threading`` module or of a ``from threading import Lock / RLock`` name. A
   lock built outside the factory is not repaired in a fork child, so a parent
   thread holding it at the fork instant leaves the child blocked forever on
   its first acquisition.
6. **G89 — owner-check lock Lua** (canonical: the ``DistributedLock`` ABC in
   ``baldur.interfaces.cache_provider``, taken from ``cache.get_lock(name)``;
   its Redis implementation is the one module allowed to carry the script): a
   string constant holding ``redis.call("get", KEYS[1]) == ARGV[1]`` (or
   ``~=``, any quote style or case) — the owner check that makes a release or
   extend safe. Measured at landing, the same release script sat in six files.
7. **G90 — boolean text parse**: text compared against a ``"true"`` literal —
   membership in a literal tuple / list / set holding ``"true"``, or equality
   with ``"true"`` in any case. Measured at landing, 67 sites across the trees
   used at least four vocabularies (``"true"`` only; ``true/1``;
   ``true/1/yes``; ``true/1/yes/on``), so one operator value means true at one
   site and false at the next. No module hosts the idiom: an env-var boolean
   belongs in a settings field, and no shared parser exists for other text
   yet.

By construction the scanners do NOT flag: docstrings and comments (invisible
to the AST scan — markdown bold like ``**counter's**`` never parses as a
power, and prose mentions of a header or module name are not code
constants), non-attempt exponent math (``std ** 2``), growth-with-cap on
non-delay state (an adaptive rate multiplier), HTTP-style header names
(``"X-Forwarded-For"``), ``django.utils.timezone`` imports, a private
module path merely *named* as data (a registry slot-factory target), and a lock
type used as an annotation (``_lock: threading.Lock = fork_safe_lock()``,
``-> threading.RLock``) or another module's ``Lock`` (``asyncio.Lock()``,
``multiprocessing.Lock()``).

ENFORCED-EMPTY for G55/G56/G57/G73/G88: there is no baseline budget. A new
inline backoff triad, forwarded-header read, parallel now-module reference,
re-inlined tier probe, or bare lock is migrated to compose the canonical, never
baselined.

FROZEN BUDGET for G89/G90: the copies that existed at landing are counted, not
listed — an exact-match budget per gate (the clone-recurrence ratchet's
shape). A new copy fails; a migrated copy must lower the budget in the same
change, so freed slack cannot be reclaimed. Each copy migrates when the change
that next touches it lands, never in a standalone sweep. Residual: a change
that removes one copy and adds another nets zero and passes.

Architectural fitness function rule registry:
``ARCHITECTURE.md#g55-backoff-primitive-drift`` /
``ARCHITECTURE.md#g56-client-ip-extraction-drift`` /
``ARCHITECTURE.md#g57-time-source-drift`` /
``ARCHITECTURE.md#g73-pro-probe-drift`` /
``ARCHITECTURE.md#g88-fork-safe-lock-drift`` /
``ARCHITECTURE.md#g89-owner-lock-lua-drift`` /
``ARCHITECTURE.md#g90-boolean-text-parse-drift``
"""

from __future__ import annotations

import ast
import re
from collections.abc import Callable
from pathlib import Path

import pytest

from tests.architecture.conftest import PROJECT_ROOT

_SRC_ROOT = PROJECT_ROOT / "src" / "baldur"

# The one module allowed to host raw backoff math (repo-relative, POSIX).
_BACKOFF_ALLOWED_ORIGIN = "core/backoff.py"
# The one module allowed to read the forwarded-header META keys.
_CLIENT_IP_ALLOWED_ORIGIN = "utils/network.py"
# The retired module itself (inert once deleted; kept so the reference scan
# never self-flags a straggler checkout mid-migration).
_TIMEZONE_ALLOWED_ORIGIN = "core/timezone.py"

# Identifier fragments that mark an exponent as attempt-anchored.
_ATTEMPT_FRAGMENTS = ("attempt", "retry", "retries", "resume", "consecutive")
# Identifier fragments that mark a multiply-with-cap target as a delay.
_DELAY_FRAGMENTS = ("delay", "backoff", "interval", "wait", "sleep", "cooldown")

_FORWARDED_HEADER_LITERALS = frozenset({"HTTP_X_FORWARDED_FOR", "HTTP_X_REAL_IP"})

# The one module allowed to probe for a private distribution's presence.
_TIER_PROBE_ALLOWED_ORIGIN = "utils/tier.py"

_PRIVATE_DISTRIBUTIONS = frozenset({"baldur_pro", "baldur_dormant"})

# The one module allowed to construct a raw threading lock (it registers each
# one for the fork-child repair).
_LOCK_FACTORY_ALLOWED_ORIGIN = "core/process_utils.py"

_THREADING_LOCK_NAMES = frozenset({"Lock", "RLock"})

# The one module allowed to carry the owner-check lock Lua: the Redis
# implementation of the DistributedLock ABC.
_OWNER_LOCK_LUA_ALLOWED_ORIGIN = "adapters/cache/redis_adapter.py"
# Read the key and compare it with the caller's token — ``==`` in a
# compare-and-act script, ``~=`` in an early-return one.
_OWNER_CHECK_LUA = re.compile(
    r"redis\.call\(\s*['\"]get['\"]\s*,\s*KEYS\[1\]\s*\)\s*(?:==|~=)\s*ARGV\[1\]",
    re.IGNORECASE,
)
# Frozen copies outside the canonical at landing: the canary rollout store
# (release + extend) and the hash-chain merge and shard locks.
_OWNER_LOCK_LUA_BUDGET = 4

# Frozen hand-rolled boolean text parses under src/baldur at landing.
_BOOLEAN_TEXT_BUDGET = 56


# ---------------------------------------------------------------------------
# Scanners (pure AST). Reused as the single source of truth by the gates
# below, by the private PRO-half gates (which point them at the private source
# trees), and exercised directly on planted source strings by the scanner
# tests.
# ---------------------------------------------------------------------------


def _ident_strings(node: ast.AST):
    """Yield every Name id / Attribute attr identifier inside ``node``."""
    for n in ast.walk(node):
        if isinstance(n, ast.Name):
            yield n.id
        elif isinstance(n, ast.Attribute):
            yield n.attr


def _refs_fragment(node: ast.AST, fragments: tuple[str, ...]) -> bool:
    return any(
        fragment in ident.lower()
        for ident in _ident_strings(node)
        for fragment in fragments
    )


def _is_attempt_pow(node: ast.BinOp) -> bool:
    """True for ``<base> ** <exp>`` with an attempt-like name in the exponent."""
    return isinstance(node.op, ast.Pow) and _refs_fragment(
        node.right, _ATTEMPT_FRAGMENTS
    )


def _same_ref(a: ast.AST, b: ast.AST) -> bool:
    """Structural equality for Name / dotted-Attribute references."""
    if isinstance(a, ast.Name) and isinstance(b, ast.Name):
        return a.id == b.id
    if isinstance(a, ast.Attribute) and isinstance(b, ast.Attribute):
        return a.attr == b.attr and _same_ref(a.value, b.value)
    return False


def _target_ident(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _is_delay_mult_cap(targets: list[ast.expr], value: ast.expr) -> bool:
    """True for ``delay = min(delay * k, cap)`` (either arg order) on a
    delay-like target."""
    if not (
        isinstance(value, ast.Call)
        and isinstance(value.func, ast.Name)
        and value.func.id == "min"
        and len(value.args) == 2
    ):
        return False
    for target in targets:
        ident = _target_ident(target)
        if ident is None or not any(f in ident.lower() for f in _DELAY_FRAGMENTS):
            continue
        for arg in value.args:
            if isinstance(arg, ast.BinOp) and isinstance(arg.op, ast.Mult):
                if _same_ref(arg.left, target) or _same_ref(arg.right, target):
                    return True
    return False


def scan_backoff_source(
    source: str, filename: str = "<planted>"
) -> list[tuple[int, str]]:
    """Return ``(lineno, kind)`` inline-backoff hits. Pure AST."""
    try:
        tree = ast.parse(source, filename=filename)
    except SyntaxError:
        return []
    hits: set[tuple[int, str]] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.BinOp) and _is_attempt_pow(node):
            hits.add((node.lineno, "attempt-anchored-pow"))
        elif isinstance(node, ast.Assign) and _is_delay_mult_cap(
            node.targets, node.value
        ):
            hits.add((node.lineno, "delay-multiply-with-cap"))
        elif (
            isinstance(node, ast.AnnAssign)
            and node.value is not None
            and _is_delay_mult_cap([node.target], node.value)
        ):
            hits.add((node.lineno, "delay-multiply-with-cap"))
    return sorted(hits)


def scan_client_ip_source(
    source: str, filename: str = "<planted>"
) -> list[tuple[int, str]]:
    """Return ``(lineno, kind)`` forwarded-header literal hits.

    Exact string-constant equality: a docstring merely *mentioning* the META
    key is a longer string and never matches.
    """
    try:
        tree = ast.parse(source, filename=filename)
    except SyntaxError:
        return []
    hits: set[tuple[int, str]] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node.value in _FORWARDED_HEADER_LITERALS
        ):
            hits.add((node.lineno, node.value))
    return sorted(hits)


def _docstring_constants(tree: ast.AST) -> set[int]:
    """``id()`` of every docstring Constant node (module / class / function)."""
    out: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                out.add(id(body[0].value))
    return out


def scan_timezone_source(
    source: str, filename: str = "<planted>"
) -> list[tuple[int, str]]:
    """Return ``(lineno, kind)`` ``baldur.core.timezone`` reference hits.

    Flags imports (absolute and relative), dotted attribute access
    (``baldur.core.timezone.now``), and non-docstring string constants (patch
    targets like ``"baldur.core.timezone.now"``). Docstrings are excluded so
    prose may mention the retired module.
    """
    try:
        tree = ast.parse(source, filename=filename)
    except SyntaxError:
        return []
    docstrings = _docstring_constants(tree)
    hits: set[tuple[int, str]] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module == "core.timezone" or module.endswith(".core.timezone"):
                hits.add((node.lineno, "core-timezone-import"))
            elif (module == "core" or module.endswith(".core")) and any(
                alias.name == "timezone" for alias in node.names
            ):
                hits.add((node.lineno, "core-timezone-import"))
        elif isinstance(node, ast.Import):
            if any(
                alias.name == "core.timezone" or alias.name.endswith(".core.timezone")
                for alias in node.names
            ):
                hits.add((node.lineno, "core-timezone-import"))
        elif isinstance(node, ast.Attribute) and node.attr == "timezone":
            value = node.value
            if (isinstance(value, ast.Attribute) and value.attr == "core") or (
                isinstance(value, ast.Name) and value.id == "core"
            ):
                hits.add((node.lineno, "core-timezone-attribute"))
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and "core.timezone" in node.value
            and id(node) not in docstrings
        ):
            hits.add((node.lineno, "core-timezone-string"))
    return sorted(hits)


def scan_pro_probe_source(
    source: str, filename: str = "<planted>"
) -> list[tuple[int, str]]:
    """Return ``(lineno, kind)`` inline private-distribution presence probes.

    Matches a ``find_spec`` **call** whose module argument is the constant
    ``"baldur_pro"`` / ``"baldur_dormant"`` — the bare ``find_spec(...)``, the
    dotted ``importlib.util.find_spec(...)`` and the keyword
    ``find_spec(name=...)`` forms. Scanning the *idiom* rather than the bare
    module string is deliberate: that string legitimately appears in registry
    slot-factory tables and reset maps in dozens of places, so a literal scan
    would be all false positives and get baselined into inertness.

    Boundary (deliberate, documented rather than closed): the module argument
    must be a literal and the callee must still be named ``find_spec``, so a
    probe built through a variable (``find_spec(_PRIVATE)``) or an aliased
    import (``from importlib.util import find_spec as _probe``) is not matched.
    Closing those needs constant propagation / alias tracking, and neither is
    the drift signature this gate exists to catch — which is a copy-paste of
    the canonical one-liner.
    """
    try:
        tree = ast.parse(source, filename=filename)
    except SyntaxError:
        return []
    hits: set[tuple[int, str]] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else None
        if name is None and isinstance(func, ast.Name):
            name = func.id
        if name != "find_spec":
            continue
        first = next(
            (kw.value for kw in node.keywords if kw.arg == "name"),
            node.args[0] if node.args else None,
        )
        if first is None:
            continue
        if (
            isinstance(first, ast.Constant)
            and isinstance(first.value, str)
            and first.value in _PRIVATE_DISTRIBUTIONS
        ):
            hits.add((node.lineno, f"find_spec-{first.value}"))
    return sorted(hits)


def _annotation_node_ids(tree: ast.AST) -> set[int]:
    """Return the ids of every node inside an annotation subtree.

    Annotations name the lock *type* of a slot the factory fills
    (``_lock: threading.Lock = fork_safe_lock()``, ``-> threading.RLock``); they
    construct nothing and stay after a conversion, so they are not value
    positions. String annotations sit inside the same subtrees.
    """
    roots: list[ast.AST] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.AnnAssign):
            roots.append(node.annotation)
        elif isinstance(node, ast.arg) and node.annotation is not None:
            roots.append(node.annotation)
        elif (
            isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
            and node.returns is not None
        ):
            roots.append(node.returns)
    return {id(n) for root in roots for n in ast.walk(root)}


def scan_bare_lock_source(
    source: str, filename: str = "<planted>"
) -> list[tuple[int, str]]:
    """Return ``(lineno, kind)`` bare ``threading.Lock`` / ``RLock`` references.

    Tracks every alias the module binds — ``import threading [as t]`` and
    ``from threading import Lock | RLock [as L]`` — and flags a load of the
    lock callable through one of them anywhere outside an annotation: the
    constructor call (``kind`` ends in ``-call``) and the callable handed on as
    a value, such as a dataclass ``default_factory`` (``-value``).
    """
    try:
        tree = ast.parse(source, filename=filename)
    except SyntaxError:
        return []
    threading_aliases: set[str] = set()
    lock_aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "threading":
                    threading_aliases.add(alias.asname or "threading")
        elif (
            isinstance(node, ast.ImportFrom)
            and node.module == "threading"
            and node.level == 0
        ):
            for alias in node.names:
                if alias.name in _THREADING_LOCK_NAMES:
                    lock_aliases[alias.asname or alias.name] = alias.name
    if not threading_aliases and not lock_aliases:
        return []
    skipped = _annotation_node_ids(tree)
    call_funcs = {
        id(node.func) for node in ast.walk(tree) if isinstance(node, ast.Call)
    }
    hits: set[tuple[int, str]] = set()
    for node in ast.walk(tree):
        if id(node) in skipped:
            continue
        name = None
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.ctx, ast.Load)
            and isinstance(node.value, ast.Name)
            and node.value.id in threading_aliases
            and node.attr in _THREADING_LOCK_NAMES
        ):
            name = node.attr
        elif (
            isinstance(node, ast.Name)
            and isinstance(node.ctx, ast.Load)
            and node.id in lock_aliases
        ):
            name = lock_aliases[node.id]
        if name is None:
            continue
        shape = "call" if id(node) in call_funcs else "value"
        hits.add((node.lineno, f"threading-{name}-{shape}"))
    return sorted(hits)


def scan_owner_lock_lua_source(
    source: str, filename: str = "<planted>"
) -> list[tuple[int, str]]:
    """Return ``(lineno, kind)`` hits for the owner-check Lua in a string constant.

    One hit per occurrence, so a script carrying both a release and an extend
    branch counts twice. Docstrings are skipped — prose describing the script
    is not a copy of it.
    """
    try:
        tree = ast.parse(source, filename=filename)
    except SyntaxError:
        return []
    docstrings = _docstring_constants(tree)
    hits: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
        ):
            hits.extend(
                (node.lineno, "owner-check-lua")
                for _ in _OWNER_CHECK_LUA.finditer(node.value)
            )
    return sorted(hits)


def _is_true_text(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value.strip().lower() == "true"
    )


def scan_boolean_text_source(
    source: str, filename: str = "<planted>"
) -> list[tuple[int, str]]:
    """Return ``(lineno, kind)`` hits for text compared against a ``"true"`` literal.

    Two shapes, one hit per comparison: membership in a literal tuple / list /
    set that holds ``"true"`` (``v.lower() in ("true", "1")``), and equality or
    inequality with ``"true"`` in any case (``v.upper() == "TRUE"``). A parse
    whose vocabulary omits ``"true"`` (``v in {"yes", "on"}``) is not seen.
    """
    try:
        tree = ast.parse(source, filename=filename)
    except SyntaxError:
        return []
    hits: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        operands = [node.left, *node.comparators]
        for op, left, right in zip(node.ops, operands[:-1], operands[1:], strict=True):
            if (
                isinstance(op, (ast.In, ast.NotIn))
                and isinstance(right, (ast.Tuple, ast.List, ast.Set))
                and any(_is_true_text(element) for element in right.elts)
            ):
                hits.append((node.lineno, "true-in-literal-set"))
            elif isinstance(op, (ast.Eq, ast.NotEq)) and (
                _is_true_text(left) or _is_true_text(right)
            ):
                hits.append((node.lineno, "compare-to-true-text"))
    return sorted(hits)


def scan_tree(
    root: Path,
    scan: Callable[[str, str], list[tuple[int, str]]],
    allowed_origin: str | None,
) -> list[tuple[Path, int, str]]:
    """Run ``scan`` on every ``*.py`` under ``root``; skip ``allowed_origin``
    (repo-relative POSIX)."""
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
        for lineno, kind in scan(source, str(path)):
            out.append((path, lineno, kind))
    return out


def _format(hits: list[tuple[Path, int, str]]) -> str:
    return "\n".join(f"  {p}:{ln} — {kind}" for p, ln, kind in hits)


def budget_verdict(
    hits: list[tuple[Path, int, str]], budget: int, *, gate: str, remedy: str
) -> str | None:
    """Failure text when the live count differs from a frozen budget, else ``None``.

    Every live site is listed either way: the count cannot say which copy
    moved, but the author of the change finds their own by path.
    """
    if len(hits) > budget:
        return (
            f"{gate}: {len(hits)} copies > frozen budget {budget} — a new copy "
            f"landed. {remedy}\n" + _format(hits)
        )
    if len(hits) < budget:
        return (
            f"{gate}: {len(hits)} copies < frozen budget {budget} — a copy was "
            f"migrated; lower the budget to {len(hits)} in the same change so "
            "the slack cannot be reclaimed.\n" + _format(hits)
        )
    return None


# ---------------------------------------------------------------------------
# Gates (enforced-empty over src/baldur/**).
# ---------------------------------------------------------------------------


class TestBackoffAdoptionDrift:
    """G55 — no inline exponential-backoff idiom outside the canonical module."""

    def test_no_inline_backoff_outside_canonical(self):
        hits = scan_tree(_SRC_ROOT, scan_backoff_source, _BACKOFF_ALLOWED_ORIGIN)
        assert not hits, (
            f"G55: {len(hits)} inline exponential-backoff idiom(s) outside "
            f"{_BACKOFF_ALLOWED_ORIGIN}. Compose baldur.core.backoff "
            "(ExponentialBackoff / BackoffStrategy.delays) instead of "
            "re-implementing the power curve or the multiply-with-cap loop — a "
            "jitter or cap fix must land in one place, never N.\n" + _format(hits)
        )


class TestClientIpAdoptionDrift:
    """G56 — no quoted forwarded-header literal outside the canonical module."""

    def test_no_forwarded_header_literal_outside_canonical(self):
        hits = scan_tree(_SRC_ROOT, scan_client_ip_source, _CLIENT_IP_ALLOWED_ORIGIN)
        assert not hits, (
            f"G56: {len(hits)} forwarded-header literal(s) outside "
            f"{_CLIENT_IP_ALLOWED_ORIGIN}. Call baldur.utils.network."
            "extract_client_ip (or extract_client_ip_from_headers) instead of "
            "reading X-Forwarded-For / X-Real-IP by hand — a header-precedence "
            "fix must land in one place, and enforcement must key the same "
            "client identity audit resolves.\n" + _format(hits)
        )


class TestTimeSourceAdoptionDrift:
    """G57 — no reference to the retired ``baldur.core.timezone`` module."""

    def test_module_file_does_not_reappear(self):
        assert not (_SRC_ROOT / "core" / "timezone.py").exists(), (
            "G57: core/timezone.py reappeared — the retired parallel "
            "now-module must not return; baldur.utils.time.utc_now is the "
            "single time source"
        )

    def test_no_core_timezone_reference(self):
        hits = scan_tree(_SRC_ROOT, scan_timezone_source, _TIMEZONE_ALLOWED_ORIGIN)
        assert not hits, (
            f"G57: {len(hits)} reference(s) to the retired baldur.core.timezone "
            "module. Use baldur.utils.time.utc_now (TimeProvider-aware) — a "
            "second now-module forks the clock source.\n" + _format(hits)
        )


class TestProProbeAdoptionDrift:
    """G73 — no inline private-distribution presence probe outside the helper."""

    def test_no_inline_pro_probe_outside_canonical(self):
        hits = scan_tree(_SRC_ROOT, scan_pro_probe_source, _TIER_PROBE_ALLOWED_ORIGIN)
        assert not hits, (
            f"G73: {len(hits)} inline private-distribution presence probe(s) "
            f"outside {_TIER_PROBE_ALLOWED_ORIGIN}. Call "
            "baldur.utils.tier.is_pro_installed instead of re-inlining "
            "find_spec — tier composition must key off one predicate, and "
            "tests must have one patch point for tier simulation.\n" + _format(hits)
        )


class TestForkSafeLockAdoptionDrift:
    """G88 — no bare ``threading.Lock`` / ``RLock`` outside the lock factory."""

    def test_no_bare_lock_outside_canonical(self):
        hits = scan_tree(_SRC_ROOT, scan_bare_lock_source, _LOCK_FACTORY_ALLOWED_ORIGIN)
        assert not hits, (
            f"G88: {len(hits)} bare threading.Lock / threading.RLock "
            f"reference(s) outside {_LOCK_FACTORY_ALLOWED_ORIGIN}. Construct "
            "the lock with baldur.core.process_utils.fork_safe_lock() / "
            "fork_safe_rlock() — a lock built elsewhere is not repaired in a "
            "fork child, which then blocks forever on it if a parent thread "
            "held it at the fork instant.\n" + _format(hits)
        )


# ---------------------------------------------------------------------------
# Gates (frozen budget over src/baldur/**).
# ---------------------------------------------------------------------------


class TestOwnerLockLuaAdoptionDrift:
    """G89 — the owner-check lock Lua may not be re-typed outside the lock adapter."""

    def test_owner_lock_lua_matches_frozen_budget(self):
        hits = scan_tree(
            _SRC_ROOT, scan_owner_lock_lua_source, _OWNER_LOCK_LUA_ALLOWED_ORIGIN
        )
        verdict = budget_verdict(
            hits,
            _OWNER_LOCK_LUA_BUDGET,
            gate="G89",
            remedy=(
                "Take the lock from the cache provider — cache.get_lock(name) "
                "returns a DistributedLock whose release() / extend() already "
                "carry the owner check — instead of re-typing the Lua; a fix to "
                "the check must land in one place, never N."
            ),
        )
        assert verdict is None, verdict


class TestBooleanTextParseDrift:
    """G90 — hand-rolled ``"true"``-text boolean parsing may not grow."""

    def test_boolean_text_parse_matches_frozen_budget(self):
        hits = scan_tree(_SRC_ROOT, scan_boolean_text_source, None)
        verdict = budget_verdict(
            hits,
            _BOOLEAN_TEXT_BUDGET,
            gate="G90",
            remedy=(
                "An env-var boolean belongs in a settings field (pydantic "
                "accepts true/false, 1/0, yes/no, on/off). For any other text "
                "the family is past the rule of three with four vocabularies in "
                "use — extract one shared parser instead of adding a copy."
            ),
        )
        assert verdict is None, verdict


# ---------------------------------------------------------------------------
# Scanner self-tests (planted positives / negatives — anti-silent-pass).
# ---------------------------------------------------------------------------


class TestG55Scanner:
    """`scan_backoff_source` flags the two idioms and honors the exclusions."""

    @pytest.mark.parametrize(
        ("source", "expected", "note"),
        [
            pytest.param(
                "import time\ndef f(attempt):\n    time.sleep(0.1 * (2 ** attempt))\n",
                1,
                "attempt-anchored power",
                id="pow-attempt",
            ),
            pytest.param(
                "def f(base, retry_count):\n    return base ** retry_count\n",
                1,
                "retry-anchored power",
                id="pow-retry-count",
            ),
            pytest.param(
                "def f(resume_count):\n    return min(30 * (2 ** resume_count), 300)\n",
                1,
                "resume-anchored power",
                id="pow-resume",
            ),
            pytest.param(
                "def f(cfg, state):\n"
                "    return cfg.base * (cfg.mult ** state.consecutive_429s)\n",
                1,
                "consecutive-anchored power via attributes",
                id="pow-consecutive-attr",
            ),
            pytest.param(
                "def f(delay, cap):\n"
                "    while True:\n"
                "        delay = min(delay * 2, cap)\n",
                1,
                "self-referential multiply-with-cap on a delay",
                id="mult-cap-delay",
            ),
            pytest.param(
                "def f(self):\n"
                "    self._backoff = min(self._max, self._backoff * self._mult)\n",
                1,
                "attribute target, reversed min args",
                id="mult-cap-attr-reversed",
            ),
            pytest.param(
                "def f(std):\n    return std ** 2\n",
                0,
                "non-attempt exponent math",
                id="neg-pow-math",
            ),
            pytest.param(
                "def f(self):\n"
                "    self._rate_multiplier = min(2.0, self._rate_multiplier * 1.1)\n",
                0,
                "growth-with-cap on non-delay state (adaptive rate)",
                id="neg-rate-multiplier",
            ),
            pytest.param(
                "def f(size, cap):\n    size = min(size * 2, cap)\n    return size\n",
                0,
                "multiply-with-cap on a non-delay name",
                id="neg-size-cap",
            ),
            pytest.param(
                "def f(delay, k, cap):\n    delay = min(delay + k, cap)\n",
                0,
                "additive growth is not the exponential idiom",
                id="neg-additive",
            ),
            pytest.param(
                'def f():\n    """delay = min(delay * 2, cap) shown in prose."""\n',
                0,
                "docstring text never parses as an assignment",
                id="neg-docstring-prose",
            ),
            pytest.param(
                "def f(a, attempt):\n    return a * attempt\n",
                0,
                "plain multiply is neither idiom",
                id="neg-plain-mult",
            ),
        ],
    )
    def test_scan_flags_expected(self, source: str, expected: int, note: str):
        assert len(scan_backoff_source(source)) == expected, note

    def test_unparseable_source_returns_empty(self):
        assert scan_backoff_source("def f(:\n") == []


class TestG56Scanner:
    """`scan_client_ip_source` flags exact META-key literals only."""

    @pytest.mark.parametrize(
        ("source", "expected", "note"),
        [
            pytest.param(
                'def f(request):\n    return request.META.get("HTTP_X_FORWARDED_FOR")\n',
                1,
                "XFF META read",
                id="xff-read",
            ),
            pytest.param(
                'HEADERS = {"HTTP_X_REAL_IP": "10.0.0.1"}\n',
                1,
                "X-Real-IP dict key",
                id="real-ip-key",
            ),
            pytest.param(
                'def f():\n    """Reads HTTP_X_REAL_IP then HTTP_X_FORWARDED_FOR."""\n',
                0,
                "docstring mention is not an exact literal",
                id="neg-docstring-mention",
            ),
            pytest.param(
                'def f(h):\n    return h.get("X-Forwarded-For")\n',
                0,
                "HTTP-style header name is out of scope",
                id="neg-http-style",
            ),
            pytest.param(
                'def f(request):\n    return request.META.get("REMOTE_ADDR")\n',
                0,
                "REMOTE_ADDR is not a forwarded header",
                id="neg-remote-addr",
            ),
        ],
    )
    def test_scan_flags_expected(self, source: str, expected: int, note: str):
        assert len(scan_client_ip_source(source)) == expected, note

    def test_unparseable_source_returns_empty(self):
        assert scan_client_ip_source("def f(:\n") == []


class TestG57Scanner:
    """`scan_timezone_source` flags imports / attributes / patch strings."""

    @pytest.mark.parametrize(
        ("source", "expected", "note"),
        [
            pytest.param(
                "from baldur.core.timezone import now\n",
                1,
                "absolute from-import",
                id="from-import",
            ),
            pytest.param(
                "import baldur.core.timezone\n",
                1,
                "plain import",
                id="plain-import",
            ),
            pytest.param(
                "from baldur.core import timezone\n",
                1,
                "parent-package from-import",
                id="parent-from-import",
            ),
            pytest.param(
                "from ..core.timezone import now\n",
                1,
                "relative from-import",
                id="relative-import",
            ),
            pytest.param(
                'def f(mocker):\n    mocker.patch("baldur.core.timezone.now")\n',
                1,
                "patch-target string constant",
                id="patch-string",
            ),
            pytest.param(
                "import baldur\ndef f():\n    return baldur.core.timezone.now()\n",
                1,
                "dotted attribute access",
                id="attribute-access",
            ),
            pytest.param(
                "from django.utils import timezone\n",
                0,
                "django.utils.timezone is a different module",
                id="neg-django-utils",
            ),
            pytest.param(
                "from baldur.core.time_provider import get_time_provider\n",
                0,
                "the TimeProvider seam is canonical",
                id="neg-time-provider",
            ),
            pytest.param(
                'def f():\n    """Formerly baldur.core.timezone; use utc_now."""\n',
                0,
                "docstring prose may mention the retired module",
                id="neg-docstring-mention",
            ),
            pytest.param(
                "from baldur.utils.time import utc_now\n",
                0,
                "the canonical import is allowed everywhere",
                id="neg-canonical-import",
            ),
        ],
    )
    def test_scan_flags_expected(self, source: str, expected: int, note: str):
        assert len(scan_timezone_source(source)) == expected, note

    def test_unparseable_source_returns_empty(self):
        assert scan_timezone_source("def f(:\n") == []


class TestG73Scanner:
    """`scan_pro_probe_source` flags find_spec calls on a private package."""

    @pytest.mark.parametrize(
        ("source", "expected", "note"),
        [
            pytest.param(
                "import importlib.util\n"
                "def f():\n"
                '    return importlib.util.find_spec("baldur_pro") is not None\n',
                1,
                "dotted find_spec call",
                id="dotted-call",
            ),
            pytest.param(
                "from importlib.util import find_spec\n"
                "def f():\n"
                '    return find_spec("baldur_dormant") is None\n',
                1,
                "bare find_spec call on the Dormant package",
                id="bare-call-dormant",
            ),
            pytest.param(
                "import importlib.util\n"
                "def f():\n"
                '    return importlib.util.find_spec(name="baldur_pro") is not None\n',
                1,
                "the keyword form is the same probe",
                id="keyword-call",
            ),
            pytest.param(
                "import importlib.util\n"
                "def f():\n"
                '    return importlib.util.find_spec("celery") is not None\n',
                0,
                "third-party probes are out of scope",
                id="neg-third-party",
            ),
            pytest.param(
                'SLOTS = {"dlq_service": "baldur_pro.services.dlq.base"}\n',
                0,
                "a registry slot-factory path is not a probe",
                id="neg-slot-table",
            ),
            pytest.param(
                'def f():\n    """Probes for baldur_pro via find_spec."""\n',
                0,
                "docstring prose is not a call",
                id="neg-docstring-mention",
            ),
            pytest.param(
                "from baldur.utils.tier import is_pro_installed\n"
                "def f():\n"
                "    return is_pro_installed()\n",
                0,
                "the canonical helper is allowed everywhere",
                id="neg-canonical-helper",
            ),
        ],
    )
    def test_scan_flags_expected(self, source: str, expected: int, note: str):
        assert len(scan_pro_probe_source(source)) == expected, note

    @pytest.mark.parametrize(
        ("source", "note"),
        [
            pytest.param(
                "import importlib.util\n"
                '_PRIVATE = "baldur_pro"\n'
                "def f():\n"
                "    return importlib.util.find_spec(_PRIVATE) is not None\n",
                "variable module argument — needs constant propagation",
                id="boundary-variable-arg",
            ),
            pytest.param(
                "from importlib.util import find_spec as _probe\n"
                "def f():\n"
                '    return _probe("baldur_pro") is not None\n',
                "aliased import — needs alias tracking",
                id="boundary-aliased-import",
            ),
        ],
    )
    def test_documented_boundary_is_not_matched(self, source: str, note: str):
        """Pin the scanner's documented blind spots so they stay deliberate.

        Neither form is the drift signature the gate exists to catch (a
        copy-paste of the canonical one-liner). This test fails if a future
        change closes one of them without updating the docstring and the rule
        registry, which is what keeps the gate's advertised reach honest.
        """
        assert scan_pro_probe_source(source) == [], note

    def test_unparseable_source_returns_empty(self):
        assert scan_pro_probe_source("def f(:\n") == []


class TestG88Scanner:
    """`scan_bare_lock_source` flags lock constructions outside the factory."""

    @pytest.mark.parametrize(
        ("source", "expected", "note"),
        [
            pytest.param(
                "import threading\n_lock = threading.Lock()\n",
                1,
                "module-level Lock constructor call",
                id="lock-call",
            ),
            pytest.param(
                "import threading\n"
                "class C:\n"
                "    def __init__(self):\n"
                "        self._lock = threading.RLock()\n",
                1,
                "RLock constructor call in __init__",
                id="rlock-call",
            ),
            pytest.param(
                "import threading as _threading\n_lock = _threading.Lock()\n",
                1,
                "an aliased threading module is the same constructor",
                id="aliased-module",
            ),
            pytest.param(
                "from threading import Lock\n_lock = Lock()\n",
                1,
                "a from-import of Lock is the same constructor",
                id="from-import",
            ),
            pytest.param(
                "from threading import RLock as _R\n_lock = _R()\n",
                1,
                "an aliased from-import of RLock is the same constructor",
                id="aliased-from-import",
            ),
            pytest.param(
                "import threading\n"
                "from dataclasses import dataclass, field\n"
                "@dataclass\n"
                "class C:\n"
                "    lock: object = field(default_factory=threading.Lock)\n",
                1,
                "the callable handed on as a value constructs a lock later",
                id="default-factory-value",
            ),
            pytest.param(
                "import threading\n"
                "from baldur.core.process_utils import fork_safe_lock\n"
                "_lock: threading.Lock = fork_safe_lock()\n",
                0,
                "the factory call is allowed; the annotation names a type",
                id="neg-annotated-factory",
            ),
            pytest.param(
                "import threading\ndef f() -> threading.RLock:\n    ...\n",
                0,
                "a return annotation constructs nothing",
                id="neg-return-annotation",
            ),
            pytest.param(
                "import threading\ndef f(lock: threading.Lock) -> None:\n    ...\n",
                0,
                "an argument annotation constructs nothing",
                id="neg-arg-annotation",
            ),
            pytest.param(
                "import asyncio\nimport multiprocessing\n"
                "a = asyncio.Lock()\nb = multiprocessing.Lock()\n",
                0,
                "other modules' Lock types are out of scope",
                id="neg-other-modules",
            ),
            pytest.param(
                'import threading\ndef f():\n    """Guarded by threading.Lock()."""\n',
                0,
                "docstring prose is not a call",
                id="neg-docstring-mention",
            ),
            pytest.param(
                "import threading\nt = threading.Thread(target=print)\n",
                0,
                "other threading callables are out of scope",
                id="neg-other-threading-callable",
            ),
        ],
    )
    def test_scan_flags_expected(self, source: str, expected: int, note: str):
        assert len(scan_bare_lock_source(source)) == expected, note

    def test_unparseable_source_returns_empty(self):
        assert scan_bare_lock_source("def f(:\n") == []


class TestG89Scanner:
    """`scan_owner_lock_lua_source` flags the owner-check Lua in code constants."""

    @pytest.mark.parametrize(
        ("source", "expected", "note"),
        [
            pytest.param(
                'RELEASE = """\n'
                'if redis.call("get", KEYS[1]) == ARGV[1] then\n'
                '    return redis.call("del", KEYS[1])\n'
                'end\n"""\n',
                1,
                "compare-and-delete release script",
                id="release",
            ),
            pytest.param(
                "S = \"if redis.call('GET',KEYS[1]) ~= ARGV[1] then return 0 end\"\n",
                1,
                "early-return form, single quotes, upper-case command",
                id="early-return",
            ),
            pytest.param(
                'S = """\n'
                'if redis.call("get", KEYS[1]) == ARGV[1] then return 1 end\n'
                'if redis.call("get", KEYS[1]) == ARGV[1] then return 2 end\n"""\n',
                2,
                "one script, two owner checks, counted twice",
                id="two-in-one",
            ),
            pytest.param(
                "def release():\n"
                '    """Runs redis.call("get", KEYS[1]) == ARGV[1] atomically."""\n',
                0,
                "a docstring describing the script is not a copy",
                id="neg-docstring",
            ),
            pytest.param(
                "S = \"return redis.call('get', KEYS[1])\"\n",
                0,
                "a plain read without the owner comparison",
                id="neg-plain-get",
            ),
        ],
    )
    def test_scan_flags_expected(self, source: str, expected: int, note: str):
        assert len(scan_owner_lock_lua_source(source)) == expected, note

    def test_unparseable_source_returns_empty(self):
        assert scan_owner_lock_lua_source("def f(:\n") == []


class TestG90Scanner:
    """`scan_boolean_text_source` flags text compared against a ``"true"`` literal."""

    @pytest.mark.parametrize(
        ("source", "expected", "note"),
        [
            pytest.param(
                'ok = raw.lower() in ("true", "1", "yes")\n',
                1,
                "membership in a literal tuple holding true",
                id="in-tuple",
            ),
            pytest.param(
                'ok = raw.strip().lower() in {"1", "true"}\n',
                1,
                "membership in a literal set",
                id="in-set",
            ),
            pytest.param(
                'ok = raw.upper() == "TRUE"\n',
                1,
                "equality with TRUE in upper case",
                id="eq-upper",
            ),
            pytest.param(
                'off = "true" != raw\n',
                1,
                "inequality with the literal on the left",
                id="ne-left",
            ),
            pytest.param(
                'ok = a == "true" or b in ("true", "on")\n',
                2,
                "two comparisons in one expression",
                id="two",
            ),
            pytest.param(
                'ok = raw in {"yes", "on"}\n',
                0,
                "a vocabulary without true is not seen (documented limit)",
                id="neg-no-true-literal",
            ),
            pytest.param(
                'ok = raw == "truest"\n',
                0,
                "a different word",
                id="neg-other-word",
            ),
        ],
    )
    def test_scan_flags_expected(self, source: str, expected: int, note: str):
        assert len(scan_boolean_text_source(source)) == expected, note

    def test_unparseable_source_returns_empty(self):
        assert scan_boolean_text_source("def f(:\n") == []


class TestBudgetVerdict:
    """`budget_verdict` fails in both directions and names every live site."""

    _HIT = (Path("pkg/a.py"), 3, "kind")

    def test_equal_count_passes(self):
        assert budget_verdict([self._HIT], 1, gate="GX", remedy="r") is None

    def test_growth_fails_and_lists_the_sites(self):
        verdict = budget_verdict([self._HIT, self._HIT], 1, gate="GX", remedy="r")
        assert verdict is not None
        assert "new copy" in verdict
        assert f"{self._HIT[0]}:3" in verdict

    def test_shrink_fails_and_names_the_new_budget(self):
        verdict = budget_verdict([], 1, gate="GX", remedy="r")
        assert verdict is not None
        assert "lower the budget to 0" in verdict


class TestScanTree:
    """`scan_tree` honors the allowed-origin skip for every scanner."""

    def test_allowed_origin_is_skipped(self, tmp_path: Path):
        pkg = tmp_path / "core"
        pkg.mkdir()
        (pkg / "backoff.py").write_text(
            "def f(attempt):\n    return 2 ** attempt\n", encoding="utf-8"
        )
        assert scan_tree(tmp_path, scan_backoff_source, _BACKOFF_ALLOWED_ORIGIN) == []
        assert len(scan_tree(tmp_path, scan_backoff_source, None)) == 1


__all__ = [
    "TestBackoffAdoptionDrift",
    "TestBooleanTextParseDrift",
    "TestBudgetVerdict",
    "TestClientIpAdoptionDrift",
    "TestForkSafeLockAdoptionDrift",
    "TestG55Scanner",
    "TestG56Scanner",
    "TestG57Scanner",
    "TestG73Scanner",
    "TestG88Scanner",
    "TestG89Scanner",
    "TestG90Scanner",
    "TestOwnerLockLuaAdoptionDrift",
    "TestProProbeAdoptionDrift",
    "TestScanTree",
    "TestTimeSourceAdoptionDrift",
    "budget_verdict",
    "scan_backoff_source",
    "scan_bare_lock_source",
    "scan_boolean_text_source",
    "scan_client_ip_source",
    "scan_owner_lock_lua_source",
    "scan_pro_probe_source",
    "scan_timezone_source",
    "scan_tree",
]
