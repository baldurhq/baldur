"""G51 — OSS-core product code may not guard-then-use-None a baldur_pro symbol.

The "guard-then-use-None" anti-pattern: a name is imported from a gated package
(``baldur_pro`` / ``baldur_dormant``) under ``try/except ImportError`` and bound
to ``None`` in the handler, then USED unconditionally (called or
attribute-accessed) with no None-guard. PRO-absent that use raises
``TypeError``/``AttributeError`` — a fail-open violation (G1's own class), and one
no test/``/verify``/import-reachability check catches because the binding still
exists and the file still imports cleanly. It is the claim-wiring fail-open class.

This gate is the product-side counterpart to the test-side G50. It is enforced
on the **OSS-core surface only**, with a documented two-mechanism exclusion set
(659 D8 / Risk R2 — the accepted cost of bounding the "C/middle" scope to the
surface where a G1-style regression actually reaches the mirror via a legitimate
OSS code path):

  1. **PRO-feature entry-point directories** (``api/``, ``tasks/``,
     ``celery_tasks/``, ``adapters/celery/``) plus the single Category-A handler
     ``services/event_bus/bus/_emergency_postmortem.py`` — excluded by the
     centralized path list below. These are PRO-only entry points: PRO-absent
     they crash loudly (acceptable per D5; a loud crash is not a silent false
     guarantee) and their tests relocate, so a clean-error pass is an OOS chore,
     not on this gate's surface. The lone file is listed explicitly because it
     lives in an otherwise-OSS package and its crash is fail-open at the EventBus
     dispatch call site.
  2. **The in-tree PRO-shim file** ``resilience/policies/hedging.py`` — a PRO
     feature located in OSS only to break the 503 circular import. Its
     ``HedgingPolicy``/``AsyncHedgingPolicy.__init__`` raise ``RuntimeError`` when
     the PRO names are None, making every downstream use unreachable PRO-absent —
     a property a static gate cannot prove. It is excluded by an inline
     ``# pro-guard-exempt:`` pragma (placed adjacent to its None-binding block for
     locality, NOT a hardcoded filename here — the justification stays local to the
     code). Any occurrence of the pragma exempts the whole file: the scan is a
     file-level substring check, not G31's node-adjacency ``# verified-by:`` window,
     because the shim's dead-but-visible None sites span the module.

After D1 (``protect_facade.py``) + D4 (``presets.py`` clean-error), the enforced
surface is empty.

ENFORCED-EMPTY baseline: a baseline entry would whitelist a real PRO-absent crash
on the OSS-core surface — the exact regression this gate exists to block — so the
key has no valid use; the meta-assertion keeps it empty. Remediation is to guard
the site (or move the name inside the ``try``), never to baseline.

Architectural fitness function rule registry:
``ARCHITECTURE.md#g51-product-guard-then-use-none``
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from tests.architecture import _helpers as arch_helpers
from tests.architecture.conftest import (
    PROJECT_ROOT,
    collect_violations,
    load_baseline,
)

_RULE_KEY = "product_guard_then_use_none"
_RULE_ANCHOR = "#g51-product-guard-then-use-none"

_GATED: tuple[str, ...] = ("baldur_pro", "baldur_dormant")
_SRC_BALDUR = PROJECT_ROOT / "src" / "baldur"

# 659 D8 exclusion mechanism 1 — PRO-feature entry-point directories (paths are
# POSIX, relative to src/baldur/). Category A: loud crash PRO-absent is acceptable
# and their tests relocate (D5).
_EXCLUDED_DIR_PREFIXES: tuple[str, ...] = (
    "api/",
    "tasks/",
    "celery_tasks/",
    "adapters/celery/",
)
# A Category-A handler that lives in an otherwise-OSS package; its crash is
# fail-open at the EventBus dispatch call site (try/except Exception).
_EXCLUDED_FILES: frozenset[str] = frozenset(
    {"services/event_bus/bus/_emergency_postmortem.py"}
)
# 659 D8 exclusion mechanism 2 — inline pragma (adjacent-comment scan, G31 style).
_PRAGMA = "# pro-guard-exempt:"


# ---------------------------------------------------------------------------
# Scanner (pure AST). Reused as the single source of truth by the gate below
# and exercised directly on planted source strings by the scanner tests.
# ---------------------------------------------------------------------------


def _gated_import(node: ast.AST) -> bool:
    if isinstance(node, ast.ImportFrom):
        return (node.module or "").split(".", 1)[0] in _GATED
    if isinstance(node, ast.Import):
        return any(a.name.split(".", 1)[0] in _GATED for a in node.names)
    return False


def _handler_catches_import(handler: ast.ExceptHandler) -> bool:
    if handler.type is None:
        return True
    if isinstance(handler.type, ast.Name):
        return handler.type.id in ("ImportError", "ModuleNotFoundError", "Exception")
    if isinstance(handler.type, ast.Tuple):
        return any(
            isinstance(e, ast.Name)
            and e.id in ("ImportError", "ModuleNotFoundError", "Exception")
            for e in handler.type.elts
        )
    return False


def _none_bound_names_from_try(try_node: ast.Try) -> set[str]:
    """Names an ``except (ImportError|…)`` binds to ``None`` when the ``try``
    imports a gated top-level."""
    if not any(_gated_import(n) for stmt in try_node.body for n in ast.walk(stmt)):
        return set()
    names: set[str] = set()
    for handler in try_node.handlers:
        if not _handler_catches_import(handler):
            continue
        for stmt in handler.body:
            if (
                isinstance(stmt, ast.Assign)
                and isinstance(stmt.value, ast.Constant)
                and stmt.value.value is None
            ):
                names.update(t.id for t in stmt.targets if isinstance(t, ast.Name))
            elif (
                isinstance(stmt, ast.AnnAssign)
                and isinstance(stmt.value, ast.Constant)
                and stmt.value.value is None
                and isinstance(stmt.target, ast.Name)
            ):
                names.add(stmt.target.id)
    return names


def _iter_scope(node: ast.AST):
    """Yield ``node`` and descendants without crossing into a nested
    function/class scope (their bindings are local to that scope)."""
    yield node
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        yield from _iter_scope(child)


def _scope_local_none_names(body: list[ast.stmt]) -> set[str]:
    """None-bound gated names from ``try`` statements lexically in this scope's
    ``body`` — NOT inside any nested function/class (659 D8 module-scope-bleed fix:
    a module-level None name used inside a guarded function must be tracked under
    the function's own guards, not flagged wholesale at module scope)."""
    names: set[str] = set()
    for stmt in body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        for node in _iter_scope(stmt):
            if isinstance(node, ast.Try):
                names |= _none_bound_names_from_try(node)
    return names


def _neg_names(test: ast.AST, names: set[str]) -> set[str]:
    """Names asserted ``None`` by ``test`` (``x is None``; OR-chains union)."""
    out: set[str] = set()
    if (
        isinstance(test, ast.Compare)
        and isinstance(test.left, ast.Name)
        and test.left.id in names
        and any(isinstance(op, ast.Is) for op in test.ops)
        and any(
            isinstance(c, ast.Constant) and c.value is None for c in test.comparators
        )
    ):
        out.add(test.left.id)
    if (
        isinstance(test, ast.UnaryOp)
        and isinstance(test.op, ast.Not)
        and isinstance(test.operand, ast.Name)
        and test.operand.id in names
    ):
        out.add(test.operand.id)
    if isinstance(test, ast.BoolOp) and isinstance(test.op, ast.Or):
        for v in test.values:
            out |= _neg_names(v, names)
    return out


def _pos_names(test: ast.AST, names: set[str]) -> set[str]:
    """Names asserted not-None by ``test`` (``x``/``x is not None``; AND-chains union)."""
    out: set[str] = set()
    if isinstance(test, ast.Name) and test.id in names:
        out.add(test.id)
    if (
        isinstance(test, ast.Compare)
        and isinstance(test.left, ast.Name)
        and test.left.id in names
        and any(isinstance(op, ast.IsNot) for op in test.ops)
    ):
        out.add(test.left.id)
    if isinstance(test, ast.BoolOp) and isinstance(test.op, ast.And):
        for v in test.values:
            out |= _pos_names(v, names)
    return out


def _terminates(stmts: list[ast.stmt]) -> bool:
    return bool(stmts) and isinstance(
        stmts[-1], (ast.Raise, ast.Return, ast.Continue, ast.Break)
    )


def _protecting_try_lines(tree: ast.AST) -> set[int]:
    """Line numbers inside a ``try`` BODY whose handlers catch
    ImportError/Exception — the SOFT class: PRO-absent the gated import fails
    first or the except degrades, so the use is unreachable/handled (659 D3)."""
    soft: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Try) and any(
            _handler_catches_import(h) for h in node.handlers
        ):
            for stmt in node.body:
                for n in ast.walk(stmt):
                    lineno = getattr(n, "lineno", None)
                    if lineno is not None:
                        soft.add(lineno)
    return soft


def _scan_expr(
    node: ast.AST,
    names: set[str],
    proven: set[str],
    hits: list[tuple[int, str, str]],
) -> None:
    for n in ast.walk(node):
        if (
            isinstance(n, ast.Call)
            and isinstance(n.func, ast.Name)
            and n.func.id in names
            and n.func.id not in proven
        ):
            hits.append((n.lineno, n.func.id, "call"))
        if (
            isinstance(n, ast.Attribute)
            and isinstance(n.value, ast.Name)
            and n.value.id in names
            and n.value.id not in proven
        ):
            hits.append((n.lineno, n.value.id, f"attr .{n.attr}"))


def _walk(
    stmts: list[ast.stmt],
    names: set[str],
    proven: set[str],
    soft: set[int],
    hits: list[tuple[int, str, str]],
) -> None:
    """Walk a block (NOT a new scope) tracking ``proven`` not-None names."""
    proven = set(proven)
    for stmt in stmts:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            _scan_scope(stmt.body, names, soft, hits)
            continue
        if isinstance(stmt, ast.If):
            _scan_expr(stmt.test, names, proven, hits)
            neg = _neg_names(stmt.test, names)
            pos = _pos_names(stmt.test, names)
            _walk(stmt.body, names, proven | pos, soft, hits)
            _walk(stmt.orelse, names, proven | neg, soft, hits)
            if neg and _terminates(stmt.body):
                proven |= neg
            continue
        if isinstance(stmt, ast.Assign):
            _scan_expr(stmt.value, names, proven, hits)
            for tgt in stmt.targets:
                if isinstance(tgt, ast.Name) and tgt.id in names:
                    proven.add(tgt.id)
            continue
        if isinstance(stmt, ast.Try):
            _walk(stmt.body, names, proven, soft, hits)
            for handler in stmt.handlers:
                _walk(handler.body, names, proven, soft, hits)
            _walk(stmt.orelse, names, proven, soft, hits)
            _walk(stmt.finalbody, names, proven, soft, hits)
            continue
        if isinstance(stmt, (ast.For, ast.AsyncFor)):
            _scan_expr(stmt.iter, names, proven, hits)
            _walk(stmt.body, names, proven, soft, hits)
            _walk(stmt.orelse, names, proven, soft, hits)
            continue
        if isinstance(stmt, ast.While):
            _scan_expr(stmt.test, names, proven, hits)
            _walk(stmt.body, names, proven, soft, hits)
            _walk(stmt.orelse, names, proven, soft, hits)
            continue
        if isinstance(stmt, (ast.With, ast.AsyncWith)):
            for item in stmt.items:
                _scan_expr(item.context_expr, names, proven, hits)
            _walk(stmt.body, names, proven, soft, hits)
            continue
        _scan_expr(stmt, names, proven, hits)


def _scan_scope(
    body: list[ast.stmt],
    inherited: set[str],
    soft: set[int],
    hits: list[tuple[int, str, str]],
) -> None:
    """Enter a function/module/class scope: add its local None names to the
    inherited set, then walk its body tracking guards."""
    _walk(body, inherited | _scope_local_none_names(body), set(), soft, hits)


def scan_source(source: str, filename: str = "<planted>") -> list[tuple[int, str, str]]:
    """Return HARD ``(lineno, name, kind)`` guard-then-use-None hits. Pure AST;
    never imports a gated package, so it runs unchanged in the public mirror."""
    try:
        tree = ast.parse(source, filename=filename)
    except SyntaxError:
        return []
    soft = _protecting_try_lines(tree)
    hits: list[tuple[int, str, str]] = []
    _scan_scope(tree.body, set(), soft, hits)
    out: list[tuple[int, str, str]] = []
    seen: set[tuple[int, str, str]] = set()
    for lineno, name, kind in hits:
        if lineno in soft:  # SOFT exclusion (659 D3)
            continue
        key = (lineno, name, kind)
        if key not in seen:
            seen.add(key)
            out.append(key)
    out.sort()
    return out


def _is_excluded(rel_posix: str) -> bool:
    if rel_posix in _EXCLUDED_FILES:
        return True
    return any(rel_posix.startswith(p) for p in _EXCLUDED_DIR_PREFIXES)


def _scan_path(path: Path) -> list[tuple[Path, int, str]]:
    """Scan one file with the OSS-core exclusion + pragma applied."""
    rel = (
        path.relative_to(_SRC_BALDUR).as_posix() if _SRC_BALDUR in path.parents else ""
    )
    if rel and _is_excluded(rel):
        return []
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    if (
        _PRAGMA in source
    ):  # 659 D8 — PRO-shim exemption: any occurrence exempts the file
        return []
    return [
        (path, ln, f"'{nm}' used as {kd}")
        for ln, nm, kd in scan_source(source, str(path))
    ]


def _scan_oss_core() -> list[tuple[Path, int | None, str | None, str | None]]:
    raw: list[tuple[Path, int | None, str | None, str | None]] = []
    if not _SRC_BALDUR.exists():
        return raw
    for path in sorted(_SRC_BALDUR.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        for offender_path, lineno, extra in _scan_path(path):
            raw.append(
                (
                    offender_path,
                    lineno,
                    None,
                    f"guard-then-use-None: {extra} with no None-guard PRO-absent",
                )
            )
    return raw


class TestProductGuardThenUseNone:
    """G51 — no unguarded guard-then-use-None on the OSS-core surface."""

    def test_no_unguarded_guard_then_use_none_on_oss_core(self):
        raw = _scan_oss_core()
        violations = collect_violations(_RULE_KEY, raw, _RULE_ANCHOR)
        assert not violations, (
            f"G51: {len(violations)} guard-then-use-None site(s) on the OSS-core "
            "surface. A baldur_pro/baldur_dormant name bound to None under "
            "try/except ImportError is used (called/attr-accessed) without a None "
            "guard — PRO-absent that raises TypeError/AttributeError (claim-wiring "
            "fail-open violation). Fix: move the use inside the try, or add an "
            "`if X is None: raise RuntimeError('… requires baldur_pro …')` guard.\n"
            + "\n".join(violations)
        )

    def test_baseline_is_enforced_empty(self):
        assert load_baseline(_RULE_KEY) == {}, (
            f"G51: baseline key '{_RULE_KEY}' must stay empty. A real PRO-absent "
            "crash on the OSS-core surface is fixed, never baselined."
        )


class TestG51Scanner:
    """`scan_source` recognizes the HARD pattern and the safe idioms (659 D8)."""

    @pytest.mark.parametrize(
        ("source", "expected_hits", "note"),
        [
            pytest.param(
                "try:\n"
                "    from baldur_pro.x import f\n"
                "except ImportError:\n"
                "    f = None\n"
                "f()\n",
                1,
                "hard-call",
                id="hard-call",
            ),
            pytest.param(
                "try:\n"
                "    from baldur_pro.x import C\n"
                "except ImportError:\n"
                "    C = None\n"
                "y = C.attr\n",
                1,
                "hard-attr",
                id="hard-attr",
            ),
            pytest.param(
                "try:\n"
                "    from baldur_pro.x import f\n"
                "except ImportError:\n"
                "    f = None\n"
                "if f is None:\n"
                "    raise RuntimeError('needs baldur_pro')\n"
                "f()\n",
                0,
                "early-guard raise proves not-None",
                id="early-guard-raise",
            ),
            pytest.param(
                "try:\n"
                "    from baldur_pro.x import f\n"
                "except ImportError:\n"
                "    f = None\n"
                "if f is not None:\n"
                "    f()\n",
                0,
                "if-not-None nesting",
                id="if-not-none",
            ),
            pytest.param(
                "try:\n"
                "    from baldur_pro.x import f\n"
                "    f()\n"
                "except ImportError:\n"
                "    f = None\n",
                0,
                "SOFT — use inside the ImportError-catching try body",
                id="soft-inside-try",
            ),
            pytest.param(
                "try:\n"
                "    from baldur_pro.x import f\n"
                "except ImportError:\n"
                "    f = None\n"
                "f = something_else()\n"
                "f()\n",
                0,
                "reassigned before use",
                id="reassigned",
            ),
            pytest.param(
                "from baldur.x import f\nif f is None:\n    f = None\nf()\n",
                0,
                "non-gated import is not tracked",
                id="non-gated-ignored",
            ),
        ],
    )
    def test_scan_source_flags_expected(
        self, source: str, expected_hits: int, note: str
    ):
        assert len(scan_source(source)) == expected_hits, note

    def test_per_function_scope_honors_in_function_guard(self):
        # Module-level None binding; one function guards it (clean), another does
        # not (HARD). The module-scope-bleed fix means ONLY the unguarded use is
        # flagged — the guarded function is not a false positive.
        source = (
            "try:\n"
            "    from baldur_pro.x import C\n"
            "except ImportError:\n"
            "    C = None\n"
            "\n"
            "def guarded():\n"
            "    if C is None:\n"
            "        raise RuntimeError('needs baldur_pro')\n"
            "    return C()\n"
            "\n"
            "def unguarded():\n"
            "    return C()\n"
        )
        hits = scan_source(source)
        assert len(hits) == 1
        assert hits[0][1] == "C"

    def test_compound_or_guard_proves_all_names(self):
        # `if A is None or B is None: raise` proves BOTH after — the D4 idiom.
        source = (
            "try:\n"
            "    from baldur_pro.a import A\n"
            "except ImportError:\n"
            "    A = None\n"
            "try:\n"
            "    from baldur_pro.b import B\n"
            "except ImportError:\n"
            "    B = None\n"
            "if A is None or B is None:\n"
            "    raise RuntimeError('needs baldur_pro')\n"
            "A()\n"
            "B.attr\n"
        )
        assert scan_source(source) == []

    def test_unparseable_source_returns_empty(self):
        assert scan_source("def f(:\n") == []

    def test_pragma_exempts_file(self, tmp_path: Path):
        # A file carrying the inline pragma is skipped wholesale (the PRO-shim
        # exemption, 659 D8) even though scan_source still flags the raw site.
        src = (
            "# pro-guard-exempt: PRO-shim file\n"
            "try:\n"
            "    from baldur_pro.x import f\n"
            "except ImportError:\n"
            "    f = None\n"
            "f()\n"
        )
        path = tmp_path / "shim.py"
        path.write_text(src, encoding="utf-8")
        assert len(scan_source(src)) == 1  # raw scan still sees it
        assert _scan_path(path) == []  # but the file-level pragma exempts it

    @pytest.mark.parametrize(
        ("rel", "excluded"),
        [
            ("api/handlers/recovery.py", True),
            ("tasks/governance.py", True),
            ("celery_tasks/circuit_breaker_tasks.py", True),
            ("adapters/celery/tasks/postmortem.py", True),
            ("services/event_bus/bus/_emergency_postmortem.py", True),
            ("resilience/policies/presets.py", False),
            ("services/event_bus/bus/default_handlers.py", False),
        ],
    )
    def test_excluded_path_set(self, rel: str, excluded: bool):
        assert _is_excluded(rel) is excluded


class TestG51BaselineEnforcedEmpty:
    """The meta-assertion goes red if the enforced-empty baseline key gains an entry."""

    def test_meta_assertion_fails_when_baseline_key_nonempty(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        path = tmp_path / "baseline.yaml"
        path.write_text(
            f"{_RULE_KEY}:\n"
            '  - {file: "src/baldur/x.py", reason: "x", ticket: "659"}\n',
            encoding="utf-8",
        )
        monkeypatch.setattr(arch_helpers, "BASELINE_PATH", path)
        arch_helpers._load_baseline_document.cache_clear()
        try:
            with pytest.raises(AssertionError):
                TestProductGuardThenUseNone().test_baseline_is_enforced_empty()
        finally:
            arch_helpers._load_baseline_document.cache_clear()


__all__ = [
    "TestG51BaselineEnforcedEmpty",
    "TestG51Scanner",
    "TestProductGuardThenUseNone",
]
