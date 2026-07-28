"""G75 — shipped Grafana boards MUST have a self-consistent panel grid.

The sample dashboards under ``examples/monitoring/`` are hand-authored JSON, and
inserting a panel means rewriting the ``gridPos`` of every panel below it. That
reflow is recurring and mechanical — two consecutive documents rewrote 14 and
~15 coordinates respectively — and every way it can go wrong is silent: two
panels whose rectangles intersect render stacked or displaced, a duplicate panel
id makes Grafana drop one of them, and a panel wider than the 24-column grid is
clipped. Nothing catches any of it until a human opens the board.

G43 sits one axis over: it validates the metric *names* inside ``target.expr``
and says so in its own docstring ("name existence, NOT population"). It never
reads ``gridPos``. This guard covers the geometry, so the same scoped commit
gate that catches a rotted series name now also catches a botched reflow.

What is asserted, per shipped board:

* panel ids are unique — Grafana keys panels by id
* no two panel rectangles intersect (half-open ranges: a panel at ``y+h`` sits
  directly below one at ``y``, which is adjacency, not overlap)
* ``x + w <= 24`` — the Grafana grid is 24 columns wide
* row panels are monotonically increasing in ``y`` — rows partition the board
  top to bottom, so an out-of-order row silently re-parents the panels under it

Rule registry:
``ARCHITECTURE.md#g75-grafana-dashboard-grid-integrity``
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.architecture.conftest import PROJECT_ROOT

_MONITORING_DIR = PROJECT_ROOT / "examples" / "monitoring"
_DASHBOARDS = (
    _MONITORING_DIR / "baldur-overview.json",
    _MONITORING_DIR / "baldur-operations.json",
)

_GRID_COLUMNS = 24


def _panels(dashboard_path: Path) -> list[dict]:
    data = json.loads(dashboard_path.read_text(encoding="utf-8"))
    return list(data.get("panels", []))


def _rect(panel: dict) -> tuple[int, int, int, int]:
    """Return ``(x, y, w, h)`` for a panel, defaulting a missing gridPos to 0."""
    grid = panel.get("gridPos", {})
    return (
        int(grid.get("x", 0)),
        int(grid.get("y", 0)),
        int(grid.get("w", 0)),
        int(grid.get("h", 0)),
    )


def _overlaps(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> bool:
    """True when two ``(x, y, w, h)`` rectangles share interior area."""
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    return ax < bx + bw and bx < ax + aw and ay < by + bh and by < ay + ah


def _label(panel: dict) -> str:
    return f"id={panel.get('id')} {panel.get('title', '<untitled>')!r}"


@pytest.mark.parametrize("dashboard", _DASHBOARDS, ids=lambda p: p.name)
def test_panel_ids_are_unique(dashboard: Path) -> None:
    """A duplicate id makes Grafana drop one of the two panels on load."""
    seen: dict[int, str] = {}
    duplicates: list[str] = []
    for panel in _panels(dashboard):
        panel_id = panel.get("id")
        if panel_id in seen:
            duplicates.append(f"{_label(panel)} collides with {seen[panel_id]}")
        else:
            seen[panel_id] = _label(panel)

    assert not duplicates, (
        f"{dashboard.name}: duplicate panel id(s). Grafana keys panels by id, "
        "so one of each pair is silently dropped.\n" + "\n".join(duplicates)
    )


@pytest.mark.parametrize("dashboard", _DASHBOARDS, ids=lambda p: p.name)
def test_panels_do_not_overlap(dashboard: Path) -> None:
    """Intersecting rectangles render stacked or displaced, never as authored."""
    panels = _panels(dashboard)
    collisions: list[str] = []
    for i, first in enumerate(panels):
        for second in panels[i + 1 :]:
            if _overlaps(_rect(first), _rect(second)):
                collisions.append(
                    f"{_label(first)} {_rect(first)} overlaps "
                    f"{_label(second)} {_rect(second)}"
                )

    assert not collisions, (
        f"{dashboard.name}: overlapping panel rectangle(s) — a reflow left a "
        "gap unclaimed or double-claimed. Coordinates are (x, y, w, h).\n"
        + "\n".join(collisions)
    )


@pytest.mark.parametrize("dashboard", _DASHBOARDS, ids=lambda p: p.name)
def test_panels_fit_the_grid_width(dashboard: Path) -> None:
    """Grafana's grid is 24 columns; anything past it is clipped."""
    overruns = [
        f"{_label(panel)} spans x={x}..{x + w} (> {_GRID_COLUMNS})"
        for panel in _panels(dashboard)
        for x, _y, w, _h in [_rect(panel)]
        if x + w > _GRID_COLUMNS
    ]

    assert not overruns, (
        f"{dashboard.name}: panel(s) wider than the {_GRID_COLUMNS}-column "
        "grid.\n" + "\n".join(overruns)
    )


@pytest.mark.parametrize("dashboard", _DASHBOARDS, ids=lambda p: p.name)
def test_row_panels_are_monotonic_in_y(dashboard: Path) -> None:
    """Rows partition the board top to bottom; out-of-order rows re-parent panels."""
    rows = [p for p in _panels(dashboard) if p.get("type") == "row"]
    ys = [_rect(p)[1] for p in rows]

    assert ys == sorted(ys), (
        f"{dashboard.name}: row panels are not in increasing y order "
        f"({ys}). Grafana assigns each panel to the nearest row above it, so "
        "an out-of-order row silently moves panels into the wrong section: "
        + ", ".join(f"{_label(p)} @ y={_rect(p)[1]}" for p in rows)
    )


def test_overlap_detector_is_not_vacuous() -> None:
    """The geometry check must actually reject a board that overlaps.

    Without this the three assertions above would stay green if ``_overlaps``
    were ever weakened to a constant ``False`` — a gate that cannot fail is
    worse than no gate, because it reads as coverage.
    """
    # Adjacency is not overlap: the second panel starts exactly where the first
    # one ends, which is how every correct reflow stacks panels.
    assert not _overlaps((0, 0, 12, 8), (0, 8, 12, 8))
    assert not _overlaps((0, 0, 12, 8), (12, 0, 12, 8))
    # A shared interior column and row IS overlap.
    assert _overlaps((0, 0, 12, 8), (0, 7, 12, 8))
    assert _overlaps((0, 0, 24, 8), (12, 4, 12, 8))
    # Full containment.
    assert _overlaps((0, 0, 24, 8), (4, 2, 4, 2))
