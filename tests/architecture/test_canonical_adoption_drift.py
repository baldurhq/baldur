"""G55/G56/G57/G73 — canonical-module adoption may not re-drift (OSS halves).

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

By construction the scanners do NOT flag: docstrings and comments (invisible
to the AST scan — markdown bold like ``**counter's**`` never parses as a
power, and prose mentions of a header or module name are not code
constants), non-attempt exponent math (``std ** 2``), growth-with-cap on
non-delay state (an adaptive rate multiplier), HTTP-style header names
(``"X-Forwarded-For"``), ``django.utils.timezone`` imports, and a private
module path merely *named* as data (a registry slot-factory target).

ENFORCED-EMPTY: there is no baseline budget. A new inline backoff triad,
forwarded-header read, parallel now-module reference, or re-inlined tier probe
is migrated to compose the canonical, never baselined.

Architectural fitness function rule registry:
``ARCHITECTURE.md#g55-backoff-primitive-drift`` /
``ARCHITECTURE.md#g56-client-ip-extraction-drift`` /
``ARCHITECTURE.md#g57-time-source-drift`` /
``ARCHITECTURE.md#g73-pro-probe-drift``
"""

from __future__ import annotations

import ast
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

    Matches a ``find_spec`` **call** whose first positional argument is the
    constant ``"baldur_pro"`` / ``"baldur_dormant"`` — both the bare
    ``find_spec(...)`` and the dotted ``importlib.util.find_spec(...)`` forms.
    Scanning the *idiom* rather than the bare module string is deliberate: that
    string legitimately appears in registry slot-factory tables and reset maps
    in dozens of places, so a literal scan would be all false positives and get
    baselined into inertness.
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
        if name != "find_spec" or not node.args:
            continue
        first = node.args[0]
        if (
            isinstance(first, ast.Constant)
            and isinstance(first.value, str)
            and first.value in _PRIVATE_DISTRIBUTIONS
        ):
            hits.add((node.lineno, f"find_spec-{first.value}"))
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

    def test_unparseable_source_returns_empty(self):
        assert scan_pro_probe_source("def f(:\n") == []


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
    "TestClientIpAdoptionDrift",
    "TestG55Scanner",
    "TestG56Scanner",
    "TestG57Scanner",
    "TestG73Scanner",
    "TestProProbeAdoptionDrift",
    "TestScanTree",
    "TestTimeSourceAdoptionDrift",
    "scan_backoff_source",
    "scan_client_ip_source",
    "scan_pro_probe_source",
    "scan_timezone_source",
    "scan_tree",
]
