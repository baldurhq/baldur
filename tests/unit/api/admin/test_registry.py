"""AdminRegistry unit tests — 429 PR3-runtime.

Verification targets:
- AdminRoute.match (literal / path-param / method-mismatch)
- AdminRegistry.register / resolve / clear
- Duplicate registration replaces (idempotent on handler-module reloads)
- Phase 1 handlers are auto-registered on first singleton access
- register_admin_route convenience accepts str or HttpMethod
"""

from __future__ import annotations

import pytest

from baldur.api.admin.registry import (
    AdminRegistry,
    AdminRoute,
    get_admin_registry,
    register_admin_route,
    reset_admin_registry,
)
from baldur.interfaces.web_framework import (
    HttpMethod,
    PermissionLevel,
    RequestContext,
    ResponseContext,
)


def _noop_handler(ctx: RequestContext) -> ResponseContext:
    return ResponseContext.json({})


def _other_handler(ctx: RequestContext) -> ResponseContext:
    return ResponseContext.json({"v": 2})


@pytest.fixture(autouse=True)
def _reset_registry():
    reset_admin_registry()
    yield
    reset_admin_registry()


# =============================================================================
# Behavior — AdminRoute.match
# =============================================================================


class TestAdminRouteMatchBehavior:
    """AdminRoute.match returns path params dict on match, None on miss."""

    def test_literal_path_matches_exactly(self):
        route = AdminRoute(HttpMethod.GET, "/health", _noop_handler)
        assert route.match("GET", "/health") == {}

    def test_literal_path_does_not_match_prefix(self):
        route = AdminRoute(HttpMethod.GET, "/health", _noop_handler)
        assert route.match("GET", "/health/extra") is None

    def test_literal_path_does_not_match_suffix(self):
        route = AdminRoute(HttpMethod.GET, "/health", _noop_handler)
        assert route.match("GET", "/api/health") is None

    def test_method_mismatch_returns_none(self):
        route = AdminRoute(HttpMethod.GET, "/health", _noop_handler)
        assert route.match("POST", "/health") is None

    def test_path_param_extracts_named_segment(self):
        route = AdminRoute(HttpMethod.GET, "/cb/{name}", _noop_handler)
        assert route.match("GET", "/cb/payment") == {"name": "payment"}

    def test_path_param_does_not_cross_slashes(self):
        """{name} matches a single segment — nested paths are miss."""
        route = AdminRoute(HttpMethod.GET, "/cb/{name}", _noop_handler)
        assert route.match("GET", "/cb/payment/extra") is None

    def test_multiple_path_params(self):
        route = AdminRoute(HttpMethod.GET, "/dlq/{domain}/{status}", _noop_handler)
        assert route.match("GET", "/dlq/payment/pending") == {
            "domain": "payment",
            "status": "pending",
        }

    def test_missing_path_segment_returns_none(self):
        route = AdminRoute(HttpMethod.GET, "/cb/{name}", _noop_handler)
        assert route.match("GET", "/cb/") is None


# =============================================================================
# Behavior — AdminRegistry.register / resolve
# =============================================================================


class TestAdminRegistryBehavior:
    """Registry is a FIFO list with (method, path) uniqueness guarantee."""

    def test_register_and_resolve_returns_same_route(self):
        reg = AdminRegistry()
        route = AdminRoute(HttpMethod.GET, "/x", _noop_handler)

        reg.register(route)
        result = reg.resolve("GET", "/x")

        assert result is not None
        resolved_route, params = result
        assert resolved_route is route
        assert params == {}

    def test_resolve_returns_none_when_no_match(self):
        reg = AdminRegistry()
        reg.register(AdminRoute(HttpMethod.GET, "/x", _noop_handler))
        assert reg.resolve("GET", "/y") is None

    def test_resolve_first_match_wins_when_multiple_patterns_overlap(self):
        """Insertion order controls resolution (FIFO)."""
        reg = AdminRegistry()
        literal = AdminRoute(HttpMethod.GET, "/cb/list", _noop_handler)
        param = AdminRoute(HttpMethod.GET, "/cb/{name}", _other_handler)
        reg.register(literal)
        reg.register(param)

        # /cb/list matches the literal first
        result = reg.resolve("GET", "/cb/list")
        assert result is not None
        assert result[0] is literal

        # /cb/payment falls through to the param route
        result = reg.resolve("GET", "/cb/payment")
        assert result is not None
        assert result[0] is param
        assert result[1] == {"name": "payment"}

    def test_duplicate_registration_replaces_existing_route(self):
        """Re-registering (GET, /x) with a new handler replaces the old one.
        Protects against handler-module reload duplicating routes."""
        reg = AdminRegistry()
        original = AdminRoute(HttpMethod.GET, "/x", _noop_handler)
        replacement = AdminRoute(HttpMethod.GET, "/x", _other_handler)

        reg.register(original)
        reg.register(replacement)

        assert len(reg.all_routes()) == 1
        resolved_route, _ = reg.resolve("GET", "/x")
        assert resolved_route is replacement

    def test_clear_removes_all_routes(self):
        reg = AdminRegistry()
        reg.register(AdminRoute(HttpMethod.GET, "/a", _noop_handler))
        reg.register(AdminRoute(HttpMethod.POST, "/b", _noop_handler))

        reg.clear()

        assert reg.all_routes() == []
        assert reg.resolve("GET", "/a") is None

    def test_all_routes_returns_snapshot_copy(self):
        """Mutating the returned list does not affect the registry."""
        reg = AdminRegistry()
        reg.register(AdminRoute(HttpMethod.GET, "/x", _noop_handler))

        snapshot = reg.all_routes()
        snapshot.clear()

        # Internal state unchanged
        assert len(reg.all_routes()) == 1


# =============================================================================
# Contract — Phase 1 auto-registration
# =============================================================================


class TestAdminRegistryPhase1Contract:
    """First singleton access pre-registers the 8 Phase 1 handlers (PR3).

    Contract: ``register_phase1_handlers`` wires 9 routes total — 8 unique
    handlers with one extra entry (list + detail) for circuit-breaker status.
    """

    def test_singleton_registers_phase1_routes(self):
        """Phase 1 routes must be present (Phase 2a may add more).

        This test enforces that the 8 Phase 1 handlers (9 routes total —
        circuit-breaker list + detail) are always registered on singleton
        init, regardless of what other phases register alongside them.
        """
        reg = get_admin_registry()
        paths = {(r.method.value, r.path) for r in reg.all_routes()}

        phase1_expected = {
            ("GET", "/liveness"),
            ("GET", "/readiness"),
            ("GET", "/health"),
            ("GET", "/dashboard/summary"),
            ("GET", "/audit/health"),
            ("GET", "/audit/circuit-breakers"),
            ("GET", "/audit/circuit-breakers/{name}"),
            ("GET", "/bulkheads"),
            ("GET", "/throttle/status"),
        }
        assert phase1_expected.issubset(paths)

    def test_liveness_readiness_are_public(self):
        """Kubernetes probes must not require auth."""
        reg = get_admin_registry()
        for path in ("/liveness", "/readiness"):
            result = reg.resolve("GET", path)
            assert result is not None
            route, _ = result
            assert route.permission_level == PermissionLevel.PUBLIC

    def test_other_phase1_routes_are_viewer(self):
        """Non-probe Phase 1 handlers are read-only, VIEWER-level."""
        reg = get_admin_registry()
        for path in (
            "/health",
            "/dashboard/summary",
            "/audit/health",
            "/audit/circuit-breakers",
            "/bulkheads",
            "/throttle/status",
        ):
            result = reg.resolve("GET", path)
            assert result is not None
            route, _ = result
            assert route.permission_level == PermissionLevel.VIEWER


# =============================================================================
# Behavior — register_admin_route convenience
# =============================================================================


class TestRegisterAdminRouteBehavior:
    """register_admin_route convenience wrapper."""

    def test_accepts_string_method(self):
        register_admin_route("GET", "/custom", _noop_handler)
        reg = get_admin_registry()
        assert reg.resolve("GET", "/custom") is not None

    def test_accepts_httpmethod_enum(self):
        register_admin_route(HttpMethod.POST, "/custom", _noop_handler)
        reg = get_admin_registry()
        assert reg.resolve("POST", "/custom") is not None

    def test_default_permission_is_viewer(self):
        register_admin_route("GET", "/custom", _noop_handler)
        reg = get_admin_registry()
        route, _ = reg.resolve("GET", "/custom")
        assert route.permission_level == PermissionLevel.VIEWER

    def test_custom_permission_is_preserved(self):
        register_admin_route(
            "POST",
            "/custom",
            _noop_handler,
            permission_level=PermissionLevel.ADMIN,
        )
        reg = get_admin_registry()
        route, _ = reg.resolve("POST", "/custom")
        assert route.permission_level == PermissionLevel.ADMIN


# =============================================================================
# Behavior — singleton lifecycle
# =============================================================================


class TestAdminRegistrySingletonBehavior:
    """get_admin_registry / reset_admin_registry lifecycle pair."""

    def test_get_returns_cached_instance(self):
        first = get_admin_registry()
        second = get_admin_registry()
        assert first is second

    def test_reset_recreates_singleton_with_phase1_routes(self):
        first = get_admin_registry()
        reset_admin_registry()
        second = get_admin_registry()

        assert first is not second
        # Phase 1 re-registration happens on every fresh singleton.
        assert len(second.all_routes()) >= 7
