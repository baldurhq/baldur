"""Architectural fitness function — ``_current_domain.set(`` allowed only in
``baldur/decorators/domain_tag.py`` (545 chokepoint 5 invariant).

545 D5 chokepoint 5 funnels the Celery legacy-header restore path through
``set_domain_context()`` so OTel-baggage / legacy-header injected ``domain``
values inherit validation + fallback. Any future caller that reaches for
``_current_domain.set(...)`` directly would bypass the chokepoint and let an
unbounded raw value back into the ContextVar.

This rule locks the invariant: only ``decorators/domain_tag.py`` (the canonical
owner that wraps the ``set`` call inside ``set_domain_context``) may call
``_current_domain.set(`` directly.

Rule registry:
``ARCHITECTURE.md#g22-domain-set-direct-call``
"""

from __future__ import annotations

import ast
from pathlib import Path

from tests.architecture._helpers import PROJECT_ROOT
from tests.architecture.conftest import (
    collect_violations,
    parse_ast,
    symbol_of,
    walk_src,
)

_RULE_KEY = "domain_set_direct_call"
_RULE_ANCHOR = "#g22-domain-set-direct-call"

_OWNER_REL = "src/baldur/decorators/domain_tag.py"
_SRC_ROOTS: tuple[Path, ...] = (
    PROJECT_ROOT / "src" / "baldur",
    PROJECT_ROOT / "src" / "baldur_pro",
)


def _is_current_domain_set_call(node: ast.Call) -> bool:
    """Return True iff ``node`` matches ``_current_domain.set(...)``."""
    func = node.func
    if not isinstance(func, ast.Attribute):
        return False
    if func.attr != "set":
        return False
    value = func.value
    if not isinstance(value, ast.Name):
        return False
    return value.id == "_current_domain"


def _scan(path: Path) -> list[tuple[Path, int, str, str]]:
    tree = parse_ast(path)
    if tree is None:
        return []
    violations: list[tuple[Path, int, str, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not _is_current_domain_set_call(node):
            continue
        violations.append(
            (path, node.lineno, symbol_of(tree, node), "_current_domain.set(...) call")
        )
    return violations


def _is_owner(path: Path) -> bool:
    try:
        rel = path.resolve().relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        rel = path.as_posix()
    return rel == _OWNER_REL


class TestDomainSetDirectCallArchitecture:
    """545 chokepoint 5 invariant — only ``decorators/domain_tag.py`` may call
    ``_current_domain.set(...)`` directly."""

    def test_no_direct_call_outside_canonical_owner(self):
        raw: list[tuple[Path, int | None, str | None, str | None]] = []
        for path in walk_src(_SRC_ROOTS):
            if _is_owner(path):
                continue
            for offender_path, line, symbol, extra in _scan(path):
                raw.append((offender_path, line, symbol, extra))

        violations = collect_violations(_RULE_KEY, raw, _RULE_ANCHOR)
        assert not violations, (
            f"545 chokepoint 5 invariant breach ({len(violations)}). "
            "Replace `_current_domain.set(...)` with "
            "`set_domain_context(...)` from `baldur.decorators.domain_tag`, "
            "or add a baseline entry under `domain_set_direct_call:` with "
            "reason+ticket.\n" + "\n".join(violations)
        )

    def test_canonical_owner_still_holds_the_call(self):
        """Belt-and-suspenders: confirm the owner does call
        ``_current_domain.set(...)`` — guards against accidental rename."""
        owner_path = PROJECT_ROOT / _OWNER_REL
        violations = _scan(owner_path)
        assert violations, (
            f"Canonical owner {_OWNER_REL} no longer calls "
            "`_current_domain.set(...)`. If the call was renamed, update "
            "this fitness function's owner anchor."
        )
