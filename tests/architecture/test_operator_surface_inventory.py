"""G82 — every operator-surface unit carries a verdict, and every verdict is live.

The defect this exists for is not a crash. It is a console that looks wired: a
number rendered from nothing, a button that updates, audits, and changes
nothing. Both shapes shipped, and both were found sideways — while working on
something else — which means discovery tracked where the digging happened, not
where the defects were.

So the surface is inventoried instead of remembered. ``_operator_surface.py``
derives the affordance-level units from code (panels and their body-field keys
from the shipped console asset, bespoke-renderer payload reads from the same
asset, control domains from instantiating the admin route registry), and this
gate asserts that the derived set and the committed verdict baseline are the
**same set, in both directions**:

* a live unit with no verdict row fails — a new panel, renderer key, operator
  input or control domain has to be traced to its producer or consumer before
  it ships;
* a verdict row matching no live unit fails as an orphan — the audit's closing
  counts are taken over these rows, so a row for a unit that no longer exists
  is a phantom inflating a count, not a harmlessly forgiven violation.

That second direction is why this file does NOT inherit ``baseline.yaml``'s
silently-accept-improvements policy. That file is a violation allowlist, where a
stale row only forgives something that stopped happening. This is a verdict
inventory, where a stale row misreports what was audited.

Tiers are derived, not assigned: T1 is what an OSS operator sees (a
``pro: false`` panel and everything hanging off it) and is the launch gate, T2
is the PRO console surface, T3 is a control domain no panel reaches. Flipping a
panel's ``pro`` flag re-tiers its whole surface with no list to edit — and
because the T1 rule below forbids an unresolved or defective verdict at T1, a
panel promoted to OSS visibility drags its unaudited units into the gate.

Registry instantiation and asset reads work identically in both repos, so this
gate carries no source-tree skip.

Rule registry:
``ARCHITECTURE.md#g82-operator-surface-inventory``
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from tests.architecture._operator_surface import (
    BODY_FIELD,
    PANEL,
    RENDERER_KEY,
    ROUTE_DOMAIN,
    T1,
    TIERS,
    admin_routes,
    console_html,
    level1_units,
)

_RULE_ANCHOR = "#g82-operator-surface-inventory"

BASELINE_PATH = Path(__file__).resolve().parent / "operator_surface_baseline.yaml"

# What a *value* unit's verdict may say — a panel or a renderer read key.
VALUE_VERDICTS = frozenset(
    {"real-producer", "honest-absent", "FABRICATED", "cross-ref", "pending-tier"}
)
# What a *control* unit's verdict may say — a control domain or an operator
# input. "DEAD-CONTROL" covers echo-only consumption: a write that is stored and
# read back by nothing behavioural is a dead control, not a reached one.
CONTROL_VERDICTS = frozenset(
    {"consumer-reached", "DEAD-CONTROL", "cross-ref", "pending-tier"}
)
_VERDICTS_BY_KIND = {
    PANEL: VALUE_VERDICTS,
    RENDERER_KEY: VALUE_VERDICTS,
    ROUTE_DOMAIN: CONTROL_VERDICTS,
    BODY_FIELD: CONTROL_VERDICTS,
}

# Verdicts a T1 unit may not still carry once T1 closes. "pending-tier" is the
# marker for a unit whose tier has not been audited yet, so it is as
# unacceptable at T1 as the two defect verdicts are.
T1_FORBIDDEN_VERDICTS = frozenset({"FABRICATED", "DEAD-CONTROL", "pending-tier"})


def _baseline() -> dict:
    return yaml.safe_load(BASELINE_PATH.read_text(encoding="utf-8")) or {}


def _rows() -> list[dict]:
    return list(_baseline().get("rows") or [])


# =============================================================================
# The gate
# =============================================================================


class TestOperatorSurfaceInventory:
    """G82 — derived inventory and committed verdicts agree, both directions."""

    def test_derived_units_and_verdict_rows_are_the_same_set(self):
        live = {unit.id for unit in level1_units()}
        recorded = {str(row.get("unit")) for row in _rows()}

        unverdicted = sorted(live - recorded)
        orphaned = sorted(recorded - live)
        drift = [*unverdicted, *orphaned]
        assert not drift, (
            "the operator-surface verdict baseline drifted from the derived "
            "inventory.\n"
            f"  live with no verdict row ({len(unverdicted)}): {unverdicted}\n"
            f"  verdict rows matching nothing ({len(orphaned)}): {orphaned}\n"
            "A new panel, renderer read key, operator input or mutating control "
            "domain needs a verdict row before it ships: trace it to the "
            "producer that fills it (or the consumer that acts on it) and "
            "record the verdict with file:line evidence. A removed unit's row "
            "goes in the same commit that removes it — regenerate with "
            "derive_operator_surface.py.\n"
            "Rule: ARCHITECTURE.md"
            f"{_RULE_ANCHOR}"
        )

    def test_recorded_tiers_match_the_derived_tiers(self):
        """A panel flipping its ``pro`` flag re-tiers its surface, loudly."""
        derived = {unit.id: unit.tier for unit in level1_units()}
        drifted = [
            (str(row.get("unit")), row.get("tier"), derived.get(str(row.get("unit"))))
            for row in _rows()
            if str(row.get("tier")) != derived.get(str(row.get("unit")))
        ]
        assert not drifted, (
            f"{len(drifted)} row(s) record a tier the asset no longer implies "
            f"(unit, recorded, derived): {drifted}. Tier is derived from panel "
            "visibility — regenerate the baseline rather than editing the tier."
        )

    def test_every_row_carries_a_verdict_for_its_kind_and_evidence(self):
        malformed = []
        for row in _rows():
            unit = str(row.get("unit", ""))
            kind = unit.split(":", 1)[0]
            allowed = _VERDICTS_BY_KIND.get(kind, frozenset())
            verdict = str(row.get("verdict", "")).strip()
            evidence = str(row.get("evidence", "")).strip()
            # Evidence is required of every verdict that makes a claim. A
            # "pending-tier" row makes none yet — its tier has not been audited —
            # and the T1 rule below is what stops that from becoming a hiding
            # place where it matters.
            needs_evidence = verdict != "pending-tier"
            if (
                verdict not in allowed
                or row.get("tier") not in TIERS
                or (needs_evidence and not evidence)
            ):
                malformed.append((unit, verdict, row.get("tier"), bool(evidence)))
        assert not malformed, (
            f"{len(malformed)} verdict row(s) are blank, mistyped, or carry a "
            f"verdict from the wrong vocabulary (unit, verdict, tier, "
            f"has_evidence): {malformed}.\n"
            f"  value units ({PANEL}, {RENDERER_KEY}): {sorted(VALUE_VERDICTS)}\n"
            f"  control units ({ROUTE_DOMAIN}, {BODY_FIELD}): "
            f"{sorted(CONTROL_VERDICTS)}"
        )

    def test_no_t1_unit_is_unaudited_or_defective(self):
        """The launch gate, mechanized: T1 holds no lie, dead control, or blank.

        This is the criterion the audit closes T1 on. It is asserted here rather
        than reviewed by hand because the whole point of the exercise was that
        hand review is what missed these in the first place.
        """
        offenders = sorted(
            (str(row.get("unit")), str(row.get("verdict")))
            for row in _rows()
            if row.get("tier") == T1
            and str(row.get("verdict")) in T1_FORBIDDEN_VERDICTS
        )
        assert not offenders, (
            f"{len(offenders)} T1 unit(s) still carry an unresolved or "
            f"defective verdict: {offenders}. A T1 fabricated value is "
            "re-dispositioned to honest absence, a T1 dead control is removed "
            "or disabled, and a unit owned by other in-flight work carries "
            "'cross-ref' with that owner in its ticket."
        )

    def test_admin_route_count_holds_its_ratchet(self):
        """A registrar that swallowed its import failure drops a whole domain.

        Every route registrar is a ``try/except`` that returns on failure, so
        "domain absent from the inventory" is never evidence that the surface is
        absent. The committed count is the floor; a deliberate route removal
        regenerates it.
        """
        floor = int(_baseline().get("meta", {}).get("admin_route_count", 0))
        live = len(admin_routes())
        assert live >= floor, (
            f"the admin registry produced {live} route(s), below the committed "
            f"floor of {floor}. A registrar most likely swallowed an import "
            "error and dropped its whole domain silently — check the route "
            "modules before touching this number."
        )

    def test_extraction_is_not_vacuous(self):
        """A regex parse that drifted to empty must fail as a parser error."""
        by_kind: dict[str, int] = {}
        for unit in level1_units():
            by_kind[unit.kind] = by_kind.get(unit.kind, 0) + 1
        empty = sorted(k for k in _VERDICTS_BY_KIND if not by_kind.get(k))
        assert not empty, (
            f"extraction produced no units at all for {empty}. The console "
            "asset's formatting or the route registry most likely drifted — fix "
            "the extractor, not the baseline."
        )


# =============================================================================
# Non-vacuity — the equality catches both directions on synthetic input
# =============================================================================

_FIXTURE_ASSET = """
  var PANELS = [
    { id: "alpha", title: "Alpha", pro: false, status: "/alpha/status",
      bodyRender: "alpha",
      actions: [
        { label: "Go", path: "/alpha/go", method: "POST", risk: "admin",
          bodyFields: [
            { key: "reason", label: "Reason", type: "text" }
          ] }
      ] }
  ];
  var BODY_RENDERERS = {
    alpha: renderAlpha
  };
  function renderAlpha(bodyEl, data) {
    var ov = (data && data.overview) || {};
    bodyEl.textContent = String(ov.error_rate);
  }
"""


def _fixture_unit_ids() -> set[str]:
    return {unit.id for unit in level1_units(raw=_FIXTURE_ASSET, routes=[])}


class TestInventoryEqualityIsNotVacuous:
    """Both failure directions reproduce on a controlled asset."""

    def test_fixture_asset_yields_the_expected_units(self):
        assert _fixture_unit_ids() == {
            "panel:alpha",
            "body-field:/alpha/go::reason",
            "renderer-key:alpha::overview",
            "renderer-key:alpha::error_rate",
        }

    def test_a_new_unit_with_no_verdict_row_is_detected(self):
        """The regression direction — the shape a new panel or key arrives in."""
        recorded = _fixture_unit_ids() - {"renderer-key:alpha::error_rate"}
        assert _fixture_unit_ids() - recorded == {"renderer-key:alpha::error_rate"}

    def test_a_row_matching_nothing_is_detected_as_an_orphan(self):
        """The direction a one-way subset check would let through forever."""
        recorded = _fixture_unit_ids() | {"panel:deleted_last_release"}
        assert recorded - _fixture_unit_ids() == {"panel:deleted_last_release"}


class TestDerivedTiering:
    """Tier follows panel visibility, so nothing is tiered by hand."""

    def test_oss_visible_panel_puts_its_whole_surface_in_t1(self):
        units = {u.id: u.tier for u in level1_units(raw=_FIXTURE_ASSET, routes=[])}
        assert set(units.values()) == {T1}

    def test_pro_panel_puts_its_surface_in_t2(self):
        pro_asset = _FIXTURE_ASSET.replace("pro: false", "pro: true")
        units = {u.id: u.tier for u in level1_units(raw=pro_asset, routes=[])}
        assert set(units.values()) == {"T2"}

    @pytest.mark.parametrize("kind", sorted(_VERDICTS_BY_KIND))
    def test_live_inventory_covers_every_unit_kind(self, kind):
        assert any(unit.kind == kind for unit in level1_units())

    def test_console_asset_is_readable(self):
        assert "var PANELS = [" in console_html()
