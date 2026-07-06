"""
Unit test for #660 — ``_SECTION_TIER`` <-> concept-guide table parity (D6).

Target: the section -> tier table in
``docs/concepts/foundations/daily-report.md`` must stay in lockstep with the
``_SECTION_TIER`` single source. The binding rule (R1 mitigation):

  - every *shipped* ``_SECTION_TIER`` key (tier in ``_SHIPPED_TIERS``) appears
    as a guide row under its mapped operator label (``oss`` -> ``OSS``,
    ``v1.0`` -> ``PRO``);
  - no *Deferred* key appears in the guide (no unshipped-feature advertising);
  - the guide carries no stray row for a section the map does not ship.

A graduation that flips one ``_SECTION_TIER`` row without updating the guide
(or vice versa) fails here.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from baldur.services.daily_report.formatters import _SECTION_TIER, _SHIPPED_TIERS

# Map the internal tier token to the operator-facing label used in the guide.
_TIER_TO_GUIDE_LABEL = {"oss": "OSS", "v1.0": "PRO"}

# A guide table row: ``| `section_key` | description | OSS |`` (or ``PRO``).
_GUIDE_ROW = re.compile(r"^\|\s*`([a-z_]+)`\s*\|.*\|\s*(OSS|PRO)\s*\|\s*$")


def _locate_repo_root() -> Path:
    """Climb to the nearest ancestor holding ``pyproject.toml`` (layout-agnostic)."""
    here = Path(__file__).resolve()
    for candidate in here.parents:
        if (candidate / "pyproject.toml").is_file():
            return candidate
    return here.parents[4]


def _guide_path() -> Path:
    return _locate_repo_root() / "docs" / "concepts" / "foundations" / "daily-report.md"


def _parse_guide_section_labels() -> dict[str, str]:
    """Return ``{section_key: guide_label}`` parsed from the guide's tier table."""
    path = _guide_path()
    if not path.is_file():
        pytest.skip(f"concept guide not present in this checkout: {path}")
    labels: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        match = _GUIDE_ROW.match(line)
        if match:
            labels[match.group(1)] = match.group(2)
    return labels


_SHIPPED_KEYS = sorted(k for k, t in _SECTION_TIER.items() if t in _SHIPPED_TIERS)
_DEFERRED_KEYS = sorted(k for k, t in _SECTION_TIER.items() if t not in _SHIPPED_TIERS)


class TestDailyReport660GuideParity:
    """The guide section -> tier table mirrors ``_SECTION_TIER`` exactly."""

    def test_guide_table_is_non_empty(self):
        """The guide actually carries a parseable section -> tier table."""
        assert _parse_guide_section_labels(), "no `section` | ... | OSS/PRO rows found"

    @pytest.mark.parametrize("section", _SHIPPED_KEYS)
    def test_shipped_section_listed_under_mapped_label(self, section):
        """Each shipped section appears in the guide under its mapped label."""
        labels = _parse_guide_section_labels()
        expected = _TIER_TO_GUIDE_LABEL[_SECTION_TIER[section]]

        assert section in labels, f"shipped section '{section}' missing from guide"
        assert labels[section] == expected

    @pytest.mark.parametrize("section", _DEFERRED_KEYS)
    def test_deferred_section_absent_from_guide(self, section):
        """No Deferred section is advertised in the public guide."""
        labels = _parse_guide_section_labels()

        assert section not in labels, f"Deferred section '{section}' leaked into guide"

    def test_guide_has_no_stray_unshipped_rows(self):
        """Every guide row maps to a shipped ``_SECTION_TIER`` key (no drift)."""
        labels = _parse_guide_section_labels()

        unknown = set(labels) - set(_SHIPPED_KEYS)
        assert not unknown, f"guide rows not in the shipped map: {sorted(unknown)}"
