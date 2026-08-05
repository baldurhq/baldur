"""Shared discovery of the shipped Grafana dashboard JSONs.

Both G43 (``test_grafana_dashboard_metric_drift.py`` — panel query metric
names) and G75 (``test_grafana_dashboard_grid_integrity.py`` — panel geometry)
police every board shipped under ``examples/monitoring/``. Each used to name
the two current boards in a literal tuple, so a newly added third board would
have been silently unscanned by both: the guards stay green while the new board
goes unchecked in exactly the two ways they exist to check.

The board set is therefore derived from the directory, never hand-listed, and
pinned by a floor — the boards that ship today MUST appear among the discovered
files, so a glob that drifts to empty (renamed directory, relocated assets)
fails loudly instead of passing vacuously over nothing. Discovery is shared
here for the same reason the PromQL tokenizer is: the two guards must not drift
apart on which boards exist.

A non-dashboard ``.json`` dropped into the directory is harmless to both
guards — a file with no ``panels`` key yields no panel cases and no geometry
cases. The alert asset is a single file with no set to derive, so G48/G78 keep
naming it directly.

This is a non-test helper (no ``test_`` prefix → pytest does not collect it).
"""

from __future__ import annotations

from pathlib import Path

__all__ = [
    "FLOOR_BOARD_NAMES",
    "missing_floor_boards",
    "shipped_dashboards",
]

# The boards known to ship today. This is a floor, not the scan set: discovery
# may find more, but never fewer.
FLOOR_BOARD_NAMES = ("baldur-overview.json", "baldur-operations.json")


def shipped_dashboards(monitoring_dir: Path) -> tuple[Path, ...]:
    """Return every dashboard JSON under ``monitoring_dir``, sorted by name."""
    return tuple(sorted(monitoring_dir.glob("*.json")))


def missing_floor_boards(dashboards: tuple[Path, ...]) -> list[str]:
    """Return the floor board names absent from ``dashboards`` (empty = ok)."""
    found = {path.name for path in dashboards}
    return [name for name in FLOOR_BOARD_NAMES if name not in found]
