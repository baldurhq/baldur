"""G84 — a measurement-named payload field MUST NOT be filled by a literal.

A counter defaulting to ``0`` is telling the truth: nothing happened. A
*measurement* defaulting to ``0`` — or to ``100`` — is not. "What is the
failure rate" has no answer when nothing measured it, and an operator reading
0.0% during an incident cannot tell the difference between a healthy service
and a dead producer. Two such fields shipped and rendered before anything
noticed: a per-service ``failure_rate_5m`` assembled as ``0.0`` inside a loop,
and a ``resolution_rate_percent`` passed to a summary DTO as
``overview.get(…, 0.0)``.

Neither was catchable by an inventory ratchet. Both live on routes that had
existed for months, so no new panel, renderer key or route domain appeared —
the surface was already inventoried, and a new *leaf* inside an inventoried
surface is exactly what an affordance-level gate cannot see. This gate closes
that specific, cheaply-mechanizable half; the rest of leaf truth stays with the
periodic claim-wiring audit and the live-smoke pass.

The rule, the name class and the eligibility context live in
``_fabrication_scan.py`` — the committed scanner is the single source, and this
gate never restates a population number of its own. What it asserts is that the
scanner's live output and the committed verdict baseline are the *same set*, in
both directions:

* a flagged field with no verdict row fails — the regression case;
* a verdict row that no longer matches a flagged field fails as an orphan —
  because the audit's closing counts are taken over these rows, so a row for a
  site that no longer exists is a phantom inflating a count, not a harmlessly
  forgiven violation. This inventory therefore does NOT copy ``baseline.yaml``'s
  silently-accept-improvements policy; anyone extending it must not either.

An occurrence count rides along per row, so a *second* fabricated site added
inside an already-flagged function still regresses rather than hiding under the
existing row.

Rule registry:
``ARCHITECTURE.md#g84-measurement-fallback-fabrication``
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from tests.architecture._fabrication_scan import (
    is_measurement_name,
    scan_roots,
    scan_source,
)
from tests.architecture.conftest import PROJECT_ROOT

_RULE_ANCHOR = "#g84-measurement-fallback-fabrication"

# The baseline-vs-scan half needs the OSS source on disk; the fixture half is
# pure source-string analysis and runs anywhere, so the skip is per class.
_SCAN_ROOTS = (PROJECT_ROOT / "src" / "baldur",)

_needs_oss_source = pytest.mark.skipif(
    not _SCAN_ROOTS[0].is_dir(),
    reason="OSS-source scan runs in the public repo; baldur is a pip sibling here",
)

BASELINE_PATH = Path(__file__).resolve().parent / "measurement_fallback_baseline.yaml"

# Verdicts a flagged field may carry. The three that accept a literal each name
# a different reason it is not a lie, so "why is this allowed" is answerable
# from the row alone:
#
#   real-producer   the producer provably always supplies the key, or the
#                   branch condition itself establishes the value
#   config-default  the number is a configured parameter's default, not a
#                   measurement — the known false-positive family of the name
#                   class, kept in-scope deliberately rather than excluded by a
#                   hand-written name list
#   out-of-surface  the site is not an operator-visible value slot at all (a log
#                   field, an internal decision record, a stub adapter), caught
#                   because the payload-assembly context rule is conservative
#
# and the three that do not:
#
#   honest-absent   the literal was replaced by an explicit absence
#   FABRICATED      a live lie, tracked by the row's ticket
#   cross-ref       another in-flight work item owns the disposition
VALUE_VERDICTS = frozenset(
    {
        "real-producer",
        "config-default",
        "out-of-surface",
        "honest-absent",
        "FABRICATED",
        "cross-ref",
    }
)


def _live_hits():
    return scan_roots(_SCAN_ROOTS, relative_to=PROJECT_ROOT)


def _baseline_rows() -> list[dict]:
    document = yaml.safe_load(BASELINE_PATH.read_text(encoding="utf-8")) or {}
    return list(document.get("rows") or [])


# =============================================================================
# The gate — scanner output vs the committed verdict baseline
# =============================================================================


@_needs_oss_source
class TestMeasurementFallbackBaseline:
    """G84 — every flagged field carries a verdict, and every verdict is live."""

    def test_scanner_and_baseline_are_the_same_set(self):
        live = {hit.key for hit in _live_hits()}
        recorded = {str(row.get("unit")) for row in _baseline_rows()}

        unverdicted = sorted(live - recorded)
        orphaned = sorted(recorded - live)
        drift = [*unverdicted, *orphaned]
        assert not drift, (
            "the measurement-fallback verdict baseline drifted from the live "
            "scan.\n"
            f"  flagged with no verdict row ({len(unverdicted)}): {unverdicted}\n"
            f"  verdict rows matching nothing ({len(orphaned)}): {orphaned}\n"
            "For a new flag: trace the field to its producer and add a row with "
            f"a verdict from {sorted(VALUE_VERDICTS)} plus file:line evidence "
            "(the baseline header says what each verdict means). For an "
            "orphan: delete the row in the same commit as the code change.\n"
            "Rule: ARCHITECTURE.md"
            f"{_RULE_ANCHOR}"
        )

    def test_baseline_row_count_equals_scanner_hit_count(self):
        """The population is the scanner's, never a number written by hand."""
        rows = _baseline_rows()
        assert len(rows) == len(_live_hits()), (
            f"{len(rows)} verdict row(s) vs {len(_live_hits())} flagged field(s) "
            "— the baseline must carry exactly one row per flagged field. A "
            "blanket or truncated baseline makes this gate inert on the very "
            "population it exists for."
        )
        assert len({str(row.get("unit")) for row in rows}) == len(rows), (
            "duplicate unit keys in the verdict baseline"
        )

    def test_occurrence_counts_track_the_live_sites(self):
        """A second fabricated site in a flagged function still regresses."""
        recorded = {
            str(row.get("unit")): int(row.get("occurrences", 1))
            for row in _baseline_rows()
        }
        drifted = [
            (hit.key, recorded.get(hit.key), hit.occurrences)
            for hit in _live_hits()
            if recorded.get(hit.key) != hit.occurrences
        ]
        assert not drifted, (
            "assembly-site counts drifted from the baseline (unit, recorded, "
            f"live): {drifted}. A new site inside an already-flagged function "
            "is a new fabricated value, not a covered one."
        )

    def test_every_row_carries_a_verdict_and_evidence(self):
        """Zero UNKNOWN, zero blank — the audit's closing criterion, mechanized."""
        malformed = []
        for row in _baseline_rows():
            verdict = str(row.get("verdict", "")).strip()
            evidence = str(row.get("evidence", "")).strip()
            if verdict not in VALUE_VERDICTS or not evidence:
                malformed.append((row.get("unit"), verdict, evidence))
        assert not malformed, (
            f"{len(malformed)} verdict row(s) missing a verdict from "
            f"{sorted(VALUE_VERDICTS)} or missing file:line evidence: "
            f"{malformed}"
        )


# =============================================================================
# Non-vacuity — induced violations reproducing the shapes that shipped
# =============================================================================

# The founding exemplar: a measurement-named key filled with a literal inside a
# loop, in a function whose returned expression is not itself the dict. An
# expression-level eligibility rule missed exactly this.
_LOOP_ASSEMBLED = """
def get_metrics(self):
    rows = []
    for name in self.services:
        rows.append({"service": name, "failure_rate_5m": 0.0})
    return {"services": rows}
"""

# The second exemplar: a DTO constructor kwarg whose fallback is the literal.
_CONSTRUCTOR_KWARG = """
def _dict_to_summary(self, overview):
    return DashboardSummary(
        resolution_rate_percent=overview.get("resolution_rate_percent", 0.0),
    )
"""

# Not a historical exemplar — a structural pin. The walk over an eligible
# function body must be depth-unbounded, and nothing else here would catch a
# narrowing of the walk applied to nesting depth rather than to statements.
_NESTED_TWO_DEEP = """
def build_payload(self):
    return {"data": {"stats": {"failure_rate_5m": 0.0}}}
"""

# Counters: ``0`` is the correct default for "how many happened", and no
# counter name carries a measurement token, so the name class excludes them by
# construction rather than by an allowlist.
_COUNTER_DEFAULTS = """
def get_stats(self, raw):
    return {
        "failure_count": 0,
        "retry_total": 0,
        "pending": raw.get("pending", 0),
    }
"""

# Config: a threshold/limit/multiplier legitimately has a numeric default, and
# these dominated the population before the exclusion existed.
_EXCLUDED_CONFIG_NAMES = """
def build_config(self, raw):
    return {
        "error_rate_threshold": 0.5,
        "burn_rate_limit": 10,
        "backoff_rate_multiplier": 2.0,
        "success_rate_min": 0.0,
        "utilization_percent_max": 100,
    }
"""

# A measurement literal outside any payload-assembly function: the context axis
# has to actually gate, or the gate drowns in config and arithmetic.
_NOT_PAYLOAD_ASSEMBLY = """
def tune(self):
    self.error_rate = 0.0
    self._recompute(error_rate=0.0)
"""


def _fields(source: str) -> set[str]:
    return {hit.field for hit in scan_source(source, file="fixture.py")}


class TestInducedViolationsAreCaught:
    """Non-vacuity: the shapes that shipped are the shapes this fires on."""

    def test_loop_assembled_dict_key_is_flagged(self):
        assert _fields(_LOOP_ASSEMBLED) == {"failure_rate_5m"}

    def test_constructor_kwarg_get_fallback_is_flagged(self):
        assert _fields(_CONSTRUCTOR_KWARG) == {"resolution_rate_percent"}

    def test_nested_dict_two_levels_deep_is_flagged(self):
        """The AST walk over an eligible body is depth-unbounded."""
        assert _fields(_NESTED_TWO_DEEP) == {"failure_rate_5m"}


class TestLegitimateDefaultsAreNotFlagged:
    """The negative half: counters and config keep their numeric defaults."""

    def test_counter_defaults_are_not_flagged(self):
        assert _fields(_COUNTER_DEFAULTS) == set()

    def test_excluded_config_names_are_not_flagged(self):
        assert _fields(_EXCLUDED_CONFIG_NAMES) == set()

    def test_non_payload_function_is_out_of_scope(self):
        assert _fields(_NOT_PAYLOAD_ASSEMBLY) == set()

    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("failure_rate_5m", True),
            ("resolution_rate_percent", True),
            ("hit_ratio", True),
            ("confidence_score", True),
            ("cpu_utilization", True),
            ("failure_count", False),
            ("retry_total", False),
            ("error_rate_threshold", False),
            ("burn_rate_limit", False),
            ("backoff_rate_multiplier", False),
            ("success_rate_min", False),
            ("usage_percent_max", False),
            ("rate", False),
        ],
    )
    def test_name_class(self, name, expected):
        assert is_measurement_name(name) is expected
