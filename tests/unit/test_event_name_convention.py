"""
CI source-scan test — full event-name convention compliance.

Walks the AST of every ``logger.<method>(...)`` call inside the baldur
package and asserts the first positional argument (when a string literal)
matches ``^[a-z][a-z0-9_]*\\.[a-z][a-z0-9_]*$``.

Strategy:
- AST walk keyed on the ``logger`` variable name (the codebase uses
  ``logger = structlog.get_logger()`` uniformly).
- Method-name-based detection is avoided — too many false positives.
- Post-512 Wave 3 sweep: zero free-form events remain across all 90 sites
  swept by #509 + #510 + #511 + #512. Threshold is a hard zero — any
  future commit introducing a non-conformant event fails this test.

Reference:
- 314 (closed)
- 512
"""

from __future__ import annotations

import ast
import re

from tests.factories import src_root

# Layout-robust: a fixed ``parents[3]`` climbs one level too high on the renamed
# mirror tree (tests/ -> tests/), resolving to a non-existent ``src/baldur``
# whose rglob yields zero files — the convention scan would then VACUOUSLY PASS
# on the mirror, silently disabling the gate. ``src_root()`` walks to the
# pyproject marker, so the scan stays live in both layouts.
_BALDUR_ROOT = src_root() / "baldur"
_EVENT_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$")
_LOG_METHODS = frozenset({"debug", "info", "warning", "error", "critical", "exception"})

_VIOLATION_THRESHOLD = 0


def _collect_violations() -> list[tuple[str, int, str]]:
    """Collect convention violations across every ``.py`` file under baldur.

    Returns:
        [(relative_path, line_number, event_name), ...]
    """
    violations: list[tuple[str, int, str]] = []

    for py_file in _BALDUR_ROOT.rglob("*.py"):
        try:
            source = py_file.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(py_file))
        except (SyntaxError, UnicodeDecodeError):
            continue

        rel_path = str(py_file.relative_to(_BALDUR_ROOT))

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue

            func = node.func
            if not isinstance(func, ast.Attribute):
                continue
            if func.attr not in _LOG_METHODS:
                continue
            if not isinstance(func.value, ast.Name):
                continue
            if func.value.id != "logger":
                continue

            if not node.args:
                continue

            first_arg = node.args[0]
            if isinstance(first_arg, ast.Constant) and isinstance(first_arg.value, str):
                if not _EVENT_NAME_PATTERN.match(first_arg.value):
                    violations.append((rel_path, node.lineno, first_arg.value))

    return violations


class TestEventNameConventionScanBehavior:
    """AST-based event-name convention source scan."""

    def test_violation_count_within_threshold(self):
        """Assert zero non-conformant events anywhere in src/baldur/."""
        violations = _collect_violations()
        violation_count = len(violations)

        assert violation_count <= _VIOLATION_THRESHOLD, (
            f"Event name convention violations ({violation_count}) exceed "
            f"threshold ({_VIOLATION_THRESHOLD}). "
            f"First 10 violations:\n"
            + "\n".join(
                f"  {path}:{line}: {name!r}" for path, line, name in violations[:10]
            )
        )
