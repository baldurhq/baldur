"""Web console handler unit tests — 536 D3/D4/D8/D9/D12.

Verification targets (handler functions called directly, no live socket):
- ``console_page(ctx)`` — enable-gate state transition (True→200 / False→404),
  text/html content-type, and no leftover placeholders after substitution.
- ``_panel_visibility()`` — registry-presence gating (register "pro" → visible;
  reset / non-"pro" provider → hidden) and the v1.0 leak-guard key set.
- ``_safe_json_for_script(data)`` — ``<`` / ``>`` / ``&`` escaping and
  ``</script>`` break-out prevention.
- CSP nonce injection — per-request freshness and header⇔body nonce match.

Panel cases use the real ``ProviderRegistry`` slot ``snapshot()`` test utility
(register / reset a fake ``"pro"`` provider) rather than mocking — ``has_provider``
is a pure dict-membership check, so no instance is ever constructed (D8).
"""

from __future__ import annotations

import json
import re
from unittest.mock import patch

import pytest

from baldur.api.admin.console.handler import (
    _panel_visibility,
    _safe_json_for_script,
    console_page,
)
from baldur.factory import ProviderRegistry
from baldur.interfaces.web_framework import HttpMethod, RequestContext
from baldur.settings.admin import AdminServerSettings

_HANDLER_SETTINGS = "baldur.api.admin.console.handler.get_admin_server_settings"

# The authored PRO panel set (handler._V1_PRO_PANEL_SLOTS keys). Hardcoded here
# so a v1.1 slot leaking into the map (D9 regression) fails this test.
# meta_watchdog joined in #583 — its detect+escalate slice graduated to v1.0 in
# pro-launch-strategy.md Rev 7, after the original 6-key set was frozen.
# runtime_config joined in 662 — the Runtime Config editor, the first post-v1.0
# panel addition (incremental-accumulation window).
_V1_PANEL_KEYS = {
    "emergency",
    "dlq",
    "bulkhead",
    "canary",
    "throttle",
    "governance",
    "meta_watchdog",
    "runtime_config",
}

_CSP_NONCE_RE = re.compile(r"script-src 'nonce-([A-Za-z0-9_-]+)'")
_SCRIPT_NONCE_RE = re.compile(r'<script nonce="([A-Za-z0-9_-]+)"')


def _get_request() -> RequestContext:
    return RequestContext(method=HttpMethod.GET, path="/")


# =============================================================================
# console_page — enable-gate + rendering
# =============================================================================


class TestConsolePageBehavior:
    """GET / render path: enable gate, content-type, placeholder substitution."""

    def test_enabled_console_returns_200_text_html(self):
        with patch(
            _HANDLER_SETTINGS,
            return_value=AdminServerSettings(console_enabled=True),
        ):
            resp = console_page(_get_request())

        assert resp.status_code == 200
        assert resp.content_type == "text/html; charset=utf-8"

    def test_disabled_console_returns_404(self):
        """console_enabled=False → 404 (the JSON API keeps serving — D4)."""
        with patch(
            _HANDLER_SETTINGS,
            return_value=AdminServerSettings(console_enabled=False),
        ):
            resp = console_page(_get_request())

        assert resp.status_code == 404

    def test_rendered_body_has_no_leftover_placeholders(self):
        """Every server-side placeholder must be substituted before serving."""
        with patch(
            _HANDLER_SETTINGS,
            return_value=AdminServerSettings(console_enabled=True),
        ):
            resp = console_page(_get_request())

        body = resp.body
        assert "__BALDUR_PANELS_JSON__" not in body
        assert "__BALDUR_ENTITLEMENT__" not in body
        assert "__BALDUR_CSP_NONCE__" not in body

    def test_rendered_body_injects_panel_map_as_json_object(self):
        """window.__BALDUR_PANELS__ is assigned a parseable JSON object whose
        keys are exactly the v1.0 panel set."""
        with patch(
            _HANDLER_SETTINGS,
            return_value=AdminServerSettings(console_enabled=True),
        ):
            resp = console_page(_get_request())

        match = re.search(r"window\.__BALDUR_PANELS__ = (\{.*?\});", resp.body)
        assert match is not None
        injected = json.loads(match.group(1))
        assert set(injected) == _V1_PANEL_KEYS


# =============================================================================
# _panel_visibility — registry-presence gating (D8/D9)
# =============================================================================


class TestPanelVisibilityBehavior:
    """A PRO panel is visible iff its slot has a provider registered under
    "pro" — registry presence reflects what is running, not license text."""

    def test_panel_visible_when_slot_has_pro_provider(self):
        with ProviderRegistry.emergency_manager.snapshot():
            ProviderRegistry.emergency_manager.register("pro", lambda: object())
            visibility = _panel_visibility()
        assert visibility["emergency"] is True

    def test_panel_hidden_when_slot_has_no_pro_provider(self):
        with ProviderRegistry.emergency_manager.snapshot():
            ProviderRegistry.emergency_manager.reset()
            visibility = _panel_visibility()
        assert visibility["emergency"] is False

    def test_non_pro_provider_does_not_make_panel_visible(self):
        """OSS pre-registers a NoOp default under a non-"pro" name (e.g.
        governance's "oss-noop"), so has_provider("pro") stays False (D9)."""
        with ProviderRegistry.governance.snapshot():
            ProviderRegistry.governance.reset()
            ProviderRegistry.governance.register("oss-noop", lambda: object())
            visibility = _panel_visibility()
        assert visibility["governance"] is False

    def test_meta_watchdog_visible_when_selfhealer_watchdog_has_pro_provider(self):
        """583 D3: registering "pro" on the selfhealer_watchdog slot surfaces the
        meta_watchdog panel. This exercises the new slot-map line end to end — a
        wrong slot name (e.g. a typo) would still pass the key-set leak guard but
        never make the panel visible, so the gating must be asserted against the
        real slot, not just the map keys."""
        with ProviderRegistry.selfhealer_watchdog.snapshot():
            ProviderRegistry.selfhealer_watchdog.register("pro", lambda: object())
            visibility = _panel_visibility()
        assert visibility["meta_watchdog"] is True

    def test_meta_watchdog_hidden_when_selfhealer_watchdog_has_no_pro_provider(self):
        """583 D3: with no "pro" provider on selfhealer_watchdog (the OSS default
        — OSS never registers the slot), the meta_watchdog panel stays hidden,
        identical gating to the other 6 v1.0 PRO panels."""
        with ProviderRegistry.selfhealer_watchdog.snapshot():
            ProviderRegistry.selfhealer_watchdog.reset()
            visibility = _panel_visibility()
        assert visibility["meta_watchdog"] is False

    def test_runtime_config_visible_when_runtime_config_manager_has_pro_provider(self):
        """662: registering "pro" on the runtime_config_manager slot surfaces the
        Runtime Config editor panel. Like meta_watchdog, this exercises the slot
        map line end to end — a wrong slot name would pass the key-set leak guard
        but never gate the panel, so the real slot must be asserted."""
        with ProviderRegistry.runtime_config_manager.snapshot():
            ProviderRegistry.runtime_config_manager.register("pro", lambda: object())
            visibility = _panel_visibility()
        assert visibility["runtime_config"] is True

    def test_runtime_config_hidden_when_runtime_config_manager_has_no_pro_provider(
        self,
    ):
        """662: with no "pro" provider on runtime_config_manager (the OSS default
        — OSS has no RuntimeConfigManager backend), the panel stays hidden,
        identical gating to the other PRO panels."""
        with ProviderRegistry.runtime_config_manager.snapshot():
            ProviderRegistry.runtime_config_manager.reset()
            visibility = _panel_visibility()
        assert visibility["runtime_config"] is False

    def test_panel_map_keys_are_exactly_the_v1_0_set(self):
        """Leak guard: the map keys are the hardcoded v1.0 set, never derived."""
        assert set(_panel_visibility()) == _V1_PANEL_KEYS

    def test_v1_1_slot_registration_never_produces_a_panel_key(self):
        """Registering "pro" on a v1.1 slot (chaos_scheduler) under a notional
        active license must NOT surface a panel — the v1.0/v1.1 split lives in
        authoring (the hardcoded map), not in registration state (D9)."""
        with ProviderRegistry.chaos_scheduler.snapshot():
            ProviderRegistry.chaos_scheduler.register("pro", lambda: object())
            visibility = _panel_visibility()
        assert "chaos_scheduler" not in visibility
        assert "saga" not in visibility
        assert set(visibility) == _V1_PANEL_KEYS

    def test_visibility_values_are_plain_bools(self):
        """JSON-serializable bool values (not truthy objects) for safe embed."""
        for value in _panel_visibility().values():
            assert isinstance(value, bool)


# =============================================================================
# _safe_json_for_script — inline-script escaping (D3)
# =============================================================================


class TestSafeJsonForScript:
    """Escape <, >, & so payload cannot break out of an inline <script>."""

    def test_angle_brackets_and_ampersand_are_unicode_escaped(self):
        result = _safe_json_for_script({"k": "a<b>c&d"})
        assert "\\u003c" in result  # <
        assert "\\u003e" in result  # >
        assert "\\u0026" in result  # &

    def test_raw_angle_brackets_absent_from_output(self):
        result = _safe_json_for_script({"k": "a<b>c"})
        assert "<" not in result
        assert ">" not in result

    def test_script_close_tag_cannot_break_out(self):
        """A literal </script> in the payload must be escaped away."""
        result = _safe_json_for_script("</script><script>alert(1)</script>")
        assert "</script>" not in result
        assert "\\u003c/script\\u003e" in result

    def test_plain_payload_roundtrips_through_json(self):
        """The escaped output is still valid JSON decoding to the input."""
        data = {"emergency": True, "dlq": False}
        assert json.loads(_safe_json_for_script(data)) == data


# =============================================================================
# CSP nonce injection — console_page (D12)
# =============================================================================


class TestConsoleCspBehavior:
    """Per-request nonce keeps CSP's inline-script protection meaningful."""

    def _render(self) -> tuple[str, str]:
        with patch(
            _HANDLER_SETTINGS,
            return_value=AdminServerSettings(console_enabled=True),
        ):
            resp = console_page(_get_request())
        return resp.headers["Content-Security-Policy"], resp.body

    def test_csp_header_present_with_nonce_script_src(self):
        csp, _ = self._render()
        assert "default-src 'self'" in csp
        assert _CSP_NONCE_RE.search(csp) is not None
        # A bare default-src 'self' (no nonce) would block the inline scripts.
        assert "script-src 'self'" not in csp

    def test_csp_header_nonce_matches_inline_script_nonce(self):
        csp, body = self._render()
        header_nonce = _CSP_NONCE_RE.search(csp).group(1)
        body_nonces = set(_SCRIPT_NONCE_RE.findall(body))
        assert body_nonces == {header_nonce}

    def test_nonce_is_fresh_per_request(self):
        csp_first, _ = self._render()
        csp_second, _ = self._render()
        nonce_first = _CSP_NONCE_RE.search(csp_first).group(1)
        nonce_second = _CSP_NONCE_RE.search(csp_second).group(1)
        assert nonce_first != nonce_second


# =============================================================================
# Asset packaging — console.html ships as in-package data (D3)
# =============================================================================


class TestConsoleAssetPackaging:
    """The console.html asset must be locatable via importlib.resources so it
    ships inside the wheel (hatchling auto-includes in-package data, like
    py.typed) and loads from an installed package, not just the source tree."""

    def test_console_html_is_importable_package_data(self):
        from importlib.resources import files

        asset = files("baldur.api.admin.console") / "console.html"
        assert asset.is_file()

    def test_console_html_carries_substitution_placeholders(self):
        """The raw asset still carries the handler-substituted placeholders;
        guards against an accidental hardcode that would skip injection."""
        from importlib.resources import files

        raw = (files("baldur.api.admin.console") / "console.html").read_text(
            encoding="utf-8"
        )
        assert "__BALDUR_PANELS_JSON__" in raw
        assert "__BALDUR_CSP_NONCE__" in raw


# =============================================================================
# Asset structure — DLQ drill-down + structured render (540 D1–D5)
# =============================================================================


def _console_html() -> str:
    from importlib.resources import files

    return (files("baldur.api.admin.console") / "console.html").read_text(
        encoding="utf-8"
    )


class TestConsoleAssetStructure:
    """Static-asset content assertions for the 540 frontend enhancements.

    The new logic is browser JS — not executable under pytest without a JS
    engine — so the realistic surface is content assertions on the packaged
    console.html (raw-dump removed, drill-down wired, safe-DOM preserved, no
    new <script> tag)."""

    def test_raw_json_dump_replaced_by_structured_renderer(self):
        """D1: the raw JSON.stringify(data, null, 2) <pre> dump is gone and the
        structured renderer anchor (severityOf) is present."""
        raw = _console_html()
        assert "JSON.stringify(data, null, 2)" not in raw
        assert "severityOf" in raw

    def test_dlq_drilldown_endpoints_wired(self):
        """D2/D3/D5: the drill-down lists /dlq/list and wires per-row
        retry/resolve path construction."""
        raw = _console_html()
        assert "/dlq/list" in raw
        assert "/retry" in raw
        assert "/resolve" in raw

    def test_no_functional_inner_html(self):
        """D2 (#536 D3): rendering stays textContent-based — no innerHTML."""
        assert ".innerHTML" not in _console_html()

    def test_no_new_script_tag_added(self):
        """DA1: all new JS lives in the existing <script> block — a new inline
        <script> would need a third CSP-nonce placeholder."""
        assert _console_html().count("__BALDUR_CSP_NONCE__") == 2

    # --- Console UX follow-ups (post-540) ---------------------------------

    def test_action_modal_dismisses_after_success(self):
        """A successful action switches the modal to a dismiss-only state:
        Confirm is hidden (no double-run) and Cancel becomes 'Close'."""
        raw = _console_html()
        assert '.textContent = "Close"' in raw
        assert 'style.display = "none"' in raw

    def test_resolve_uses_plain_text_note_field(self):
        """Resolve note is a labelled plain-text field (bodyField), not a
        raw-JSON textarea: the old '{"notes": ""}' default is gone."""
        raw = _console_html()
        assert "bodyField" in raw
        assert "modal-bodyfield" in raw
        assert "Resolution note" in raw
        assert '{"notes": ""}' not in raw

    def test_panel_freshness_and_autorefresh_present(self):
        """Per-panel 'updated Xs ago' (relAgo) + header auto-refresh toggle."""
        raw = _console_html()
        assert "relAgo" in raw
        assert "autorefresh-toggle" in raw

    def test_dlq_filter_is_drift_free(self):
        """DLQ drill-down has a filter row; status options derive from the live
        by_status counts (never a hardcoded FailedOperationStatus mirror)."""
        raw = _console_html()
        assert "dlq-filter" in raw
        assert "by_status" in raw

    # --- 542 D6: faceted filter — sample dropped, /dlq/facets wired -------

    def test_dlq_domain_sample_fetch_is_removed(self):
        """D6 / G4: the prior ``/dlq/list?page=1&page_size=100`` sample
        used to source domain options is GONE — a 100-entry sample silently
        misses domains at PRO scale (G4)."""
        raw = _console_html()
        assert "page_size=100" not in raw
        # The sample-derived `seen[row.domain]` accumulator is also gone
        # (the row-cell `row.domain` in the table renderer is unrelated).
        assert "seen[row.domain]" not in raw

    def test_dlq_facets_endpoint_is_wired(self):
        """D6: both dropdowns are sourced from ``/dlq/facets`` and the
        endpoint is called as the panel opens (refreshFacets)."""
        raw = _console_html()
        assert "/dlq/facets" in raw
        assert "refreshFacets" in raw

    def test_dlq_status_and_domain_dropdowns_cross_filter(self):
        """D6: each ``change`` handler triggers ``refreshFacets()`` so the
        other dropdown's options narrow (status↔domain cross-filter, D2)."""
        raw = _console_html()
        # Each select's change handler must wire both refreshFacets() AND
        # load() — narrows the cross-axis options AND re-queries the list.
        assert raw.count("refreshFacets(); load()") >= 2

    def test_dlq_active_zero_count_selection_is_preserved(self):
        """D6: when the operator's active selection has 0 count under the
        new scope, the option is still rendered as ``value (0)`` so their
        filter is not silently dropped."""
        raw = _console_html()
        # The synthesised zero-count option appends "(0)" as a literal.
        assert '"(0)"' in raw or "+ ' (0)'" in raw or '+ " (0)"' in raw

    # --- 582: System Control panel (kill switch + dry-run) ----------------

    def test_system_control_panel_wired(self):
        """582 D1: the OSS ``system`` panel surfaces the kill switch + dry-run
        controls — status source plus all four ``/system/*`` action paths.

        Raw-asset string presence (precedent: ``test_dlq_drilldown_endpoints_wired``)
        — ``PANELS`` is client-side JS, not executable under pytest."""
        raw = _console_html()
        assert 'id: "system"' in raw
        assert '"/system/status"' in raw
        assert '"/system/enable"' in raw
        assert '"/system/disable"' in raw
        assert '"/system/dry-run/enable"' in raw
        assert '"/system/dry-run/disable"' in raw

    def test_system_control_panel_is_oss(self):
        """582 D1/Testability: the ``system`` panel is ``pro:false`` — it stays
        out of the PRO leak-guard sets (``_V1_PANEL_KEYS`` /
        ``_V1_PRO_PANEL_SLOTS``) and renders for OSS deployments."""
        assert "system" not in _V1_PANEL_KEYS
        raw = _console_html()
        # The system panel entry declares pro:false (same block as its status).
        match = re.search(
            r'\{\s*id:\s*"system".*?status:\s*"/system/status"', raw, re.DOTALL
        )
        assert match is not None
        assert "pro: false" in match.group(0)

    def test_system_control_server_required_bodies_present(self):
        """582 D1: the two server-required bodies are wired client-side, else
        each POST returns HTTP 400 and the control silently no-ops.

        - ``disable`` carries a ``reason`` ``bodyField`` marked ``required: true``
          (the server 400s on an empty reason; ``required`` consumes #583 D8's
          client-side validation, inert/graceful pre-D8).
        - ``dry-run disable`` defaults its body to ``{"confirm": true}`` (the
          server 400s without it)."""
        raw = _console_html()
        assert 'key: "reason"' in raw
        assert "required: true" in raw
        assert '{"confirm": true}' in raw

    # --- 583: Meta-Watchdog panel + modal-framework hardening (D1/D2/D7/D8) ---

    def test_meta_watchdog_panel_wired(self):
        """583 D1: the PRO ``meta_watchdog`` panel surfaces the status source
        plus both OPERATOR action paths (force-check + escalation-test).

        Raw-asset string presence (precedent: ``test_system_control_panel_wired``)
        — ``PANELS`` is client-side JS, not executable under pytest."""
        raw = _console_html()
        assert 'id: "meta_watchdog"' in raw
        assert '"/meta-watchdog/status"' in raw
        assert '"/meta-watchdog/force-check"' in raw
        assert '"/meta-watchdog/escalation-test"' in raw

    def test_meta_watchdog_panel_is_pro(self):
        """583 D3: the ``meta_watchdog`` panel is ``pro:true`` — gated on the
        ``selfhealer_watchdog`` registry slot, hidden for OSS deployments."""
        raw = _console_html()
        match = re.search(
            r'\{\s*id:\s*"meta_watchdog".*?status:\s*"/meta-watchdog/status"',
            raw,
            re.DOTALL,
        )
        assert match is not None
        assert "pro: true" in match.group(0)

    def test_escalation_test_carries_real_notification_warn(self):
        """583 D2: escalation-test actually pages every configured channel, so
        its action carries a ``warn`` caption that ``openActionModal`` renders
        into an ``.action-warn`` box. The friction tier stays ``operator`` (the
        route is OPERATOR) — raising it to ``admin`` would display the modal's
        hardcoded ``BALDUR_ADMIN_UNLOCK=1`` lie (536 D10 invariant)."""
        raw = _console_html()
        assert "REAL notification" in raw
        # openActionModal renders action.warn into an .action-warn caption box.
        assert 'el("div", "action-warn", action.warn)' in raw
        esc = re.search(r'label:\s*"Escalation test".*?\}', raw, re.DOTALL)
        assert esc is not None
        assert "warn:" in esc.group(0)
        assert 'risk: "operator"' in esc.group(0)
        assert 'risk: "admin"' not in esc.group(0)

    def test_force_check_has_no_warn(self):
        """583 D2: force-check is an internal health check with no outbound
        effect — it carries no ``warn`` and stays ``risk:"operator"``."""
        raw = _console_html()
        fc = re.search(r'label:\s*"Force check".*?\}', raw, re.DOTALL)
        assert fc is not None
        assert "warn" not in fc.group(0)
        assert 'risk: "operator"' in fc.group(0)

    def test_double_submit_guard_disables_confirm_in_flight(self):
        """583 D7: ``runPendingAction`` disables the Confirm button immediately
        before the outstanding request (so an escalation-test double-click can
        not page twice) and re-enables it in the failure branches; the success
        branch hides the button instead. Raw-asset presence — no JS engine."""
        raw = _console_html()
        assert "confirmBtn.disabled = true" in raw
        assert "confirmBtn.disabled = false" in raw
        # The guard arms immediately before the request dispatch.
        guard = re.search(
            r"confirmBtn\.disabled = true;.*?adminFetch\(path, opts\)",
            raw,
            re.DOTALL,
        )
        assert guard is not None

    def test_double_submit_guard_reset_on_modal_open(self):
        """583 D7: ``openActionModal`` re-enables Confirm when a fresh action is
        opened, clearing any leftover in-flight/post-success disable — so the
        guard is strictly per-request and never sticks across modal opens. (The
        re-arm-on-failure sites live in ``runPendingAction``; this one is in the
        open path.)"""
        raw = _console_html()
        reset = re.search(
            r"function openActionModal\(action\).*?confirmBtn\.disabled = false",
            raw,
            re.DOTALL,
        )
        assert reset is not None

    def test_bodyfield_required_validation_present(self):
        """583 D8: an empty required ``bodyField`` aborts the submit inline with
        a 'Provide a …' notice before sending (consumed by #582's disable reason
        field, inert/graceful before this branch landed)."""
        raw = _console_html()
        req = re.search(
            r"action\.bodyField\.required && !fieldVal.*?return;",
            raw,
            re.DOTALL,
        )
        assert req is not None
        assert "Provide a " in req.group(0)

    # --- 550: Canary panel — lifecycle drill-down + create/panic actions ----

    def _canary_drilldown_fn(self, raw: str) -> str:
        """The renderCanaryDrilldown function body, sliced up to the DRILLDOWNS
        dispatch table that follows it (placed there by 550 D3)."""
        fn = re.search(
            r"function renderCanaryDrilldown\(container\).*?(?=\n  var DRILLDOWNS)",
            raw,
            re.DOTALL,
        )
        assert fn is not None
        return fn.group(0)

    def _canary_panel_block(self, raw: str) -> str:
        """The canary PANELS entry, sliced up to the next (throttle) panel."""
        panel = re.search(
            r'\{ id: "canary".*?(?=\n    \{ id: "throttle")', raw, re.DOTALL
        )
        assert panel is not None
        return panel.group(0)

    def test_canary_drilldown_and_dispatch_wired(self):
        """550 D3: the canary panel renders via a per-rollout drill-down,
        resolved through the generalized DRILLDOWNS dispatch table (DLQ + canary)
        rather than a hardcoded renderDlqDrilldown call. fetchInto is hoisted to
        module scope so both drill-downs share one fetch/error-render wrapper."""
        raw = _console_html()
        assert "renderCanaryDrilldown" in raw
        assert "DRILLDOWNS" in raw
        assert 'drilldown: "dlq"' in raw
        assert 'drilldown: "canary"' in raw
        # fetchInto is now a module-level function (shared), not nested-only.
        assert "function fetchInto(" in raw

    def test_canary_lifecycle_action_paths_present(self):
        """550 D3/SC1: all six lifecycle action suffixes are wired against the
        canary rollout resource (the rollout_id is prepended at click time:
        /canary/rollouts/{id}/<action>), so no id is hand-typed."""
        raw = _console_html()
        for suffix in [
            "/start",
            "/promote",
            "/pause",
            "/resume",
            "/rollback",
            "/cancel",
        ]:
            assert suffix in raw, f"missing canary lifecycle suffix {suffix}"
        assert '"/canary/rollouts/" + encodeURIComponent(rolloutId)' in raw

    def test_canary_lifecycle_buttons_are_admin_risk(self):
        """550 D1: every canary lifecycle button is built risk:"admin" (typed
        CONFIRM) — no OPERATOR-tier canary action exists (recovery.py registers
        all canary routes PermissionLevel.ADMIN)."""
        fn = self._canary_drilldown_fn(_console_html())
        assert 'risk: "admin"' in fn
        assert 'risk: "operator"' not in fn

    def test_canary_panel_create_and_panic_actions(self):
        """550 D2/D4/#6: create and panic-rollback stay panel-level; both are
        risk:"admin"; the create bodyDefault is a complete nested template
        (config_type + new_values + stages); the panic label conveys its global
        ALL-active-rollout scope before the typed CONFIRM (#6 blast-radius)."""
        block = self._canary_panel_block(_console_html())
        assert 'drilldown: "canary"' in block
        # Panel actions POST to the exact existing endpoints (D2/SC3).
        assert '"/canary/rollouts"' in block
        assert '"/canary/panic-rollback"' in block
        # No OPERATOR-tier canary action; both panel actions are ADMIN (D1/SC2).
        assert 'risk: "operator"' not in block
        assert block.count('risk: "admin"') >= 2
        # Panic label states the ALL-active scope (#6 blast-radius).
        assert "ALL active" in block
        # Create template carries the nested create payload shape (D4/SC5).
        assert "config_type" in block
        assert "new_values" in block
        assert "stages" in block

    def test_canary_metrics_button_in_drilldown(self):
        """550 D5: the per-rollout detail carries a read-only Metrics button
        that GETs /canary/rollouts/{id}/metrics — decision support for the
        promote/rollback buttons that live in the same detail view."""
        fn = self._canary_drilldown_fn(_console_html())
        assert "Metrics" in fn
        assert 'base + "/metrics"' in fn

    def _canary_lifecycle_block(self, raw: str) -> str:
        """The CANARY_LIFECYCLE action array (declared just above
        renderCanaryDrilldown), sliced to its closing bracket."""
        block = re.search(r"var CANARY_LIFECYCLE = \[.*?\n  \];", raw, re.DOTALL)
        assert block is not None
        return block.group(0)

    def test_canary_action_body_shapes_match_handler_signatures(self):
        """550 D2: each lifecycle action's modal body shape mirrors the matching
        handler's body reads. The critical case is promote.force — a BOOLEAN —
        which MUST use the raw-JSON ``body`` modal (sends a real JSON ``false``),
        NOT a ``bodyField`` (which string-wraps the textarea into a truthy
        ``"false"`` → silent ``force=True``). Locking this guards the D2
        correction against a future "simplify promote to a bodyField" regression
        that no path/suffix assertion would catch. rollback carries an optional
        ``reason`` bodyField; panic-rollback's body template carries reason +
        emergency_code."""
        lifecycle = self._canary_lifecycle_block(_console_html())
        # promote: raw-JSON boolean body, default {"force": false} (boolean-safe).
        assert 'suffix: "/promote", label: "Promote", body: true' in lifecycle
        assert '{"force": false}' in lifecycle
        # promote must NOT be a bodyField — a "force" bodyField is the D2 bug.
        assert 'key: "force"' not in lifecycle
        # rollback: optional reason bodyField (empty → server default applies).
        assert 'key: "reason"' in lifecycle
        # panic-rollback body template carries reason + emergency_code (D2).
        assert "emergency_code" in self._canary_panel_block(_console_html())

    # --- 662: Runtime Config editor panel (D5/D6) -------------------------

    def test_runtime_config_panel_wired(self):
        """662 D6: the runtime_config panel sources status from /config/editable,
        renders via the runtime_config drill-down, and wires the global reset.

        Raw-asset string presence (precedent: ``test_meta_watchdog_panel_wired``)
        — ``PANELS`` is client-side JS, not executable under pytest."""
        raw = _console_html()
        assert 'id: "runtime_config"' in raw
        assert '"/config/editable"' in raw
        assert 'drilldown: "runtime_config"' in raw
        assert "renderRuntimeConfigDrilldown" in raw
        assert '"/config/reset"' in raw

    def test_runtime_config_panel_is_pro(self):
        """662 D0: the runtime_config panel is pro:true — gated on the
        runtime_config_manager registry slot, hidden for OSS deployments."""
        raw = _console_html()
        match = re.search(
            r'\{ id: "runtime_config".*?status: "/config/editable"', raw, re.DOTALL
        )
        assert match is not None
        assert "pro: true" in match.group(0)

    def test_runtime_config_all_paths_stay_under_config_domain(self):
        """662 D6 / G44: every runtime_config action/status path stays under
        ``/config`` so the panel's status domain == action domain. A stray
        non-/config path would break the G44 console-coverage invariant."""
        raw = _console_html()
        block = re.search(
            r'\{ id: "runtime_config".*?actions: \[.*?\] \}', raw, re.DOTALL
        )
        assert block is not None
        # The per-section edit (PUT /config/{slug}) lives in the drilldown JS;
        # the panel-level paths are status + reset, both under /config.
        assert "/config/editable" in block.group(0)
        assert "/config/reset" in block.group(0)

    def test_runtime_config_apply_uses_diff_confirm_and_prebuilt_body(self):
        """662 D6: the edit-confirm modal renders a per-field current→proposed
        diff (diffRows) and submits a pre-built rawBody payload (typed numbers,
        avoiding the string-wrap clamp bypass). Locks the diff-confirm + rawBody
        apply against a regression to a generic confirm/bodyField."""
        raw = _console_html()
        assert "diffRows" in raw
        assert "rawBody" in raw

    def test_runtime_config_clamp_hint_present(self):
        """662 D4/D6: number fields with a VALIDATION_RULES bound (clamps flag)
        render an inline advisory hint that out-of-range values are clamped."""
        assert "out-of-range will be clamped" in _console_html()

    def test_runtime_config_audit_badge_present(self):
        """662 D5: the panel renders an audit-status badge driven by
        ``audit_enabled`` — when OFF it warns the change reason is not durably
        retained (no "reason theater")."""
        assert "audit_enabled" in _console_html()

    # --- 681: instrument-principle visual redesign (D12 anchors) -----------
    # CSS is not executed under pytest, so the redesign's verifiable surface is
    # content assertions on the packaged asset (precedent: this whole class).

    # The old-palette inventory retired by 681 D1 (removal-type full sweep). Any
    # survivor leaves a stale-temperature fragment after the token swap.
    _RETIRED_PALETTE = [
        "0f1419",
        "1a212b",
        "2b3440",
        "d7dde4",
        "8a97a5",
        "4ea1f3",
        "f3614e",
        "5fcf80",
        "f3c14e",
        "354150",
        "4a2620",
        "5e2e26",
        "06121f",
        "3a2e12",
        "2d2438",
        "c08ef3",
        "1e3a28",
        "3a3312",
        "3a1f1c",
        "222b36",
        "244a31",
        "#233",
        "95,207,128",
        "243,193,78",
        "243,97,78",
    ]

    def test_oss_tier_badge_removed(self):
        """681 D5: the per-panel OSS badge is gone (a badge on all 11 panels
        carries no information) — the tier badge is PRO-only. The ``"OSS"``
        string literal is absent; the ``"PRO"`` render remains."""
        raw = _console_html()
        assert '"OSS"' not in raw
        assert '"PRO"' in raw

    def test_null_fields_collapse_anchor_present(self):
        """681 D4: an object's null-valued fields fold to one clamped
        "not set — a · b (+N more)" line (the renderer emits the "not set"
        prefix) instead of one ``null`` row each."""
        assert "not set" in _console_html()

    @pytest.mark.parametrize("severity", ["ok", "warn", "danger"])
    def test_severity_glyph_rule_present(self, severity: str):
        """681 D2: state is encoded by color + shape — each severity has a
        CSS-drawn ``::before`` glyph (filled dot / half / hollow ring) so it
        survives colorblindness."""
        raw = _console_html()
        assert re.search(r"\.tag\." + severity + r"::before", raw) is not None

    def test_retired_palette_absent_and_new_palette_present(self):
        """681 D1: the old palette is fully retired and the v5 token anchors
        (``--ink`` #0b0d10, ``--dawn`` #e8a33d) are present."""
        raw = _console_html().lower()
        for stale in self._RETIRED_PALETTE:
            assert stale.lower() not in raw, f"retired palette value survived: {stale}"
        assert "0b0d10" in raw
        assert "e8a33d" in raw

    def test_opacity_dimming_of_calm_panels_removed(self):
        """681 D7: healthy/none panels are no longer opacity-dimmed (that is
        legibility loss, not attention routing) — no ``data-health="ok"`` or
        ``"none"`` rule carries ``opacity``. Rules are single-line, so a
        line-scoped scan is sound."""
        for line in _console_html().splitlines():
            if re.search(r'data-health="(ok|none)"', line):
                assert "opacity" not in line

    def test_null_collapse_toggle_has_aria_expanded(self):
        """681 D4: the null-collapse toggle (and the retrofitted meta-watchdog
        worker toggle) are ``<button>``s carrying ``aria-expanded`` synced to
        the open state — screen-reader parity for the silent ▸/▾ glyph."""
        assert "aria-expanded" in _console_html()
