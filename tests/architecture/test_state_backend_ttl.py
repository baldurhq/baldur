"""G11 — `StateBackend.set()` MUST pass `ttl_seconds=` (related to OOS #444).

CLAUDE.md § Pattern Compliance and ``core/state_backend.py:51`` —
``ttl_seconds: int | None = None``. Callers can silently store keys without
TTL, causing indefinite Redis persistence. OOS #444 covers the upstream
signature-hardening to keyword-only; this rule lands the *complementary
detection* per D9.

Detection (heuristic, per D8.b):
1. Find ``ast.Call`` of shape ``<expr>.set(key, value)``.
2. The receiver ``<expr>`` plausibly resolves to a ``StateBackend`` instance:
   - variable named ``*_backend`` / ``*_state`` / ``*_store``;
   - attribute access matching the same suffix
     (``self.state_backend.set(...)``, ``self._cb_store.set(...)``);
   - direct call ``get_state_backend().set(...)``.
3. No ``ttl_seconds=`` keyword argument is passed.

False positives (``dict.set``, ``set()`` built-in, type-perfect match needs
mypy) are inevitable. The doc explicitly accepts this trade-off — baseline
absorbs current offenders with **line-level granularity per D4.c** so new
violations in the same file regress even when an old one is allowlisted.

Rule registry: ``ARCHITECTURE.md#g11-state-backend-ttl``
"""

from __future__ import annotations

import ast
from pathlib import Path

from tests.architecture.conftest import (
    DEFAULT_SRC_ROOTS,
    collect_violations,
    parse_ast,
    symbol_of,
    walk_src,
)

_RULE_KEY = "state_backend_ttl"
_RULE_ANCHOR = "#g11-state-backend-ttl"

_RECEIVER_NAME_SUFFIXES = ("_backend", "_state", "_store")
_DIRECT_FACTORIES = frozenset({"get_state_backend"})


def _receiver_looks_like_state_backend(receiver: ast.expr) -> bool:
    if isinstance(receiver, ast.Name):
        return receiver.id.endswith(_RECEIVER_NAME_SUFFIXES)
    if isinstance(receiver, ast.Attribute):
        return receiver.attr.endswith(_RECEIVER_NAME_SUFFIXES)
    if isinstance(receiver, ast.Call):
        func = receiver.func
        if isinstance(func, ast.Name) and func.id in _DIRECT_FACTORIES:
            return True
        if isinstance(func, ast.Attribute) and func.attr in _DIRECT_FACTORIES:
            return True
    return False


def _has_ttl_kwarg(call: ast.Call) -> bool:
    return any(kw.arg == "ttl_seconds" for kw in call.keywords)


def _scan(path: Path) -> list[tuple[Path, int, str, str]]:
    tree = parse_ast(path)
    if tree is None:
        return []
    violations: list[tuple[Path, int, str, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "set"):
            continue
        if not _receiver_looks_like_state_backend(func.value):
            continue
        if _has_ttl_kwarg(node):
            continue
        violations.append(
            (
                path,
                node.lineno,
                symbol_of(tree, node),
                "StateBackend.set() called without ttl_seconds= "
                "(receiver heuristic — annotate or migrate)",
            )
        )
    return violations


class TestStateBackendTtlContract:
    """G11 — every `StateBackend.set(...)` MUST pass `ttl_seconds=`."""

    def test_no_unbaselined_violations(self):
        raw: list[tuple[Path, int | None, str | None, str | None]] = []
        for path in walk_src(DEFAULT_SRC_ROOTS):
            for offender_path, line, symbol, extra in _scan(path):
                raw.append((offender_path, line, symbol, extra))

        violations = collect_violations(_RULE_KEY, raw, _RULE_ANCHOR)
        assert not violations, (
            f"G11: StateBackend.set() TTL omission regressions ({len(violations)}). "
            "Pass `ttl_seconds=...` (use `None` only for genuinely infinite "
            "keys with code comment justification) or add a line-level baseline "
            "entry under `state_backend_ttl:` with reason+ticket.\n"
            + "\n".join(violations)
        )
