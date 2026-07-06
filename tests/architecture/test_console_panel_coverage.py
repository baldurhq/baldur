"""Impl 585 — G35 admin-console panel-coverage fitness function.

A mutating OPERATOR/ADMIN control **feature-domain** (a route path-prefix on the
admin route registry) MUST be exposed by a ``console.html`` ``PANELS`` entry or
be listed in ``_PANEL_EXEMPT_DOMAINS`` with a justification (G1). This closes the
coverage-direction blind spot that let #582 (System Control / kill switch) and
#583 (Meta-Watchdog) ship routed control features with no console surface — the
existing console leak-guards (``test_console_handler.py``) are anti-leak only
(they catch an *extra* v1.1 panel, but are structurally blind to a *missing*
panel because a forgotten panel drops out of the slot map and its hardcoded
oracle simultaneously, so equality still holds).

G35 also pins the one un-pinned edge of the 3-way panel-set triangle the existing
leak-guards already half-pin: ``PANELS pro:true ids == set(_V1_PRO_PANEL_SLOTS)``
(G2/D4). It does NOT touch the test-module oracle ``_V1_PANEL_KEYS`` — collapsing
that into the slot map would degrade the leak-guard to a tautology (D4).

The gate is keyed on the **domain** (first non-parameter path segment), not the
individual route (D1): the console exposes one panel per feature-domain showing a
curated subset of actions, so a route-level predicate would false-positive on the
non-action mutating routes inside already-paneled domains. The blind spot the gate
targets is a feature-domain with *no panel at all* (the #582/#583 shape).

Impl 634 adds **G44** (``TestConsolePanelStatusActionCoherence``), a complementary
*intra-panel* coherence gate: for every ``PANELS`` entry that has ≥1 action, every
action's domain MUST equal the panel's status domain (unless the panel id is in the
enforced-empty ``_CROSS_DOMAIN_PANELS`` allowlist). G35 unions a panel's status +
action domains into one covered-set, so it is structurally blind to a panel whose
status reads one subsystem while its action mutates another — exactly the bug that
let the Circuit Breakers panel display *audit-backend* CBs while its Reset action
targeted *application service* CBs. G44 splits the ``PANELS`` block per panel (on
``id:`` boundaries) and asserts status-domain == action-domain(s) per panel.

Rule registry:
``ARCHITECTURE.md#g35-console-panel-coverage``
``ARCHITECTURE.md#g44-console-panel-status-action-coherence``
"""

from __future__ import annotations

import re
from importlib.resources import files

import pytest

from baldur.api.admin.console.handler import _V1_PRO_PANEL_SLOTS
from baldur.api.admin.registry import AdminRoute, _create_admin_registry
from baldur.interfaces.web_framework import HttpMethod, PermissionLevel

# A "control" route = a mutating method at operator-grade permission (D1). PATCH
# is included (chaos config routes are PATCH/admin). The AUTHENTICATED-tier
# mutating routes (/gate/reset, /config/gate) are deliberately excluded — they
# are low-privilege self-service config toggles, not operator incident controls.
_MUTATING_METHODS = frozenset(
    {HttpMethod.POST, HttpMethod.PUT, HttpMethod.PATCH, HttpMethod.DELETE}
)
_CONTROL_LEVELS = frozenset({PermissionLevel.OPERATOR, PermissionLevel.ADMIN})

# Parse-sanity floor (D5): the live v1.0 PRO panel count, derived from the
# server-side slot map — NOT a magic literal, and self-maintaining (it tracks the
# slot map, so adding a v1.0 PRO panel moves the floor up automatically). A regex
# parse that drifts to empty returns fewer than this and fails AS A PARSER ERROR
# before any coverage assertion can mis-diagnose it.
_PANEL_FLOOR = len(_V1_PRO_PANEL_SLOTS)

# console.html is a static HTML/JS asset, not importable Python — PANELS data is
# regex-parsed from the raw asset (precedent: test_console_handler.py asset
# parsing). The parsing assumes the established formatting (one panel per
# ``{...}``, ``pro:`` after ``title:``, status/action paths as ``"/..."``
# literals); a formatting change breaks the parse loudly via _PANEL_FLOOR.
_PANELS_BLOCK_RE = re.compile(r"var PANELS = \[(.*?)\];", re.DOTALL)
# A panel's status endpoint + each action's route path, both ``"/..."`` literals.
_PANEL_PATH_RE = re.compile(r'(?:status|path):\s*"(/[^"]*)"')
# A pro:true panel's id. ``id``, ``title``, ``pro`` are contiguous and precede any
# nested ``{`` (the actions array), so a flat regex is unambiguous.
_PANEL_PRO_ID_RE = re.compile(
    r'id:\s*"([^"]+)"\s*,\s*title:\s*"[^"]*"\s*,\s*pro:\s*true'
)

# G44 per-panel parse (D4): a panel object always opens with ``id: "..."`` while
# nested action / ``bodyField`` objects carry only ``label:``/``path:``/``key:``
# (never ``id:``), so the PANELS block slices into per-panel chunks on ``id:``
# boundaries. Within a chunk the first ``status:`` literal is the panel's status
# read and every ``path:`` literal is an action route. Both value forms must start
# with ``/`` so a multi-line ``bodyDefault`` JSON cannot false-match.
_PANEL_ID_RE = re.compile(r'id:\s*"([^"]+)"')
_PANEL_STATUS_RE = re.compile(r'status:\s*"(/[^"]*)"')
_PANEL_ACTION_RE = re.compile(r'path:\s*"(/[^"]*)"')

# Intentionally un-paneled control domains (D2). domain -> non-empty justification.
# Inline dict (not baseline.yaml) because a route domain has no natural
# (file, symbol) key — the domain->registrar mapping is 1:N. The PR diff is the
# review gate (precedent: the inline core_dependency_modules allowlist). The
# v1.1-deferred entries name their promotion tracker (536 OOS) by convention so a
# future cleanup pass knows what to re-check.
#
# Posture amendment (662): the 536 D9 "config is managed via REST/settings, not a
# generic console editor" posture is REVERSED for runtime-editable config
# *values* only — the Runtime Config editor panel (console.html id "runtime_config")
# now covers the /config domain, so its exemption row is removed (the panel's
# /config/editable status + /config/reset action cover it; G35 advisory no longer
# needs it). The remaining low-level config surfaces below (config-history,
# l2-storage, drift-threshold, tiering, rollback) stay REST-only — they are not
# runtime-editable values and keep the 536 D9 posture.
_PANEL_EXEMPT_DOMAINS: dict[str, str] = {
    # v1.1 feature-domains — a console panel arrives with the v1.1 promotion
    # ceremony (536 OOS). error-budget/FinOps reconciliation and chaos/postmortem
    # are v1.1, not v1.0 launch-set features.
    "chaos": "v1.1 feature — console panel arrives with the v1.1 promotion (536 OOS)",
    "postmortem": "v1.1 feature — console panel arrives with the v1.1 promotion (536 OOS)",
    "finops": "v1.1 feature (error-budget/FinOps) — panel arrives with the v1.1 promotion (536 OOS)",
    "reconciliation": "v1.1 feature — console panel arrives with the v1.1 promotion (536 OOS)",
    "learning": "v1.1 feature — console panel arrives with the v1.1 promotion (536 OOS)",
    "auto-tuning": "v1.1-Deferred feature (#20a) — console panel arrives with the v1.1 promotion; v1.0 has no auto-tuning control surface",
    # Low-level config-management / tuning surfaces — managed via REST/settings,
    # not an incident-control console panel (536 D9 posture). NOTE: the "config"
    # domain itself is NOT exempt anymore — the Runtime Config editor panel (662)
    # covers runtime-editable config values; only these low-level surfaces remain.
    "config-history": "internal config-management surface, not an incident-control panel (536 D9)",
    "l2-storage": "low-level storage tuning surface, not an incident-control panel (536 D9)",
    "blast-radius": "low-level chaos-safety tuning surface, not an incident-control panel (536 D9)",
    "drift-threshold": "low-level drift-tuning surface, not an incident-control panel (536 D9)",
    "resilience": "low-level resilience tuning surface, not an incident-control panel (536 D9)",
    "rollback": "low-level config-rollback surface, not an incident-control panel (536 D9)",
    "tiering": "low-level storage-tiering surface, not an incident-control panel (536 D9)",
    # Other non-incident-control surfaces.
    "compliance": "report-driven compliance surface (run -> report), not an interactive incident panel",
    "grafana": "inbound webhook receiver (/grafana/webhook is external->Baldur ingress), not an operator-clicked control",
    "metric-sync": "internal metric-sync trigger, not an incident control",
}

# Panels that DELIBERATELY mix a status domain with a different action domain (G44,
# D4). Enforced-empty: panel id -> non-empty justification. A coherent panel
# (status domain == every action domain) needs no entry; an intentional exception
# is added here with a reason. Mirrors the inline ``_PANEL_EXEMPT_DOMAINS`` dict —
# a panel id is the natural key for "this panel intentionally spans subsystems",
# and the PR diff is the review gate (a route domain has no natural file/symbol
# key). Kept empty by design: all six action-bearing panels are intra-coherent.
_CROSS_DOMAIN_PANELS: dict[str, str] = {}


def _domain(path: str) -> str | None:
    """First non-parameter path segment, or ``None`` if the path has none (D1).

    ``/control/reset/{service_name}`` -> ``control``; ``/audit/circuit-breakers``
    -> ``audit``. Total over degenerate inputs (never produced by a live route,
    but the helper must not raise on synthetic input): ``/`` -> None,
    ``/{a}/{b}`` -> None (no non-param segment), ``/{id}/x`` -> ``x``,
    ``/a/b/c/d`` -> ``a``. Callers drop ``None``.
    """
    for segment in path.strip("/").split("/"):
        if segment and not (segment.startswith("{") and segment.endswith("}")):
            return segment
    return None


def _control_domains(routes: list[AdminRoute]) -> set[str]:
    """Domains carrying at least one mutating OPERATOR/ADMIN route (D1)."""
    domains: set[str] = set()
    for route in routes:
        if (
            route.method in _MUTATING_METHODS
            and route.permission_level in _CONTROL_LEVELS
        ):
            domain = _domain(route.path)
            if domain is not None:
                domains.add(domain)
    return domains


def _panels_block(raw: str) -> str:
    """The text inside the ``var PANELS = [...]`` array, or ``""`` if not found.

    Returning ``""`` (rather than raising) on a missing array funnels a drifted
    asset into the _PANEL_FLOOR parse-sanity assertion downstream, so the failure
    surfaces as a parser error, not an ``AttributeError``.
    """
    match = _PANELS_BLOCK_RE.search(raw)
    return match.group(1) if match else ""


def _panel_covered_domains(raw: str, *, floor: int = _PANEL_FLOOR) -> set[str]:
    """Domains referenced by any PANELS status/action path (D1).

    Asserts a parse-sanity floor (D5) so a drifted/garbled regex fails loud as a
    parser error rather than mis-diagnosing as a coverage failure. ``floor`` is
    overridable for unit tests that feed minimal synthetic fixtures.
    """
    block = _panels_block(raw)
    domains: set[str] = set()
    for path in _PANEL_PATH_RE.findall(block):
        domain = _domain(path)
        if domain is not None:
            domains.add(domain)
    assert len(domains) >= floor, (
        f"PANELS path parse returned {len(domains)} domain(s) (< floor {floor}); "
        f"console.html PANELS formatting likely drifted (or v1.0 panels were "
        f"removed). Fix the parser/asset, not the coverage assertion."
    )
    return domains


def _panels_pro_ids(raw: str, *, floor: int = _PANEL_FLOOR) -> set[str]:
    """Ids of PANELS entries marked ``pro: true`` (D4).

    Same parse-sanity floor (D5) as :func:`_panel_covered_domains`.
    """
    block = _panels_block(raw)
    ids = set(_PANEL_PRO_ID_RE.findall(block))
    assert len(ids) >= floor, (
        f"PANELS pro-id parse returned {len(ids)} id(s) (< floor {floor}); "
        f"console.html PANELS formatting likely drifted (or a v1.0 PRO panel was "
        f"removed). Fix the parser/asset, not the coverage assertion."
    )
    return ids


def _panels_by_id(
    raw: str, *, floor: int = _PANEL_FLOOR
) -> dict[str, tuple[str | None, list[str]]]:
    """Per-panel ``(status_path, [action_paths])`` keyed by panel id (D4/G44).

    The ``PANELS`` block is sliced on ``id:`` boundaries — a panel object always
    opens with ``id: "..."`` while nested action / ``bodyField`` objects carry only
    ``label:``/``path:``/``key:``. Within each slice the first ``status:`` literal
    is the panel's status read and every ``path:`` literal is an action route.
    Same parse-sanity floor (D5) as the other helpers: a drifted parse fails loud
    as a parser error rather than mis-diagnosing a coherence failure. ``floor`` is
    overridable for unit tests that feed minimal synthetic fixtures.
    """
    block = _panels_block(raw)
    starts = list(_PANEL_ID_RE.finditer(block))
    panels: dict[str, tuple[str | None, list[str]]] = {}
    for i, match in enumerate(starts):
        begin = match.end()
        end = starts[i + 1].start() if i + 1 < len(starts) else len(block)
        chunk = block[begin:end]
        status_match = _PANEL_STATUS_RE.search(chunk)
        status_path = status_match.group(1) if status_match else None
        action_paths = _PANEL_ACTION_RE.findall(chunk)
        panels[match.group(1)] = (status_path, action_paths)
    assert len(panels) >= floor, (
        f"PANELS per-panel parse returned {len(panels)} panel(s) (< floor "
        f"{floor}); console.html PANELS formatting likely drifted. Fix the "
        f"parser/asset, not the coherence assertion."
    )
    return panels


def _incoherent_panels(
    raw: str, *, floor: int = _PANEL_FLOOR
) -> list[tuple[str, str | None, list[str]]]:
    """Panels whose status domain differs from an action domain (D4/G44).

    Returns ``(panel_id, status_domain, [mismatched_action_domains])`` for every
    action-bearing panel NOT in ``_CROSS_DOMAIN_PANELS`` whose status domain does
    not equal every action domain. An empty list means every panel is coherent.
    Panels with no action are skipped (the status read has nothing to drift from).
    """
    incoherent: list[tuple[str, str | None, list[str]]] = []
    for panel_id, (status_path, action_paths) in _panels_by_id(
        raw, floor=floor
    ).items():
        if not action_paths or panel_id in _CROSS_DOMAIN_PANELS:
            continue
        status_domain = _domain(status_path) if status_path else None
        action_domains = {
            d for d in (_domain(p) for p in action_paths) if d is not None
        }
        mismatched = sorted(d for d in action_domains if d != status_domain)
        if mismatched:
            incoherent.append((panel_id, status_domain, mismatched))
    return incoherent


def _console_html() -> str:
    return (files("baldur.api.admin.console") / "console.html").read_text(
        encoding="utf-8"
    )


# =============================================================================
# G35 — top-level gate over the real route registry + console asset
# =============================================================================


class TestConsolePanelCoverage:
    """G1 (D1/D3) — every mutating OPERATOR/ADMIN control domain is paneled or exempt."""

    def test_every_control_domain_is_paneled_or_exempt(self):
        """A control feature-domain with no panel and no exemption is a violation.

        Computed over **live-registered** routes only (D3): in an OSS-only checkout
        an absent PRO route handler simply shrinks ``control_domains``, so the gate
        can never false-positive there.
        """
        routes = _create_admin_registry().all_routes()
        control_domains = _control_domains(routes)
        covered_domains = _panel_covered_domains(_console_html())

        # Domains that carry control routes but have no panel covering them.
        needs_panel = control_domains - covered_domains
        uncovered_unexempt = sorted(needs_panel - set(_PANEL_EXEMPT_DOMAINS))

        assert not uncovered_unexempt, (
            f"{len(uncovered_unexempt)} mutating OPERATOR/ADMIN control "
            f"feature-domain(s) have no admin-console panel and no exemption "
            f"(the #582/#583 blind-spot class): {uncovered_unexempt}.\n"
            f"For each domain X, either:\n"
            f"  (1) add a PANELS entry in "
            f"src/baldur/api/admin/console/console.html whose status or action "
            f"path falls under /X (the operator-facing surface), or\n"
            f"  (2) if X is an internal / v1.1 / report-driven surface, add X to "
            f"_PANEL_EXEMPT_DOMAINS in this file with a justification reason.\n"
            f"Rule: ARCHITECTURE.md"
            f"#g35-console-panel-coverage"
        )

        # Advisory only (D3): an exempt entry not currently needed — a PRO-only
        # domain absent in an OSS-only run, or a domain that became both paneled
        # AND exempt (the D6 transitional-entry case). Never a hard failure.
        unused_exempt = sorted(set(_PANEL_EXEMPT_DOMAINS) - needs_panel)
        if unused_exempt:
            print(
                f"[G35 advisory] {len(unused_exempt)} _PANEL_EXEMPT_DOMAINS "
                f"entr(y/ies) not currently needed (PRO route absent, or now "
                f"paneled): {unused_exempt}. Safe to leave; remove if permanently "
                f"obsolete."
            )

    def test_pro_id_server_map_consistency(self):
        """G2 (D4) — frontend ``pro:true`` ids == server-side ``_V1_PRO_PANEL_SLOTS``.

        Closes the one un-pinned edge of the 3-way triangle: a ``pro:true`` panel
        added to PANELS but not to the slot map renders but is never visible
        (``panelVisible`` requires ``window.__BALDUR_PANELS__[id]``), and the two
        lists can otherwise drift independently.
        """
        pro_ids = _panels_pro_ids(_console_html())
        assert pro_ids == set(_V1_PRO_PANEL_SLOTS), (
            "console.html PANELS pro:true ids drifted from the server-side "
            "_V1_PRO_PANEL_SLOTS keys (handler.py).\n"
            f"  only in PANELS (renders, never visible): "
            f"{sorted(pro_ids - set(_V1_PRO_PANEL_SLOTS))}\n"
            f"  only in slot map (gated, no panel): "
            f"{sorted(set(_V1_PRO_PANEL_SLOTS) - pro_ids)}\n"
            "Keep the frontend pro:true ids and the server slot map in lockstep."
        )

    def test_every_exempt_domain_has_a_nonempty_reason(self):
        """D2/D5 — every ``_PANEL_EXEMPT_DOMAINS`` entry justifies its exemption."""
        for domain, reason in _PANEL_EXEMPT_DOMAINS.items():
            assert isinstance(reason, str), (
                f"exempt domain {domain!r} reason must be a string"
            )
            assert reason.strip(), (
                f"exempt domain {domain!r} has an empty reason — every "
                f"_PANEL_EXEMPT_DOMAINS entry must state WHY it has no console "
                f"panel (v1.1-deferred, internal config surface, etc.)"
            )


# =============================================================================
# G44 — intra-panel status<->action domain coherence over the real asset
# =============================================================================


class TestConsolePanelStatusActionCoherence:
    """G44 (D4) — within each panel, status domain == every action domain."""

    def test_every_action_panel_status_and_actions_share_a_domain(self):
        """A panel reading one subsystem but mutating another is a violation.

        This is the bug 634 fixed: the Circuit Breakers panel displayed
        audit-backend CBs (``/audit/circuit-breakers``) while its Reset action
        targeted application-service CBs (``/control/reset/...``). G35 unions a
        panel's status + action domains, so it cannot see intra-panel drift.
        """
        incoherent = _incoherent_panels(_console_html())
        assert not incoherent, (
            f"{len(incoherent)} admin-console panel(s) read one subsystem in "
            f"their status path but mutate another in an action path "
            f"(status_domain != action_domain): "
            f"{[(pid, sd, ad) for pid, sd, ad in incoherent]}.\n"
            f"For each panel, either:\n"
            f"  (1) repoint the status or action path so both fall under the same "
            f"/domain (the panel's display and its controls must target the same "
            f"subsystem), or\n"
            f"  (2) if the panel intentionally spans subsystems, add its id to "
            f"_CROSS_DOMAIN_PANELS in this file with a justification.\n"
            f"Rule: ARCHITECTURE.md"
            f"#g44-console-panel-status-action-coherence"
        )

    def test_every_cross_domain_panel_has_a_nonempty_reason(self):
        """D4 — every ``_CROSS_DOMAIN_PANELS`` entry justifies the exception."""
        for panel_id, reason in _CROSS_DOMAIN_PANELS.items():
            assert isinstance(reason, str), (
                f"cross-domain panel {panel_id!r} reason must be a string"
            )
            assert reason.strip(), (
                f"cross-domain panel {panel_id!r} has an empty reason — every "
                f"_CROSS_DOMAIN_PANELS entry must state WHY its status and action "
                f"intentionally target different subsystems"
            )


# =============================================================================
# _control_domains — synthetic AdminRoute lists (no registry, no asset)
# =============================================================================


def _route(
    method: HttpMethod,
    path: str,
    level: PermissionLevel = PermissionLevel.VIEWER,
) -> AdminRoute:
    return AdminRoute(
        method=method, path=path, handler=lambda ctx: None, permission_level=level
    )


class TestControlDomainsHelper:
    """D1 — only mutating OPERATOR/ADMIN routes contribute a control domain."""

    def test_mutating_operator_route_is_a_control_domain(self):
        routes = [_route(HttpMethod.POST, "/foo/do", PermissionLevel.OPERATOR)]
        assert _control_domains(routes) == {"foo"}

    def test_mutating_admin_route_is_a_control_domain(self):
        routes = [_route(HttpMethod.DELETE, "/bar/x", PermissionLevel.ADMIN)]
        assert _control_domains(routes) == {"bar"}

    def test_patch_admin_route_is_a_control_domain(self):
        """PATCH is a mutating method (chaos config routes are PATCH/admin)."""
        routes = [_route(HttpMethod.PATCH, "/cfg/set", PermissionLevel.ADMIN)]
        assert _control_domains(routes) == {"cfg"}

    def test_read_only_route_is_not_a_control_domain(self):
        """A GET (read) route never contributes a control domain (out of scope)."""
        routes = [_route(HttpMethod.GET, "/foo/status", PermissionLevel.OPERATOR)]
        assert _control_domains(routes) == set()

    def test_authenticated_tier_mutating_route_is_excluded(self):
        """AUTHENTICATED-tier mutating routes (/gate/reset) are excluded (D1)."""
        routes = [_route(HttpMethod.POST, "/gate/reset", PermissionLevel.AUTHENTICATED)]
        assert _control_domains(routes) == set()

    def test_param_first_segment_is_skipped(self):
        """The domain is the first NON-param segment, not the literal first token."""
        routes = [_route(HttpMethod.POST, "/{id}/x", PermissionLevel.OPERATOR)]
        assert _control_domains(routes) == {"x"}

    def test_all_param_path_contributes_no_domain(self):
        """An all-param path has no domain and is silently dropped (not raised)."""
        routes = [_route(HttpMethod.POST, "/{a}/{b}", PermissionLevel.OPERATOR)]
        assert _control_domains(routes) == set()

    def test_multiple_routes_collapse_to_distinct_domains(self):
        routes = [
            _route(HttpMethod.POST, "/dlq/replay", PermissionLevel.OPERATOR),
            _route(HttpMethod.POST, "/dlq/purge", PermissionLevel.ADMIN),
            _route(HttpMethod.PUT, "/canary/promote", PermissionLevel.OPERATOR),
        ]
        assert _control_domains(routes) == {"dlq", "canary"}


class TestDomainHelper:
    """D1 — ``_domain`` is total over degenerate paths (never raises)."""

    @pytest.mark.parametrize(
        ("path", "expected"),
        [
            ("/control/reset/{service_name}", "control"),
            ("/audit/circuit-breakers", "audit"),
            ("/meta-watchdog/status", "meta-watchdog"),
            ("/", None),  # root: no segment
            ("/{id}/x", "x"),  # param-first: first non-param wins
            ("/{a}/{b}", None),  # all-param: no non-param segment
            ("/a/b/c/d", "a"),  # deep path: first segment
        ],
    )
    def test_domain_extraction(self, path, expected):
        assert _domain(path) == expected


# =============================================================================
# Parse helpers + parse-sanity floor — synthetic raw fixtures (D5)
# =============================================================================

# A minimal valid PANELS asset (2 panels) for parser anti-silent-pass checks.
# floor=0 is passed so the small fixture is not rejected by the live floor.
_MINIMAL_PANELS = """
  var PANELS = [
    { id: "alpha", title: "Alpha", pro: false, status: "/alpha/status" },
    { id: "beta", title: "Beta", pro: true, status: "/beta/status",
      actions: [
        { label: "Go", path: "/gamma/go", method: "POST", risk: "admin" }
      ] }
  ];
"""


class TestPanelParsing:
    """D5 — the regex helpers extract the right sets from a controlled fixture."""

    def test_covered_domains_extracted_from_status_and_action_paths(self):
        # alpha (status), beta (status), gamma (action path) — three domains.
        assert _panel_covered_domains(_MINIMAL_PANELS, floor=0) == {
            "alpha",
            "beta",
            "gamma",
        }

    def test_pro_ids_extracted_only_for_pro_true_panels(self):
        # alpha is pro:false; only beta is pro:true.
        assert _panels_pro_ids(_MINIMAL_PANELS, floor=0) == {"beta"}

    def test_pro_id_parse_would_catch_drift(self):
        """A fixture whose pro ids differ from the slot map is reported as drift.

        Proves G2 (test_pro_id_server_map_consistency) is not vacuous: the helper
        returns the *actual* parsed ids, so an induced mismatch fails the equality.
        """
        parsed = _panels_pro_ids(_MINIMAL_PANELS, floor=0)
        assert parsed != set(_V1_PRO_PANEL_SLOTS)


class TestParseSanityFloor:
    """D5 — a drifted/empty parse fails AS A PARSER ERROR, not a coverage failure."""

    def test_no_panels_array_trips_covered_domains_floor(self):
        with pytest.raises(AssertionError, match="PANELS path parse returned 0"):
            _panel_covered_domains("<html>no panels here</html>")

    def test_no_panels_array_trips_pro_ids_floor(self):
        with pytest.raises(AssertionError, match="PANELS pro-id parse returned 0"):
            _panels_pro_ids("<html>no panels here</html>")

    def test_real_asset_clears_the_floor(self):
        """Sanity: the real asset parses well above the live floor for both helpers."""
        raw = _console_html()
        assert len(_panel_covered_domains(raw)) >= _PANEL_FLOOR
        assert len(_panels_pro_ids(raw)) >= _PANEL_FLOOR


# =============================================================================
# G44 per-panel parse + coherence — synthetic raw fixtures (D4)
# =============================================================================

# Coherent: the action-bearing panel's status and action share the /alpha domain.
_COHERENT_PANELS = """
  var PANELS = [
    { id: "alpha", title: "Alpha", pro: false, status: "/alpha/status",
      actions: [
        { label: "Go", path: "/alpha/do", method: "POST", risk: "admin" }
      ] }
  ];
"""

# Incoherent (the 634 bug shape): status reads /alpha but the action mutates /beta.
_INCOHERENT_PANELS = """
  var PANELS = [
    { id: "alpha", title: "Alpha", pro: false, status: "/alpha/status",
      actions: [
        { label: "Go", path: "/beta/do", method: "POST", risk: "admin" }
      ] }
  ];
"""


class TestPanelByIdParsing:
    """D4 — ``_panels_by_id`` slices PANELS per panel on ``id:`` boundaries."""

    def test_status_and_actions_grouped_under_their_panel(self):
        # alpha has only a status; beta has a status + one action path.
        assert _panels_by_id(_MINIMAL_PANELS, floor=0) == {
            "alpha": ("/alpha/status", []),
            "beta": ("/beta/status", ["/gamma/go"]),
        }

    def test_no_panels_array_trips_floor(self):
        with pytest.raises(AssertionError, match="per-panel parse returned 0"):
            _panels_by_id("<html>no panels here</html>")

    def test_real_asset_clears_the_floor(self):
        assert len(_panels_by_id(_console_html())) >= _PANEL_FLOOR


class TestPanelCoherenceFixtures:
    """D4 — ``_incoherent_panels`` detects (and only detects) intra-panel drift."""

    def test_coherent_panel_passes(self):
        assert _incoherent_panels(_COHERENT_PANELS, floor=0) == []

    def test_induced_cross_domain_panel_fails(self):
        """Non-vacuity (SC #2): a status/action subsystem mismatch is flagged.

        Mirrors ``test_pro_id_parse_would_catch_drift`` — proves G44 is not
        vacuous: the helper returns the *actual* mismatch, so the gate assertion
        would fire on an induced cross-domain panel.
        """
        assert _incoherent_panels(_INCOHERENT_PANELS, floor=0) == [
            ("alpha", "alpha", ["beta"])
        ]

    def test_allowlisted_cross_domain_panel_passes(self, monkeypatch):
        """An allowlisted incoherent panel is suppressed (mirrors the exempt dict)."""
        monkeypatch.setitem(
            _CROSS_DOMAIN_PANELS, "alpha", "intentional cross-domain for this test"
        )
        assert _incoherent_panels(_INCOHERENT_PANELS, floor=0) == []
