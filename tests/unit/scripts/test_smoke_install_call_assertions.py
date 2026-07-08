"""Unit tests for ``scripts/smoke_install.py`` call_assertions (516 D5).

Scope:
- ``_build_call_assertion_script`` composes a single-subprocess script that
  exercises each ``(call_expr, expected)`` pair in a cell's
  ``call_assertions`` list. Each ``expected`` shape ("ok", "silent_noop",
  "NotImplementedError") wraps the call differently; this test boundary-
  checks all three shapes by *executing* the produced script in a real
  ``python -c`` subprocess and asserting the exit code matches the
  shape's contract.
- Each cell in the ``CELLS`` dict carries a ``call_assertions`` list
  whose entries conform to the documented 3-shape so the smoke gate
  contract stays stable across cells.

These tests don't run the full smoke gate (that requires a built wheel +
clean venv); they isolate the script-builder under test.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[3] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import smoke_install  # noqa: E402


def _run_script(script: str) -> subprocess.CompletedProcess[str]:
    """Execute the composed script via ``python -c`` and return the result."""
    return subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=30,
    )


# =============================================================================
# Expected shapes — Behavior boundary analysis
# =============================================================================


class TestCallAssertionScriptBehavior:
    """Each "expected" shape produces a script whose exit code matches the contract."""

    def test_ok_call_that_succeeds_exits_zero(self):
        script = smoke_install._build_call_assertion_script(
            [("assert 1 + 1 == 2", "ok")]
        )

        result = _run_script(script)

        assert result.returncode == 0, result.stderr

    def test_ok_call_that_raises_exits_nonzero(self):
        """An "ok" assertion that throws fails the cell.

        The contract: "ok" means the call expression must complete without
        raising. A raise must surface as a non-zero exit so CI fails.
        """
        script = smoke_install._build_call_assertion_script(
            [("raise ValueError('boom')", "ok")]
        )

        result = _run_script(script)

        assert result.returncode != 0
        assert "call_assertion_0" in result.stderr
        assert "ValueError" in result.stderr

    def test_silent_noop_call_that_succeeds_exits_zero(self):
        """ "silent_noop" is the same exit shape as "ok" — the call expression
        encodes the noop check internally via inline ``assert`` clauses.
        """
        script = smoke_install._build_call_assertion_script(
            [("assert True", "silent_noop")]
        )

        result = _run_script(script)

        assert result.returncode == 0

    def test_silent_noop_call_that_raises_exits_nonzero(self):
        script = smoke_install._build_call_assertion_script(
            [("raise RuntimeError('nope')", "silent_noop")]
        )

        result = _run_script(script)

        assert result.returncode != 0
        assert "silent_noop" in result.stderr

    def test_notimplementederror_call_that_raises_correctly_exits_zero(self):
        """ "NotImplementedError" passes iff the call raises that exact type."""
        script = smoke_install._build_call_assertion_script(
            [("raise NotImplementedError()", "NotImplementedError")]
        )

        result = _run_script(script)

        assert result.returncode == 0

    def test_notimplementederror_call_that_succeeds_exits_nonzero(self):
        """If the call completes without raising, the contract is violated."""
        script = smoke_install._build_call_assertion_script(
            [("x = 42", "NotImplementedError")]
        )

        result = _run_script(script)

        assert result.returncode != 0
        assert "expected NotImplementedError" in result.stderr

    def test_notimplementederror_call_that_raises_wrong_type_exits_nonzero(self):
        script = smoke_install._build_call_assertion_script(
            [("raise ValueError('wrong type')", "NotImplementedError")]
        )

        result = _run_script(script)

        assert result.returncode != 0
        assert "expected NotImplementedError" in result.stderr
        assert "ValueError" in result.stderr

    def test_unknown_expected_shape_is_rejected(self):
        """Unrecognized ``expected`` shapes fail loudly so typos don't
        silently pass the gate.
        """
        script = smoke_install._build_call_assertion_script([("x = 1", "magic_string")])

        result = _run_script(script)

        assert result.returncode != 0
        assert "unknown expected shape" in result.stderr

    def test_multiple_assertions_in_single_subprocess(self):
        """All assertions in one cell run in one subprocess for CI cost.

        The composed script wraps each pair under a unique label
        (``call_assertion_<idx>``) so failure messages point at the right
        offender, and the subprocess only exits non-zero once any
        assertion fails.
        """
        script = smoke_install._build_call_assertion_script(
            [
                ("assert 1 == 1", "ok"),
                ("assert 2 == 2", "ok"),
                ("assert 3 == 3", "ok"),
            ]
        )

        result = _run_script(script)

        assert result.returncode == 0

    def test_failing_assertion_uses_indexed_label(self):
        """Failure messages identify which assertion failed by index."""
        script = smoke_install._build_call_assertion_script(
            [
                ("assert 1 == 1", "ok"),
                ("raise ValueError('second one')", "ok"),
                ("assert 3 == 3", "ok"),
            ]
        )

        result = _run_script(script)

        assert result.returncode != 0
        assert "call_assertion_1" in result.stderr


# =============================================================================
# CELLS spec — call_assertions shape contract
# =============================================================================


class TestSmokeInstallCellsCallAssertionsContract:
    """Each cell's ``call_assertions`` entry conforms to the 3-shape contract."""

    _VALID_EXPECTED = {"ok", "silent_noop", "NotImplementedError"}

    @pytest.mark.parametrize("cell_name", list(smoke_install.CELLS.keys()))
    def test_call_assertions_present(self, cell_name):
        """Every cell carries a ``call_assertions`` list (D5 contract)."""
        assert "call_assertions" in smoke_install.CELLS[cell_name]
        assert isinstance(smoke_install.CELLS[cell_name]["call_assertions"], list)

    @pytest.mark.parametrize("cell_name", list(smoke_install.CELLS.keys()))
    def test_each_assertion_uses_known_expected_shape(self, cell_name):
        for call_expr, expected in smoke_install.CELLS[cell_name]["call_assertions"]:
            assert expected in self._VALID_EXPECTED, (
                f"cell={cell_name} call_expr={call_expr!r} "
                f"expected={expected!r} (allowed: {sorted(self._VALID_EXPECTED)})"
            )
            assert isinstance(call_expr, str), (
                f"cell={cell_name} call_expr must be a string"
            )
            assert call_expr.strip(), (
                f"cell={cell_name} call_expr must be a non-empty string"
            )

    def test_total_assertions_count_in_target_range(self):
        """Guards both lower and upper bounds on total assertions across the
        OSS cells.

        Lower bound stops a contributor from accidentally removing all
        assertions from a cell. Upper bound caps unbounded growth. The range
        spans the 9 OSS cells (baseline carries the bulk — the OSS->PRO
        boundary None-slot + relocated-feature-absence assertions).
        """
        total = sum(len(cfg["call_assertions"]) for cfg in smoke_install.CELLS.values())
        assert 5 <= total <= 36, f"got {total} total call_assertions"
