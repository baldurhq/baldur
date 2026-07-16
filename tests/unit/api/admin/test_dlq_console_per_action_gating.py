"""Console per-action tier gating for the DLQ panel (708 D5).

708 un-gates the DLQ *read + single-entry* surface for OSS while keeping the
batch/management actions PRO. In the console this is per-action gating rather
than whole-panel gating:

- The DLQ panel is ``pro: false`` (renders for OSS), so an OSS operator sees the
  entry drilldown + the read-only cleanup-stats body.
- The batch/management actions (Replay / Archive / Purge) carry a per-action
  ``pro: true`` flag; the action-render loop hides a ``pro: true`` action when
  ``window.__BALDUR_PANELS__[panelId] !== true`` (the same registry map that
  drives whole-panel visibility). Retry / Resolve are drilldown-rendered per
  entry (no panel-level action), so they are never per-action gated.
- The ``dlq`` slot stays in ``_V1_PRO_PANEL_SLOTS`` so ``__BALDUR_PANELS__``
  still reflects PRO presence — now consumed for per-action gating.

``console.html`` is a static HTML/JS asset (not importable Python), so the panel
definition + render gate are regex-parsed from the raw asset — the precedent set
by the console panel-coverage fitness function.
"""

from __future__ import annotations

import re
from importlib.resources import files

import pytest

from baldur.api.admin.console.handler import _V1_PRO_PANEL_SLOTS

# The per-action-gated batch/management actions (D5/D6). Retry / Resolve are
# deliberately absent — they are drilldown-rendered, never panel actions.
_EXPECTED_PRO_ACTIONS = {"Replay", "Archive", "Purge"}

# The render-time gate: a pro:true action is hidden unless the panel's registry
# map value is exactly true. This is what makes per-action gating work.
_ACTION_GATE_SNIPPET = "a.pro && (window.__BALDUR_PANELS__ || {})[p.id] !== true"


def _console_html() -> str:
    return (files("baldur.api.admin.console") / "console.html").read_text(
        encoding="utf-8"
    )


def _dlq_panel_chunk(raw: str) -> str:
    """The ``PANELS`` slice for the DLQ panel: ``id: "dlq"`` up to the next panel.

    A panel object opens with ``id: "..."`` while its nested action / bodyField
    objects carry only ``label:`` / ``path:`` / ``key:`` (never ``id:``), so the
    slice from the DLQ id boundary to the next id boundary is exactly the DLQ
    panel (definition + actions).
    """
    match = re.search(r'id:\s*"dlq".*?(?=\bid:\s*")', raw, re.DOTALL)
    assert match, 'DLQ panel (id: "dlq") not found in console.html PANELS'
    return match.group(0)


class TestConsoleDlqPerActionGating:
    """The DLQ panel renders for OSS; only its batch/management actions gate."""

    def test_dlq_stays_in_pro_panel_slot_map(self):
        """The ``dlq`` slot is retained so ``__BALDUR_PANELS__['dlq']`` carries the
        per-action PRO-presence signal (D5 — slot kept, panel flipped)."""
        assert "dlq" in _V1_PRO_PANEL_SLOTS

    def test_dlq_panel_renders_for_oss(self):
        """Panel-level ``pro: false`` — the DLQ panel is visible in a pure-OSS
        console (``panelVisible`` short-circuits ``if (!p.pro) return true``)."""
        chunk = _dlq_panel_chunk(_console_html())
        assert re.search(
            r'id:\s*"dlq"\s*,\s*title:\s*"[^"]*"\s*,\s*pro:\s*false', chunk
        ), "the DLQ panel must be pro:false so it renders for OSS"

    def test_batch_and_management_actions_carry_per_action_pro_flag(self):
        """Replay / Archive / Purge each carry a per-action ``pro: true`` flag."""
        chunk = _dlq_panel_chunk(_console_html())
        # A label whose action object sets pro:true before any nested `{`
        # (bodyField/bodyFields). Retry/Resolve are not panel actions, so they
        # never appear here.
        pro_actions = set(re.findall(r'label:\s*"([^"]+)"[^{]*?pro:\s*true', chunk))
        assert pro_actions == _EXPECTED_PRO_ACTIONS, (
            f"DLQ panel per-action pro:true set drifted: {pro_actions} "
            f"(expected {_EXPECTED_PRO_ACTIONS})"
        )

    def test_retry_resolve_are_not_gated_panel_actions(self):
        """Retry / Resolve are drilldown-rendered per entry — never a pro-gated
        panel action, so the OSS operator can always retry/resolve."""
        chunk = _dlq_panel_chunk(_console_html())
        assert 'label: "Retry"' not in chunk
        assert 'label: "Resolve"' not in chunk
        assert 'drilldown: "dlq"' in chunk

    def test_dlq_panel_body_is_the_oss_read_stats_endpoint(self):
        """The panel body reads ``/dlq/cleanup/stats`` (VIEWER, routed OSS)."""
        chunk = _dlq_panel_chunk(_console_html())
        assert 'status: "/dlq/cleanup/stats"' in chunk

    def test_render_loop_has_the_per_action_gate(self):
        """The action-render loop hides a pro:true action unless the panel's
        registry map value is true — the mechanism per-action gating relies on."""
        assert _ACTION_GATE_SNIPPET in _console_html()

    @pytest.mark.parametrize(
        "action_path", ["/dlq/replay", "/dlq/cleanup/archive", "/dlq/cleanup/purge"]
    )
    def test_each_pro_action_targets_a_pro_route(self, action_path):
        """Each per-action-gated button targets one of the PRO-only routes."""
        chunk = _dlq_panel_chunk(_console_html())
        assert f'path: "{action_path}"' in chunk
