"""G41 - the grandfathered complexity-noqa debt MUST shrink monotonically.

601 D3 selects the three complexity rules (C901 / PLR0912 / PLR0915) at ruff's
default thresholds so the gate is live for NEW code immediately, and
grandfathers the pre-existing violations with ``ruff check --add-noqa`` (one
``# noqa`` line per offending ``def``, regardless of how many of the three rules
that ``def`` trips). This gate caps the grandfathered debt with an **exact-match
ratchet**, not a ``>=`` floor:

- ``count > budget`` fails with "new complexity debt added - refactor or
  justify": new code cannot grandfather itself by adding a complexity noqa.
- ``count < budget`` fails with "you reduced debt - lower the budget": a refactor
  that removes a noqa MUST ratchet the budget down in the same commit, so the
  freed slack can never be silently re-filled by a new complex ``def``.

The ``>=`` form was rejected because it only closes the bypass leak at landing;
after a refactor drops the count below an untouched budget, the slack
(``budget - count``) can be silently reclaimed, reopening the leak. Exact-match
forces every reduction to ratchet the budget, so the closed-leak guarantee holds
over time.

**Per-root budget (OSS-only-checkout robust).** The budget is a per-root map, not a
single scalar: the public repo ships ``src/baldur`` but not the private
``baldur_pro`` / ``baldur_dormant`` trees, so a single all-``src`` exact count
would fail on an OSS-only checkout (precedent: G20/G21/G38/G39 are all
OSS-only-checkout robust). Each present root is checked against its own budget; an absent
root is skipped, so the gate holds identically on the private repo (all three roots)
and the public repo (``baldur`` only). Each refactor lowers exactly the budget of the
root it touched.

**Unit = noqa LINES, not violations.** ``--add-noqa`` emits one line per ``def``
even when that ``def`` trips two or three of the rules, so a
``# noqa: C901, PLR0912`` line counts once. The budget tracks lines (the natural
``--add-noqa`` unit and the per-function refactor worklist), not the ~253 raw
violations.

**Code tokenization mirrors ruff.** ruff splits the codes after ``noqa:`` on
commas AND whitespace and matches them case-sensitively, so ``# noqa: C901
PLR0912`` (space-separated) and ``# noqa: C901 keep this`` (trailing reason) both
suppress C901, while lowercase ``c901`` does not. The counter extracts
case-sensitive ``[A-Z][A-Z0-9]+`` code tokens from the whole post-``noqa:``
remainder to match ruff exactly - a comma-only split would miss the
space-separated / trailing-reason forms and let a genuinely complex ``def``
grandfather itself invisibly to the exact-match budget.

**Known limitations.** A bare ``# noqa`` (no codes - suppresses everything) is
not counted, so it could bypass the budget; bare noqa is a review-caught bad
practice and ruff places the complexity noqa on the ``def`` line with explicit
codes. Off-line placement, and a complexity code appearing inside a string
literal (a non-comment ``"# noqa: C901"``), are likewise out of the line-scan
counter's reach.

The grandfathered ``# noqa`` set is the per-function refactor worklist; the
systematic refactor is a tracked OOS backlog item, with this gate guaranteeing
the debt shrinks and never grows.

Rule registry:
``ARCHITECTURE.md#g41-complexity-noqa-budget``
"""

from __future__ import annotations

import re
from pathlib import Path

from tests.architecture.conftest import PROJECT_ROOT, walk_src

_SRC = PROJECT_ROOT / "src"

# The three complexity rules grandfathered + gated by 601 D3.
_COMPLEXITY_CODES = frozenset({"C901", "PLR0912", "PLR0915"})

# Inline monotonic-DECREASING per-root budget: the count of ``noqa`` comment
# lines naming >=1 complexity code under each src root, measured at landing.
# Lower the relevant entry (never raise it) whenever a refactor removes a
# complexity noqa from that root - the exact-match assertion enforces it.
_ROOT_BUDGETS: dict[str, int] = {
    # 670: +1 — AsyncRetryPolicy.execute gained a C901 noqa when the observe-only
    # single-attempt guard + should_dlq exhaustion metadata landed, matching its
    # sync twin RetryPolicy.execute which already carries C901.
    # 672: +1 — AsyncIdempotencyGate._check_and_acquire gained a C901 noqa,
    # mirroring its sync twin IdempotencyGate._check_and_acquire (same
    # status-branch shape: CONTINUE/SKIP/ABORT + stale/failed retry paths).
    "baldur": 109,
    # 666: -1 — _update_config_with_meta refactored into _versioned_write +
    # _merge_changes + _post_write helpers, dropping its complexity noqa.
    "baldur_pro": 41,
    "baldur_dormant": 12,
}

# A ``noqa`` directive carrying an explicit code list (a bare ``noqa`` with no
# codes - no colon - is intentionally NOT matched; see the module docstring).
# The ``noqa`` keyword is case-insensitive (ruff accepts ``NOQA``); the codes are
# extracted case-sensitively below to mirror ruff (lowercase ``c901`` is NOT a
# valid suppression in ruff).
_NOQA_RE = re.compile(r"#\s*noqa\s*:(.*)", re.IGNORECASE)

# A ruff rule code: an uppercase letter prefix followed by alphanumerics
# (``C901`` / ``PLR0912`` / ``E501`` / ``F401``). Used with ``findall`` so codes
# separated by commas OR whitespace and any trailing free-text reason are all
# tokenized the way ruff itself does.
_CODE_RE = re.compile(r"[A-Z][A-Z0-9]+")


def count_complexity_noqa_lines(root: Path) -> int:
    """Count ``# noqa`` lines naming >=1 complexity code under ``root`` (pure).

    One line counts once regardless of how many complexity codes it names, so a
    ``# noqa: C901, PLR0912`` line contributes 1. A noqa naming only non-complexity
    codes (e.g. ``# noqa: E501``) contributes 0. Takes a root path so the
    non-vacuity tests can inject a tmp tree instead of scanning real ``src/``.
    """
    total = 0
    for path in walk_src((root,)):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for line in text.splitlines():
            match = _NOQA_RE.search(line)
            if not match:
                continue
            codes = set(_CODE_RE.findall(match.group(1)))
            if codes & _COMPLEXITY_CODES:
                total += 1
    return total


def complexity_budget_verdict(actual: int, budget: int) -> str | None:
    """Return a failure reason when ``actual`` does not exactly match ``budget``.

    ``None`` means in-budget. The exact-match ratchet: above budget is new debt
    (refactor or justify); below budget is unratcheted slack (lower the budget so
    it cannot be silently reclaimed).
    """
    if actual > budget:
        return f"new complexity debt added ({actual} > budget {budget}) - refactor or justify"
    if actual < budget:
        return f"you reduced debt ({actual} < budget {budget}) - lower the budget to {actual}"
    return None


class TestComplexityNoqaBudget:
    """G41 - grandfathered complexity-noqa debt is exact-match-ratcheted per root."""

    def test_complexity_noqa_budget_exact_match(self):
        mismatches: list[str] = []
        for root, budget in _ROOT_BUDGETS.items():
            root_path = _SRC / root
            if not root_path.exists():
                continue  # absent private root on an OSS-only checkout
            verdict = complexity_budget_verdict(
                count_complexity_noqa_lines(root_path), budget
            )
            if verdict is not None:
                mismatches.append(f"src/{root}: {verdict}")
        assert not mismatches, (
            "G41: complexity-noqa budget mismatch. The budget is an exact-match "
            "ratchet - it only ever moves down, in lockstep with refactors that "
            "remove a complexity noqa. Registry: "
            "ARCHITECTURE.md#g41-complexity-noqa-budget"
            "\n" + "\n".join(mismatches)
        )

    def test_over_budget_input_fails(self, tmp_path):
        """Non-vacuity (upward): count above budget is flagged as new debt."""
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        (pkg / "m.py").write_text(
            "def f():  # noqa: C901\n    pass\n", encoding="utf-8"
        )
        count = count_complexity_noqa_lines(pkg)
        assert count == 1
        assert complexity_budget_verdict(count, 0) is not None

    def test_under_budget_input_fails(self):
        """Non-vacuity (downward): count below budget is flagged as unratcheted slack."""
        assert complexity_budget_verdict(0, 5) is not None
        assert complexity_budget_verdict(3, 5) is not None

    def test_exact_match_passes(self):
        assert complexity_budget_verdict(5, 5) is None

    def test_multi_code_noqa_counts_once(self, tmp_path):
        """One ``def`` tripping all three rules is one noqa LINE, not three."""
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        (pkg / "m.py").write_text(
            "def f():  # noqa: C901, PLR0912, PLR0915\n    pass\n", encoding="utf-8"
        )
        assert count_complexity_noqa_lines(pkg) == 1

    def test_non_complexity_noqa_not_counted(self, tmp_path):
        """A noqa naming only non-complexity codes contributes 0."""
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        (pkg / "m.py").write_text("x = 1  # noqa: E501, F401\n", encoding="utf-8")
        assert count_complexity_noqa_lines(pkg) == 0

    def test_space_separated_codes_counted(self, tmp_path):
        """``# noqa: C901 PLR0912`` (space-separated) counts - ruff honors it.

        Regression: ruff 0.15.10 suppresses C901 for a space-separated code list,
        but the original comma-only split tokenized ``C901 PLR0912`` as one
        non-matching code and counted 0 - letting a genuinely complex ``def``
        grandfather itself invisibly to the exact-match budget. The counter now
        tokenizes on whitespace too, matching ruff.
        """
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        (pkg / "m.py").write_text(
            "def f():  # noqa: C901 PLR0912\n    pass\n", encoding="utf-8"
        )
        assert count_complexity_noqa_lines(pkg) == 1

    def test_trailing_reason_noqa_counted(self, tmp_path):
        """``# noqa: C901 <reason>`` counts - ruff honors a code plus free text.

        Regression: a trailing reason after the code (no comma) must not zero the
        count; ruff suppresses C901 here, so the budget must see it.
        """
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        (pkg / "m.py").write_text(
            "def f():  # noqa: C901 keep this complex\n    pass\n", encoding="utf-8"
        )
        assert count_complexity_noqa_lines(pkg) == 1

    def test_lowercase_code_not_counted(self, tmp_path):
        """Lowercase ``c901`` counts 0 - ruff does NOT honor it (case-sensitive).

        A lowercase code does not suppress in ruff, so such a ``def`` still trips
        C901 and fails ``ruff check``; the counter must not phantom-count it.
        """
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        (pkg / "m.py").write_text(
            "def f():  # noqa: c901\n    pass\n", encoding="utf-8"
        )
        assert count_complexity_noqa_lines(pkg) == 0
