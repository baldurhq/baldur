"""Console route-registration unit tests — 536 D2/D5 + 542 D1.

Verification targets:
- ``_register_console_routes(registry)`` wires ``GET /`` at
  ``PermissionLevel.PUBLIC`` (the HTML shell must load without the
  X-Baldur-Admin-Key header a browser top-level navigation cannot send).
- The route is included by the umbrella ``register_all_routes`` registrar so
  the autostarted admin server serves it with no extra wiring.
- (542 D1) ``GET /dlq/facets`` resolves to ``dlq_facets``, NOT to
  ``dlq_detail`` with ``pk="facets"``. ``AdminRegistry.resolve`` returns the
  first matching route and ``/dlq/{pk}`` compiles to ``^/dlq/([^/]+)$`` which
  would swallow the single-segment ``/dlq/facets`` if registered earlier.
"""

from __future__ import annotations

from baldur.api.admin.registry import AdminRegistry
from baldur.api.admin.routes import register_all_routes
from baldur.api.admin.routes.console import _register_console_routes
from baldur.api.admin.routes.dlq import _register_dlq_routes
from baldur.interfaces.web_framework import HttpMethod, PermissionLevel


class TestConsoleRouteRegistration:
    """GET / resolves to the console handler at PUBLIC."""

    def test_register_console_routes_adds_get_root(self):
        registry = AdminRegistry()
        _register_console_routes(registry)

        resolved = registry.resolve("GET", "/")
        assert resolved is not None

    def test_get_root_route_is_public(self):
        """PUBLIC so the shell loads without auth (D5); the JS fetches enforce
        their own VIEWER/OPERATOR/ADMIN levels through the unchanged auth path."""
        registry = AdminRegistry()
        _register_console_routes(registry)

        route, _params = registry.resolve("GET", "/")
        assert route.permission_level == PermissionLevel.PUBLIC

    def test_get_root_route_handler_is_console_page(self):
        from baldur.api.admin.console.handler import console_page

        registry = AdminRegistry()
        _register_console_routes(registry)

        route, _params = registry.resolve("GET", "/")
        assert route.handler is console_page

    def test_register_all_routes_includes_console_route(self):
        """The umbrella registrar wires GET / so the autostarted server serves
        the console with no separate startup call site (D2)."""
        registry = AdminRegistry()
        register_all_routes(registry)

        resolved = registry.resolve("GET", "/")
        assert resolved is not None
        route, _params = resolved
        assert route.permission_level == PermissionLevel.PUBLIC


# =============================================================================
# 542 D1 — /dlq/facets MUST be registered before /dlq/{pk}
# =============================================================================


class TestDlqFacetsRouteOrdering:
    """``/dlq/facets`` shadowing guard — registered before ``/dlq/{pk}``.

    The ``/dlq/{pk}`` route compiles to ``^/dlq/([^/]+)$`` which matches the
    single-segment ``/dlq/facets`` literal. ``AdminRegistry.resolve`` returns
    the first matching ``AdminRoute`` (registry.py:118), so the
    registration ORDER determines the resolved handler. This regression
    guard fails if anyone re-orders ``_register_dlq_routes`` and puts
    ``/dlq/{pk}`` ahead of ``/dlq/facets`` again.
    """

    def test_resolve_returns_dlq_facets_handler_not_detail(self):
        """The resolved handler must be ``dlq_facets`` — not ``dlq_detail``
        with ``pk="facets"`` (D1 shadowing precedent matches ``/dlq/list``)."""
        from baldur.api.handlers.dlq import dlq_detail, dlq_facets

        registry = AdminRegistry()
        _register_dlq_routes(registry)

        resolved = registry.resolve(HttpMethod.GET, "/dlq/facets")
        assert resolved is not None
        route, _params = resolved
        assert route.handler is dlq_facets
        assert route.handler is not dlq_detail

    def test_dlq_facets_route_is_viewer_permission_level(self):
        """``/dlq/facets`` is a read-only operator query — VIEWER (D1)."""
        registry = AdminRegistry()
        _register_dlq_routes(registry)

        route, _params = registry.resolve(HttpMethod.GET, "/dlq/facets")
        assert route.permission_level == PermissionLevel.VIEWER

    def test_dlq_pk_path_still_resolves_for_other_single_segments(self):
        """A literal ``/dlq/123`` must still route to ``dlq_detail`` —
        ``/dlq/facets`` is the only carve-out (D1)."""
        from baldur.api.handlers.dlq import dlq_detail

        registry = AdminRegistry()
        _register_dlq_routes(registry)

        route, params = registry.resolve(HttpMethod.GET, "/dlq/123")
        assert route.handler is dlq_detail
        assert params == {"pk": "123"}

    def test_dlq_pk_path_resolves_for_composite_opaque_ids(self):
        """538 D2 composite opaque ids (``pod:1:abc:0``) still resolve to
        the detail handler — the ``[^/]+`` capture matches them fine."""
        from baldur.api.handlers.dlq import dlq_detail

        registry = AdminRegistry()
        _register_dlq_routes(registry)

        route, params = registry.resolve(HttpMethod.GET, "/dlq/pod-1:abc:0")
        assert route.handler is dlq_detail
        assert params == {"pk": "pod-1:abc:0"}
