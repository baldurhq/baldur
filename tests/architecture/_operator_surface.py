"""Derived inventory of the operator-visible surface.

Every value an operator reads and every control an operator clicks is supposed
to have something real behind it — a producer that computes the number, a
consumer that acts on the button. Both have shipped without one before, and
both were found by accident rather than by looking. This module is the "by
looking" half: it extracts, from code artifacts alone, the set of surface units
that a verdict has to exist for.

The extraction is mechanical on purpose. A hand-written unit list regenerates
its own omissions every time it is revised, so nothing here is authored — the
route domains come from instantiating the admin registry, the panels and their
body-field keys come from parsing the shipped console asset, and the bespoke
renderers' payload reads come from the same asset. What a human writes is the
verdict per unit, never the unit list.

Four unit kinds make up level 1 (the affordance level):

``panel``
    A console panel id. Adding one adds a whole read surface.
``route-domain``
    The first non-parameter path segment of a mutating OPERATOR/ADMIN admin
    route — the granularity at which control surfaces are paneled.
``body-field``
    A ``bodyField``/``bodyFields`` key an operator types into, keyed by the
    action path that carries it. This is the input half of a control: a key the
    console sends that no handler reads is a dead input.
``renderer-key``
    A payload property one of the six bespoke ``BODY_RENDERERS`` reads. The
    generic panel renderer walks whatever keys the payload carries, so its
    slots are the producer's dict; a bespoke renderer names its keys, and a
    named key with no producer renders as a permanent blank.

Level 2 (individual leaf values inside a payload) is deliberately NOT derived
here: spread assembly such as ``{**stats}`` makes the leaf population a runtime
property, so leaf truth is carried by the fabrication detector, the periodic
claim-wiring audit, and the live-smoke pass instead.

Extraction boundaries worth knowing before trusting a count:

- Every route registrar swallows its own import failure and returns, which
  drops a whole domain silently. Callers therefore ratchet the raw route count
  against the committed baseline rather than treating "domain absent" as
  "surface absent".
- Renderer reads are resolved through the payload parameter and one fixpoint of
  local aliases. A read rooted in an expression rather than a name — the
  ``(workers[w] || {}).status`` shape — is not captured, and stays owned by the
  periodic audit.
- Operator input keys are read from the ``PANELS`` declaration only. The asset
  declares a handful of others outside it — actions a drilldown builds at click
  time (``openActionModal({path: …, bodyField: {key: "notes"}})``) and the
  canary lifecycle table, which names a ``suffix`` rather than a ``path``.
  Attributing those needs a unit key and a tier rule for a second declaration
  site, which is a design decision rather than a wider regex, so they are
  tracked as an open item and NOT silently counted. Read the tier close as
  "every unit this extractor derives", not "every key on the screen".

This is a non-test helper (no ``test_`` prefix, so pytest does not collect it).
"""

from __future__ import annotations

import functools
import re
from dataclasses import dataclass
from importlib.resources import files

from baldur.api.admin.registry import AdminRoute, _create_admin_registry
from baldur.interfaces.web_framework import HttpMethod, PermissionLevel

__all__ = [
    "BODY_FIELD",
    "PANEL",
    "RENDERER_KEY",
    "ROUTE_DOMAIN",
    "SurfaceUnit",
    "T1",
    "T2",
    "T3",
    "TIERS",
    "admin_routes",
    "body_field_units",
    "console_html",
    "handler_qualname",
    "level1_units",
    "mutating_control_domains",
    "panel_units",
    "panel_domain_tiers",
    "panels",
    "renderer_key_units",
    "renderer_read_keys",
    "route_domain_units",
    "unit_id",
]

# Unit kinds. A unit id is ``"<kind>:<key>"`` — stable under reformatting and
# never keyed on a line number, so a verdict row survives an unrelated edit.
PANEL = "panel"
ROUTE_DOMAIN = "route-domain"
BODY_FIELD = "body-field"
RENDERER_KEY = "renderer-key"

# A control route: a mutating method at operator grade. Same predicate the
# console panel-coverage gate uses, restated here rather than imported so the
# inventory does not acquire a dependency on a gate module.
_MUTATING_METHODS = frozenset(
    {HttpMethod.POST, HttpMethod.PUT, HttpMethod.PATCH, HttpMethod.DELETE}
)
_CONTROL_LEVELS = frozenset({PermissionLevel.OPERATOR, PermissionLevel.ADMIN})

_PANELS_BLOCK_RE = re.compile(r"var PANELS = \[(.*?)\];", re.DOTALL)
_PANEL_ID_RE = re.compile(r'id:\s*"([^"]+)"')
_ACTION_PATH_RE = re.compile(r'path:\s*"(/[^"]*)"')
_PANEL_PROLOGUE_RE = re.compile(r'\s*,\s*title:\s*"[^"]*"\s*,\s*pro:\s*(true|false)')
_PANEL_STATUS_RE = re.compile(r'status:\s*"(/[^"]*)"')
_PANEL_BODY_RENDER_RE = re.compile(r'bodyRender:\s*"([^"]+)"')
# ``key:`` appears in the PANELS block only inside bodyField/bodyFields objects
# (actions carry ``label``/``path``/``method``, option objects carry
# ``value``/``label``), so a flat scan inside an action slice is unambiguous.
_BODY_FIELD_KEY_RE = re.compile(r'key:\s*"([^"]+)"')
_BODY_RENDERERS_RE = re.compile(r"var BODY_RENDERERS = \{(.*?)\};", re.DOTALL)
_RENDERER_ENTRY_RE = re.compile(r"(\w+)\s*:\s*(\w+)")


# Audit tiers, derived from the console asset rather than assigned by hand: T1
# is what an OSS operator sees (a ``pro: false`` panel and everything hanging
# off it) and is the launch gate; T2 is the PRO console surface; T3 is a control
# domain no panel reaches at all — REST/CLI-only territory.
T1 = "T1"
T2 = "T2"
T3 = "T3"
TIERS = (T1, T2, T3)


@dataclass(frozen=True)
class SurfaceUnit:
    """One level-1 operator-surface unit, where it came from, and its tier."""

    kind: str
    key: str
    source: str
    tier: str

    @property
    def id(self) -> str:
        return unit_id(self.kind, self.key)


def unit_id(kind: str, key: str) -> str:
    return f"{kind}:{key}"


# =============================================================================
# Admin route registry
# =============================================================================


def admin_routes() -> list[AdminRoute]:
    """Every route the admin registry wires, by instantiation.

    Instantiated rather than grepped: several domains append their routes in a
    loop, so a source scan undercounts them.
    """
    return _create_admin_registry().all_routes()


def handler_qualname(route: AdminRoute) -> str:
    """The handler's module-qualified name, unwrapping ``functools.partial``.

    The runtime-config domain registers its per-section GET/PUT handlers as
    partials, so the raw attribute is ``functools.partial`` for those and
    attribution collapses onto one meaningless name without the unwrap.
    """
    handler = route.handler
    while isinstance(handler, functools.partial):
        handler = handler.func
    module = getattr(handler, "__module__", "?")
    name = getattr(handler, "__qualname__", getattr(handler, "__name__", "?"))
    return f"{module}.{name}"


def path_domain(path: str) -> str | None:
    """First non-parameter path segment, or ``None`` when the path has none."""
    for segment in path.strip("/").split("/"):
        if segment and not (segment.startswith("{") and segment.endswith("}")):
            return segment
    return None


def mutating_control_domains(routes: list[AdminRoute]) -> dict[str, list[AdminRoute]]:
    """Control domains mapped to the mutating OPERATOR/ADMIN routes under them."""
    domains: dict[str, list[AdminRoute]] = {}
    for route in routes:
        if (
            route.method not in _MUTATING_METHODS
            or route.permission_level not in _CONTROL_LEVELS
        ):
            continue
        domain = path_domain(route.path)
        if domain is not None:
            domains.setdefault(domain, []).append(route)
    return domains


def panel_domain_tiers(raw: str) -> dict[str, str]:
    """Route domain -> tier, from which panels reach it.

    A domain touched by an OSS-visible panel is T1, a domain touched only by a
    PRO panel is T2, and a domain no panel touches at all is T3 — the whole
    assignment falls out of the asset, so flipping a panel's ``pro`` flag
    re-tiers its surface without anyone editing a list.
    """
    tiers: dict[str, str] = {}
    for panel in panels(raw):
        tier = T2 if panel.pro else T1
        for path in panel.paths:
            domain = path_domain(path)
            if domain is None:
                continue
            if tiers.get(domain) != T1:
                tiers[domain] = tier
    return tiers


def route_domain_units(
    routes: list[AdminRoute] | None = None, raw: str | None = None
) -> list[SurfaceUnit]:
    """One unit per mutating control domain, sourced by its first route."""
    routes = admin_routes() if routes is None else routes
    tiers = panel_domain_tiers(console_html() if raw is None else raw)
    units = []
    for domain, domain_routes in sorted(mutating_control_domains(routes).items()):
        first = sorted(domain_routes, key=lambda r: (r.path, r.method.value))[0]
        units.append(
            SurfaceUnit(
                tier=tiers.get(domain, T3),
                kind=ROUTE_DOMAIN,
                key=domain,
                source=f"{first.method.value} {first.path} -> {handler_qualname(first)}",
            )
        )
    return units


# =============================================================================
# Console asset
# =============================================================================


def console_html() -> str:
    return (files("baldur.api.admin.console") / "console.html").read_text(
        encoding="utf-8"
    )


def _panels_block(raw: str) -> str:
    match = _PANELS_BLOCK_RE.search(raw)
    return match.group(1) if match else ""


@dataclass(frozen=True)
class ConsolePanel:
    """A parsed ``PANELS`` entry."""

    id: str
    pro: bool
    status_path: str | None
    body_render: str | None
    action_paths: tuple[str, ...]

    @property
    def paths(self) -> tuple[str, ...]:
        head = (self.status_path,) if self.status_path else ()
        return (*head, *self.action_paths)


def panels(raw: str) -> list[ConsolePanel]:
    """Parse ``PANELS`` into per-panel records.

    A panel object opens with ``id: "..."`` and nested action/bodyField objects
    never carry ``id:``, so the block slices cleanly on ``id:`` boundaries. The
    panel's own ``pro:`` flag is read from the fixed ``id``/``title``/``pro``
    prologue, which keeps a per-action ``pro: true`` further down the object
    from being mistaken for the panel's.
    """
    block = _panels_block(raw)
    starts = list(_PANEL_ID_RE.finditer(block))
    parsed = []
    for i, match in enumerate(starts):
        end = starts[i + 1].start() if i + 1 < len(starts) else len(block)
        chunk = block[match.end() : end]
        prologue = _PANEL_PROLOGUE_RE.match(chunk)
        status = _PANEL_STATUS_RE.search(chunk)
        renderer = _PANEL_BODY_RENDER_RE.search(chunk)
        parsed.append(
            ConsolePanel(
                id=match.group(1),
                pro=bool(prologue) and prologue.group(1) == "true",
                status_path=status.group(1) if status else None,
                body_render=renderer.group(1) if renderer else None,
                action_paths=tuple(_ACTION_PATH_RE.findall(chunk)),
            )
        )
    return parsed


def panel_units(raw: str) -> list[SurfaceUnit]:
    return [
        SurfaceUnit(
            kind=PANEL,
            key=panel.id,
            source="console.html PANELS",
            tier=T2 if panel.pro else T1,
        )
        for panel in panels(raw)
    ]


def body_field_units(raw: str) -> list[SurfaceUnit]:
    """One unit per ``bodyField``/``bodyFields`` key, keyed by its action path.

    Within a panel slice each action begins at its ``path:`` literal, so the
    span from one action path to the next holds exactly that action's body
    fields.

    Scoped to the ``PANELS`` declaration — see the module docstring's extraction
    boundaries for the keys declared elsewhere in the asset and why they are an
    open item rather than a wider regex.
    """
    block = _panels_block(raw)
    starts = list(_PANEL_ID_RE.finditer(block))
    parsed = {panel.id: panel for panel in panels(raw)}
    units: list[SurfaceUnit] = []
    seen: set[str] = set()
    for i, match in enumerate(starts):
        end = starts[i + 1].start() if i + 1 < len(starts) else len(block)
        chunk = block[match.end() : end]
        panel_id = match.group(1)
        tier = T2 if parsed[panel_id].pro else T1
        actions = list(_ACTION_PATH_RE.finditer(chunk))
        for j, action in enumerate(actions):
            action_end = actions[j + 1].start() if j + 1 < len(actions) else len(chunk)
            slice_ = chunk[action.end() : action_end]
            for key in _BODY_FIELD_KEY_RE.findall(slice_):
                unit = SurfaceUnit(
                    kind=BODY_FIELD,
                    key=f"{action.group(1)}::{key}",
                    source=f"console.html PANELS {panel_id} action",
                    tier=tier,
                )
                if unit.id not in seen:
                    seen.add(unit.id)
                    units.append(unit)
    return units


# =============================================================================
# Bespoke panel-body renderers
# =============================================================================


def _function_body(raw: str, name: str) -> str:
    """The brace-balanced body of ``function <name>(...) { ... }``, or ``""``.

    Quoted spans are skipped so a brace inside a string literal cannot
    unbalance the scan.
    """
    opener = re.search(r"function\s+" + re.escape(name) + r"\s*\(([^)]*)\)\s*\{", raw)
    if opener is None:
        return ""
    depth = 0
    i = opener.end() - 1
    quote: str | None = None
    while i < len(raw):
        ch = raw[i]
        if quote is not None:
            if ch == "\\":
                i += 2
                continue
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return raw[opener.end() : i]
        i += 1
    return ""


def _payload_param(raw: str, name: str) -> str | None:
    """The renderer's payload argument — the second formal parameter."""
    opener = re.search(r"function\s+" + re.escape(name) + r"\s*\(([^)]*)\)", raw)
    if opener is None:
        return None
    params = [p.strip() for p in opener.group(1).split(",") if p.strip()]
    return params[1] if len(params) > 1 else None


_DECL_RE = re.compile(r"\b(?:var|let|const)\s+(\w+)\s*=\s*([^;]*);")
_CALL_RE = re.compile(r"\w\s*\(")
_ELEMENT_RE_TEMPLATE = (
    r"\b(?:{roots})\s*\.\s*(?:forEach|map|filter|some|every)\s*"
    r"\(\s*function\s*\(\s*(\w+)"
)


def _reads_rooted_at(body: str, payload_param: str) -> set[str]:
    """Property names the body reads out of its payload argument.

    Roots grow by two idioms and only those two, because both preserve
    "this name still holds payload":

    - an object alias — ``var ov = (data && data.overview) || {}``, or a
      call-free member/index chain such as ``var w = det.details && det.details.workers``;
    - an array element — ``rollouts.forEach(function (r) { … r.state … })``,
      where ``rollouts`` itself came from a payload read defaulted to ``[]``.

    An alias whose right-hand side calls something (``String(data.x || "?")``)
    is deliberately NOT a root: it holds a coerced scalar, and treating it as
    payload would harvest JavaScript's own members (``toUpperCase``, ``length``)
    as if they were producer keys. Two reads stay outside the extraction for the
    same reason precision is worth more than reach here: a read rooted in an
    expression rather than a name (``(workers[w] || {}).status``), and a read
    inside a helper function the renderer delegates to. Both are owned by the
    periodic audit, not by this ratchet.
    """
    keys: set[str] = set()
    object_roots = {payload_param}
    array_roots: set[str] = set()
    while True:
        alternation = "|".join(sorted(re.escape(r) for r in object_roots))
        keys |= set(re.findall(rf"\b(?:{alternation})\.([A-Za-z_]\w*)", body))

        new_objects: set[str] = set()
        new_arrays: set[str] = set()
        mentions_root = re.compile(rf"\b(?:{alternation})\b")
        for name, rhs in _DECL_RE.findall(body):
            if name in object_roots or name in array_roots:
                continue
            if not mentions_root.search(rhs):
                continue
            stripped = rhs.strip()
            if stripped.endswith("|| {}"):
                new_objects.add(name)
            elif stripped.endswith("|| []"):
                new_arrays.add(name)
            elif not _CALL_RE.search(stripped):
                new_objects.add(name)

        if array_roots:
            element_re = re.compile(
                _ELEMENT_RE_TEMPLATE.format(
                    roots="|".join(sorted(re.escape(r) for r in array_roots))
                )
            )
            new_objects |= {
                m for m in element_re.findall(body) if m not in object_roots
            }

        if not (new_objects - object_roots) and not (new_arrays - array_roots):
            return keys
        object_roots |= new_objects
        array_roots |= new_arrays


def body_renderers(raw: str) -> dict[str, str]:
    """``bodyRender`` key -> renderer function name, from the dispatch map."""
    match = _BODY_RENDERERS_RE.search(raw)
    if match is None:
        return {}
    return dict(_RENDERER_ENTRY_RE.findall(match.group(1)))


def renderer_read_keys(raw: str) -> dict[str, set[str]]:
    """Payload properties each bespoke renderer reads, keyed by ``bodyRender``."""
    reads: dict[str, set[str]] = {}
    for render_key, function_name in body_renderers(raw).items():
        param = _payload_param(raw, function_name)
        body = _function_body(raw, function_name)
        reads[render_key] = _reads_rooted_at(body, param) if param and body else set()
    return reads


def renderer_key_units(raw: str) -> list[SurfaceUnit]:
    """One unit per bespoke-renderer payload read, tiered by its panel."""
    tier_of_renderer = {
        panel.body_render: (T2 if panel.pro else T1)
        for panel in panels(raw)
        if panel.body_render
    }
    units = []
    for render_key, keys in sorted(renderer_read_keys(raw).items()):
        for key in sorted(keys):
            units.append(
                SurfaceUnit(
                    kind=RENDERER_KEY,
                    key=f"{render_key}::{key}",
                    source=f"console.html {render_key} renderer",
                    tier=tier_of_renderer.get(render_key, T3),
                )
            )
    return units


# =============================================================================
# The level-1 inventory
# =============================================================================


def level1_units(
    raw: str | None = None, routes: list[AdminRoute] | None = None
) -> list[SurfaceUnit]:
    """Every level-1 unit, sorted by id — the set a verdict baseline must match."""
    raw = console_html() if raw is None else raw
    units = [
        *panel_units(raw),
        *route_domain_units(routes, raw),
        *body_field_units(raw),
        *renderer_key_units(raw),
    ]
    return sorted(units, key=lambda u: u.id)
