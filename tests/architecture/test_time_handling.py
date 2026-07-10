"""G5 — `datetime.now()` / `datetime.utcnow()` outside `utils/time.py`.

CLAUDE.md § Code Rules — Time handling: use `utils/time.utc_now()` instead of
``datetime.now()`` / ``datetime.utcnow()`` directly. ``time.time()`` /
``time.monotonic()`` remain acceptable for perf/rate-limit.

Detection pattern (AST `ast.Call`):
- ``datetime.now()`` / ``datetime.utcnow()`` where ``datetime`` is the class
  imported via ``from datetime import datetime``.
- ``datetime.datetime.now()`` / ``datetime.datetime.utcnow()`` via
  ``import datetime``.

Exempt modules (defined the abstractions): ``utils/time.py``,
``core/time_provider.py``.

Rule registry: ``ARCHITECTURE.md#g5-time-handling``
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

_RULE_KEY = "time_handling"
_RULE_ANCHOR = "#g5-time-handling"
_FORBIDDEN_ATTRS = frozenset({"now", "utcnow"})

_EXEMPT_SUFFIXES = (
    "utils/time.py",
    "core/time_provider.py",
)


def _is_exempt(path: Path) -> bool:
    posix = path.as_posix()
    return any(posix.endswith(suffix) for suffix in _EXEMPT_SUFFIXES)


def _is_datetime_class_call(call: ast.Call) -> bool:
    func = call.func
    if not isinstance(func, ast.Attribute):
        return False
    if func.attr not in _FORBIDDEN_ATTRS:
        return False
    receiver = func.value
    if isinstance(receiver, ast.Name) and receiver.id == "datetime":
        return True
    if (
        isinstance(receiver, ast.Attribute)
        and receiver.attr == "datetime"
        and isinstance(receiver.value, ast.Name)
        and receiver.value.id == "datetime"
    ):
        return True
    return False


def _scan(path: Path) -> list[tuple[Path, int, str, str]]:
    tree = parse_ast(path)
    if tree is None:
        return []
    violations: list[tuple[Path, int, str, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _is_datetime_class_call(node):
            attr = node.func.attr  # type: ignore[attr-defined]
            violations.append(
                (path, node.lineno, symbol_of(tree, node), f"datetime.{attr}() call")
            )
    return violations


class TestTimeHandlingContract:
    """G5 — direct `datetime.now()` / `utcnow()` is forbidden."""

    def test_no_unbaselined_violations(self):
        raw: list[tuple[Path, int | None, str | None, str | None]] = []
        for path in walk_src(DEFAULT_SRC_ROOTS):
            if _is_exempt(path):
                continue
            for offender_path, line, symbol, extra in _scan(path):
                raw.append((offender_path, line, symbol, extra))

        violations = collect_violations(_RULE_KEY, raw, _RULE_ANCHOR)
        assert not violations, (
            f"G5: time handling regressions ({len(violations)}). "
            "Replace `datetime.now()` / `datetime.utcnow()` with "
            "`baldur.utils.time.utc_now()` or add a baseline entry under "
            "`time_handling:` with reason+ticket.\n" + "\n".join(violations)
        )
