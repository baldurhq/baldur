"""Extraction-layer tests for the operator-surface inventory helpers (748).

``_operator_surface`` is what makes G82's unit list derived rather than
authored, and that is exactly why it needs its own tests: the gate compares the
extracted set against a committed baseline, so an extractor that starts
returning a *smaller* set still matches a baseline regenerated from the same
broken extractor. The failure is silent by construction — nothing inside the
gate can see it.

The live tree exercises these helpers only on whatever the shipped console
asset happens to contain today, which is incidental coverage, not a pinned
contract. Every test below injects a synthetic asset (or synthetic routes)
instead, so each parsing rule fails for its own reason.

Two documented extraction boundaries are pinned by *absence* assertions rather
than left implicit: a read rooted in an expression, and a read inside a helper
the renderer delegates to, are both outside the ratchet and owned by the
periodic audit. Asserting they stay out keeps the limitation a decision instead
of letting it decay into an undetected regression.
"""

from __future__ import annotations

import functools

import pytest

from baldur.api.admin.registry import AdminRoute
from baldur.interfaces.web_framework import HttpMethod, PermissionLevel
from tests.architecture._operator_surface import (
    BODY_FIELD,
    PANEL,
    RENDERER_KEY,
    ROUTE_DOMAIN,
    T1,
    T2,
    T3,
    SurfaceUnit,
    _function_body,
    _reads_rooted_at,
    body_field_units,
    handler_qualname,
    level1_units,
    panel_domain_tiers,
    panels,
    renderer_read_keys,
    route_domain_units,
    unit_id,
)

# ---------------------------------------------------------------------------
# Synthetic console asset
#
# ``{health_pro}`` is the only variable: the tiering tests flip it to prove the
# whole dependent surface re-tiers without any list being edited.
# ---------------------------------------------------------------------------

_CONSOLE_TEMPLATE = """
var PANELS = [
  {{
    id: "health", title: "Health", pro: {health_pro},
    status: "/health/status",
    bodyRender: "health",
    actions: [
      {{ label: "Reset", path: "/health/reset", method: "POST",
        bodyFields: [{{ key: "scope", label: "Scope" }}, {{ key: "reason", label: "Why" }}] }},
      {{ label: "Ping", path: "/health/ping", method: "POST",
        bodyField: {{ key: "target", label: "Target" }} }}
    ]
  }},
  {{
    id: "dlq", title: "DLQ", pro: false,
    status: "/dlq/stats",
    actions: [
      {{ label: "Purge", path: "/dlq/purge", method: "POST", pro: true }}
    ]
  }},
  {{
    id: "about", title: "About", pro: false
  }},
  {{
    id: "canary", title: "Canary", pro: true,
    status: "/canary/status",
    actions: [
      {{ label: "Promote", path: "/canary/promote", method: "POST",
        bodyField: {{ key: "rollout", label: "Rollout" }} }}
    ]
  }}
];

var BODY_RENDERERS = {{ health: renderHealth }};

function renderHealth(el, data) {{
  var ov = (data && data.overview) || {{}};
  el.innerHTML = ov.uptime + data.state;
}}
"""


def _console(*, health_pro: bool = False) -> str:
    return _CONSOLE_TEMPLATE.format(health_pro="true" if health_pro else "false")


def _plain_handler(request: object) -> None:
    """Stand-in admin handler — only its qualname is under test."""


class _CallableHandler:
    """A callable object, which carries no ``__qualname__`` of its own."""

    def __call__(self, request: object) -> None: ...


def _route(
    path: str,
    *,
    method: HttpMethod = HttpMethod.POST,
    level: PermissionLevel = PermissionLevel.OPERATOR,
    handler: object = _plain_handler,
) -> AdminRoute:
    return AdminRoute(method=method, path=path, handler=handler, permission_level=level)


class TestConsolePanelParsing:
    """``panels()`` slices the PANELS block on ``id:`` boundaries."""

    def test_panels_parses_every_entry_in_declaration_order(self) -> None:
        parsed = panels(_console())

        assert [panel.id for panel in parsed] == ["health", "dlq", "about", "canary"]

    def test_panels_reads_status_body_render_and_action_paths(self) -> None:
        health = panels(_console())[0]

        assert health.status_path == "/health/status"
        assert health.body_render == "health"
        assert health.action_paths == ("/health/reset", "/health/ping")
        assert health.paths == ("/health/status", "/health/reset", "/health/ping")

    def test_panels_with_no_status_actions_or_renderer_parses_as_empty(self) -> None:
        # Given a panel declaring nothing but its prologue
        about = panels(_console())[2]

        # Then every optional slot is absent rather than guessed
        assert about.status_path is None
        assert about.body_render is None
        assert about.action_paths == ()
        assert about.paths == ()

    def test_panels_ignores_per_action_pro_flag_when_reading_panel_visibility(
        self,
    ) -> None:
        # Given a `pro: false` panel whose single action carries `pro: true`
        dlq = panels(_console())[1]

        # Then the panel's own prologue decides visibility, not the action's
        assert dlq.pro is False
        assert panels(_console())[3].pro is True

    def test_panels_reads_panel_pro_flag_from_the_prologue(self) -> None:
        assert panels(_console(health_pro=False))[0].pro is False
        assert panels(_console(health_pro=True))[0].pro is True

    def test_panels_without_a_panels_block_returns_empty(self) -> None:
        assert panels("var OTHER = [];") == []


class TestBodyFieldExtraction:
    """``body_field_units()`` attributes each input key to its own action."""

    def test_body_field_units_captures_first_and_last_action_in_a_panel(self) -> None:
        # Given a panel whose first action carries two keys and last carries one
        keys = {unit.key for unit in body_field_units(_console())}

        # Then no action slice swallows or leaks into its neighbour
        assert "/health/reset::scope" in keys
        assert "/health/reset::reason" in keys
        assert "/health/ping::target" in keys

    def test_body_field_units_reads_both_singular_and_plural_field_forms(self) -> None:
        keys = {unit.key for unit in body_field_units(_console())}

        # bodyFields: [...] on /health/reset, bodyField: {...} on /canary/promote
        assert {"/health/reset::scope", "/health/reset::reason"} <= keys
        assert "/canary/promote::rollout" in keys

    def test_body_field_units_inherits_the_panel_tier(self) -> None:
        by_key = {unit.key: unit for unit in body_field_units(_console())}

        assert by_key["/health/reset::scope"].tier == T1
        assert by_key["/canary/promote::rollout"].tier == T2

    def test_body_field_units_dedups_a_key_shared_by_two_panels(self) -> None:
        # Given two panels exposing the same action path with the same key
        raw = """
        var PANELS = [
          { id: "a", title: "A", pro: false,
            actions: [{ label: "Purge", path: "/dlq/purge", method: "POST",
              bodyField: { key: "queue" } }] },
          { id: "b", title: "B", pro: false,
            actions: [{ label: "Purge", path: "/dlq/purge", method: "POST",
              bodyField: { key: "queue" } }] }
        ];
        """

        # When extracting
        units = body_field_units(raw)

        # Then one unit is produced, not one per panel
        assert [unit.key for unit in units] == ["/dlq/purge::queue"]

    def test_body_field_units_ignores_keys_outside_any_action_slice(self) -> None:
        # Given a `key:` appearing before the panel's first action path
        raw = """
        var PANELS = [
          { id: "a", title: "A", pro: false, filter: { key: "not_an_input" },
            actions: [{ label: "Go", path: "/a/go", method: "POST",
              bodyField: { key: "real_input" } }] }
        ];
        """

        # Then only the key inside an action slice becomes an input unit
        assert [unit.key for unit in body_field_units(raw)] == ["/a/go::real_input"]


class TestRendererReadRoots:
    """``_reads_rooted_at()`` grows roots by two idioms, and only those two."""

    def test_reads_rooted_at_captures_direct_payload_properties(self) -> None:
        body = "el.innerHTML = data.state + data.count;"

        assert _reads_rooted_at(body, "data") == {"state", "count"}

    def test_reads_rooted_at_follows_an_object_alias_defaulted_to_empty_object(
        self,
    ) -> None:
        body = """
        var ov = (data && data.overview) || {};
        el.innerHTML = ov.uptime + ov.errors;
        """

        assert _reads_rooted_at(body, "data") == {"overview", "uptime", "errors"}

    def test_reads_rooted_at_follows_array_elements_through_foreach(self) -> None:
        # Given an array read defaulted to [] and iterated with a function arg
        body = """
        var rows = data.rollouts || [];
        rows.forEach(function (r) { el.innerHTML += r.state + r.percent; });
        """

        # When resolving reads
        keys = _reads_rooted_at(body, "data")

        # Then the element name becomes a root and its properties are payload keys
        assert keys == {"rollouts", "state", "percent"}

    def test_reads_rooted_at_follows_a_call_free_member_chain_alias(self) -> None:
        # Given an alias whose right-hand side only walks members
        body = """
        var det = data.details && data.details.workers;
        el.innerHTML = det.alive;
        """

        # When resolving reads
        keys = _reads_rooted_at(body, "data")

        # Then the alias is a root. Only the first segment of the chain is read
        # off the payload root, so the intermediate `workers` stays uncaptured —
        # the same one-level rule every root follows.
        assert keys == {"details", "alive"}

    def test_reads_rooted_at_rejects_an_alias_produced_by_a_call(self) -> None:
        # Given a coerced scalar alias — the JS-builtin harvesting regression
        body = """
        var label = String(data.state || "?");
        el.textContent = label.toUpperCase() + label.length;
        """

        # When resolving reads
        keys = _reads_rooted_at(body, "data")

        # Then the payload read survives and JavaScript's own members do not
        assert keys == {"state"}
        assert "toUpperCase" not in keys
        assert "length" not in keys

    def test_reads_rooted_at_does_not_capture_a_read_rooted_in_an_expression(
        self,
    ) -> None:
        # Given the documented out-of-scope shape `(workers[w] || {}).status`
        body = """
        var workers = data.workers || {};
        var st = (workers[w] || {}).status;
        """

        # Then the boundary holds: the expression-rooted read is not harvested
        assert _reads_rooted_at(body, "data") == {"workers"}

    def test_reads_rooted_at_does_not_capture_a_read_inside_a_delegated_helper(
        self,
    ) -> None:
        # Given a renderer that hands the payload slice to a helper
        body = "renderRows(el, data.rows);"

        # Then only the read the renderer itself performs is captured
        assert _reads_rooted_at(body, "data") == {"rows"}

    def test_renderer_read_keys_maps_each_dispatch_key_to_its_payload_reads(
        self,
    ) -> None:
        assert renderer_read_keys(_console()) == {
            "health": {"overview", "uptime", "state"}
        }


class TestFunctionBodyScan:
    """``_function_body()`` balances braces without being fooled by strings."""

    def test_function_body_ignores_braces_inside_a_string_literal(self) -> None:
        raw = 'function renderX(el, data) { var t = "}{"; el.innerHTML = t + data.a; }'

        body = _function_body(raw, "renderX")

        assert body == ' var t = "}{"; el.innerHTML = t + data.a; '

    def test_function_body_ignores_an_escaped_quote_inside_a_string_literal(
        self,
    ) -> None:
        # Given a string whose escaped quote must not close the quoted span early
        raw = r"""function renderY(el, data) { var t = "a\"}"; return data.b; }"""

        body = _function_body(raw, "renderY")

        assert body == r""" var t = "a\"}"; return data.b; """

    def test_function_body_for_a_missing_function_returns_empty(self) -> None:
        assert _function_body("function other(a) { return 1; }", "renderZ") == ""

    def test_function_body_for_an_unterminated_body_returns_empty(self) -> None:
        assert (
            _function_body("function renderZ(el, data) { var t = 1;", "renderZ") == ""
        )


class TestDerivedTiering:
    """Tier falls out of panel visibility — nothing here is assigned by hand."""

    def test_panel_domain_tiers_assigns_oss_and_pro_panels_their_own_tier(self) -> None:
        tiers = panel_domain_tiers(_console())

        assert tiers["health"] == T1
        assert tiers["dlq"] == T1
        assert tiers["canary"] == T2

    def test_panel_domain_tiers_keeps_t1_when_a_pro_panel_also_touches_a_domain(
        self,
    ) -> None:
        # Given one OSS-visible and one PRO panel reaching the same domain
        raw = """
        var PANELS = [
          { id: "pro_view", title: "Pro", pro: true, status: "/shared/status" },
          { id: "oss_view", title: "Oss", pro: false, status: "/shared/status" }
        ];
        """

        # Then the more-exposed tier wins regardless of declaration order
        assert panel_domain_tiers(raw)["shared"] == T1

    def test_route_domain_units_tiers_a_domain_no_panel_touches_as_t3(self) -> None:
        # Given a mutating control domain with no panel behind it
        routes = [_route("/health/reset"), _route("/secrets/rotate")]

        # When deriving control-domain units
        by_key = {unit.key: unit for unit in route_domain_units(routes, _console())}

        # Then it lands outside the console tiers entirely
        assert by_key["health"].tier == T1
        assert by_key["secrets"].tier == T3

    def test_route_domain_units_excludes_read_only_and_viewer_routes(self) -> None:
        routes = [
            _route("/health/status", method=HttpMethod.GET),
            _route("/audit/export", level=PermissionLevel.VIEWER),
            _route("/health/reset"),
        ]

        assert [unit.key for unit in route_domain_units(routes, _console())] == [
            "health"
        ]

    def test_level1_units_re_tiers_the_whole_dependent_surface_on_a_pro_flip(
        self,
    ) -> None:
        # Given the identical asset differing only in the health panel's pro flag
        routes = [_route("/health/reset")]
        before = {u.id: u.tier for u in level1_units(_console(), routes)}
        after = {u.id: u.tier for u in level1_units(_console(health_pro=True), routes)}

        # When comparing, the unit set is unchanged
        assert before.keys() == after.keys()

        # Then every unit hanging off that panel moved T1 -> T2, and nothing else
        moved = {uid for uid in before if before[uid] != after[uid]}
        assert moved == {
            unit_id(PANEL, "health"),
            unit_id(ROUTE_DOMAIN, "health"),
            unit_id(BODY_FIELD, "/health/reset::scope"),
            unit_id(BODY_FIELD, "/health/reset::reason"),
            unit_id(BODY_FIELD, "/health/ping::target"),
            unit_id(RENDERER_KEY, "health::overview"),
            unit_id(RENDERER_KEY, "health::uptime"),
            unit_id(RENDERER_KEY, "health::state"),
        }
        assert {before[uid] for uid in moved} == {T1}
        assert {after[uid] for uid in moved} == {T2}


class TestHandlerAttribution:
    """``handler_qualname()`` names the function an operator's click reaches."""

    def test_handler_qualname_uses_the_module_qualified_name(self) -> None:
        assert handler_qualname(_route("/x")) == f"{__name__}._plain_handler"

    def test_handler_qualname_unwraps_nested_partials_to_the_real_function(
        self,
    ) -> None:
        # Given the per-section handler shape: a partial wrapping a partial
        nested = functools.partial(functools.partial(_plain_handler, 1), 2)

        # Then attribution reaches the underlying function, not functools
        assert handler_qualname(_route("/x", handler=nested)) == (
            f"{__name__}._plain_handler"
        )

    def test_handler_qualname_for_a_callable_object_falls_back_to_a_marker(
        self,
    ) -> None:
        # A callable instance carries neither __qualname__ nor __name__
        result = handler_qualname(_route("/x", handler=_CallableHandler()))

        assert result == f"{__name__}.?"


class TestSurfaceUnitContract:
    """The unit id is the baseline row key, so its shape is frozen."""

    def test_unit_id_joins_kind_and_key_with_a_single_colon(self) -> None:
        assert unit_id(PANEL, "health") == "panel:health"
        assert unit_id(ROUTE_DOMAIN, "dlq") == "route-domain:dlq"
        assert unit_id(BODY_FIELD, "/a/go::flag") == "body-field:/a/go::flag"
        assert unit_id(RENDERER_KEY, "health::uptime") == "renderer-key:health::uptime"

    def test_surface_unit_id_matches_unit_id_of_its_kind_and_key(self) -> None:
        unit = SurfaceUnit(kind=PANEL, key="health", source="asset", tier=T1)

        assert unit.id == unit_id(PANEL, "health")

    def test_surface_unit_is_frozen(self) -> None:
        unit = SurfaceUnit(kind=PANEL, key="health", source="asset", tier=T1)

        with pytest.raises(AttributeError):
            unit.tier = T2  # type: ignore[misc]

    def test_unit_ids_stay_unique_across_the_derived_inventory(self) -> None:
        units = level1_units(_console(), [_route("/health/reset")])

        assert len({unit.id for unit in units}) == len(units)
